"""Crawl4AI-backed page fetch — the single backend for `fetch_page`.

Replaces Serper's paid scrape endpoint (scrape.serper.dev). Crawl4AI drives a real
headless Chromium via Playwright, so it renders JS-built pages that the old
httpx+trafilatura free path silently returned nothing for, and it costs no API credits.
The trade-off, accepted deliberately: a browser must be running, and the first fetch in a
process pays browser startup.

Three things here are load-bearing and easy to get wrong:

1. **The result contract is unchanged.** `{"url","content"}` on success,
   `{"url","content":"","error"}` on failure, never an exception. `app/researcher.py`'s
   `_format_tool_result_as_note` and `_result_is_usable` both key off `content`, and a lane
   must be able to treat any failure as could_not_verify rather than crash.

2. **Crawl4AI's own logger crashes on Windows.** Its default `AsyncLogger` prints Unicode
   box/arrow glyphs through `rich`, which raises `UnicodeEncodeError` on a cp1252 console —
   and the exception escapes `arun()`, so it kills the call rather than just garbling a log
   line (reproduced live 2026-08-12 on `↓`). `_QuietLogger` below replaces it entirely
   and routes to stdlib `logging`, so behavior no longer depends on the console encoding.

3. **One browser per process, bounded concurrency.** `AsyncWebCrawler` is documented as
   reusable across many `arun()` calls, so it is started once lazily and shared — same
   reasoning as `app/llm.py:_get_shared_client`. `_CRAWL_SEMAPHORE` bounds how many pages
   render at once: under batch concurrency (8 leads x up to 3 lanes) an unbounded browser
   would open dozens of tabs and exhaust memory.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.tools import tool

from app.tools.cache import cached_call

logger = logging.getLogger(__name__)

# Chromium renders are memory-heavy; cap simultaneous page loads regardless of how many
# lanes are in flight. Unrelated to API rate limiting (there is no API to rate-limit here),
# so this is a plain semaphore rather than a token bucket in app/tools/ratelimit.py.
_MAX_CONCURRENT_CRAWLS = 4
_CRAWL_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_CRAWLS)

_PAGE_TIMEOUT_MS = 30_000
# Hard ceiling around the whole call. page_timeout covers navigation, but markdown
# generation and a hung browser handshake sit outside it — a lane has its own 240s budget
# and must not spend all of it on one page.
_OVERALL_TIMEOUT_S = 45.0
_MIN_CONTENT_CHARS = 1
# Below this, a "successful" render is almost certainly a JS shell (nav + skip-links only)
# rather than a real page, and is worth one retry with networkidle. Tuned against the
# adviserinfo.sec.gov failure, which returned 595 chars of chrome.
_SHELL_CONTENT_CHARS = 1200

_crawler: Any = None
_crawler_lock = asyncio.Lock()
_crawler_failed: str | None = None


class _QuietLogger:
    """Silent stand-in for Crawl4AI's `AsyncLogger`.

    Implements the full `AsyncLoggerBase` surface (debug/info/success/warning/error/
    url_status/error_status) and forwards to stdlib logging at debug level. This exists to
    stop the rich/cp1252 `UnicodeEncodeError` described in the module docstring from
    escaping `arun()`; it also keeps batch runs from spraying crawl banners into stdout,
    which would corrupt any subprocess JSON contract that shares the stream.
    """

    def debug(self, message: str, tag: str = "DEBUG", **kwargs: Any) -> None:
        logger.debug("crawl4ai[%s] %s", tag, message)

    def info(self, message: str, tag: str = "INFO", **kwargs: Any) -> None:
        logger.debug("crawl4ai[%s] %s", tag, message)

    def success(self, message: str, tag: str = "SUCCESS", **kwargs: Any) -> None:
        logger.debug("crawl4ai[%s] %s", tag, message)

    def warning(self, message: str, tag: str = "WARNING", **kwargs: Any) -> None:
        logger.debug("crawl4ai[%s] %s", tag, message)

    def error(self, message: str, tag: str = "ERROR", **kwargs: Any) -> None:
        logger.debug("crawl4ai[%s] %s", tag, message)

    def url_status(
        self, url: str, success: bool, timing: float, tag: str = "FETCH", url_length: int = 100
    ) -> None:
        logger.debug("crawl4ai fetch %s success=%s %.2fs", url[:url_length], success, timing)

    def error_status(
        self, url: str, error: str, tag: str = "ERROR", url_length: int = 100
    ) -> None:
        logger.debug("crawl4ai error %s: %s", url[:url_length], error)


async def _get_crawler() -> Any:
    """Lazily start one shared AsyncWebCrawler. Raises RuntimeError if Crawl4AI can't
    start (not installed, or `crawl4ai-setup` never run so no browser is present) — the
    caller converts that into the normal error result shape. The failure reason is cached
    so a broken install costs one attempt per process, not one per fetch."""
    global _crawler, _crawler_failed
    if _crawler is not None:
        return _crawler
    async with _crawler_lock:
        if _crawler is not None:
            return _crawler
        if _crawler_failed is not None:
            raise RuntimeError(_crawler_failed)
        try:
            from crawl4ai import AsyncWebCrawler, BrowserConfig

            browser_config = BrowserConfig(
                headless=True,
                verbose=False,
                # text_mode skips images/fonts — we only ever want the text, and it cuts
                # page load time substantially on media-heavy sites.
                text_mode=True,
            )
            crawler = AsyncWebCrawler(config=browser_config, logger=_QuietLogger())
            await crawler.start()
        except Exception as exc:  # noqa: BLE001 — a broken install must degrade, not crash
            _crawler_failed = (
                f"crawl4ai unavailable ({type(exc).__name__}: {exc}) — "
                "is it installed and has `crawl4ai-setup` been run?"
            )
            logger.warning("Crawl4AI failed to start: %s", exc)
            raise RuntimeError(_crawler_failed) from exc
        _crawler = crawler
        return _crawler


def _extract_text(result: Any) -> str:
    """Pull the best available text off a CrawlResult.

    `result.markdown` is a `StringCompatibleMarkdown` whose `.raw_markdown` holds the
    HTML-to-markdown conversion; it is str-compatible, so `str()` is a valid fallback if
    the attribute layout shifts (crawl4ai removed `markdown_v2` in 0.5, so this surface has
    moved before). `cleaned_html` is the last resort — worse for an LLM to read, but far
    better than reporting an empty page.
    """
    markdown = getattr(result, "markdown", None)
    if markdown is not None:
        raw = getattr(markdown, "raw_markdown", None)
        if raw and raw.strip():
            return raw.strip()
        as_str = str(markdown).strip()
        if as_str:
            return as_str
    cleaned = getattr(result, "cleaned_html", None)
    return cleaned.strip() if cleaned else ""


async def crawl_page_raw(url: str) -> dict[str, Any]:
    """Fetch one page and return `{"url","content"}`, or `{"url","content":"","error"}`.

    Never raises. Keeps the exact contract the removed `serper_scrape_raw` had, so it is a
    drop-in replacement at every call site (the `fetch_page` tool and
    `app.tools.freefetch.fetch_page_free_first`'s escalation tier).
    """
    try:
        crawler = await _get_crawler()
    except RuntimeError as exc:
        return {"url": url, "content": "", "error": str(exc)}

    async def _attempt(wait_until: str) -> Any:
        from crawl4ai import CacheMode, CrawlerRunConfig

        run_config = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,  # we do our own caching in app/tools/cache.py
            page_timeout=_PAGE_TIMEOUT_MS,
            wait_until=wait_until,
            verbose=False,
            log_console=False,
        )
        async with _CRAWL_SEMAPHORE:
            return await asyncio.wait_for(
                crawler.arun(url, config=run_config), timeout=_OVERALL_TIMEOUT_S
            )

    def _unwrap(res: Any) -> Any:
        # arun() may hand back a container wrapping the single result depending on version.
        if not hasattr(res, "success") and isinstance(res, (list, tuple)) and res:
            return res[0]
        return res

    try:
        result = _unwrap(await _attempt("domcontentloaded"))
        content = _extract_text(result) if getattr(result, "success", False) else ""

        # Adaptive escalation for JS single-page apps. `domcontentloaded` fires before a
        # client-rendered app has fetched its data, so the crawl "succeeds" and returns a
        # navigation-only shell. That is not a hypothetical: the identity lane fetched
        # adviserinfo.sec.gov/firm/summary/151736 — the SEC adviser record that settles
        # SFO-vs-MFO — and got 595 bytes of skip-links and header chrome, so the decisive
        # evidence never reached the model (found live 2026-08-12). Retrying every page with
        # networkidle would slow all fetches, so escalate only when the first render is
        # suspiciously thin.
        if len(content) < _SHELL_CONTENT_CHARS:
            retried = _unwrap(await _attempt("networkidle"))
            retried_content = _extract_text(retried) if getattr(retried, "success", False) else ""
            if len(retried_content) > len(content):
                result, content = retried, retried_content
    except asyncio.TimeoutError:
        return {"url": url, "content": "", "error": f"crawl timed out after {_OVERALL_TIMEOUT_S}s"}
    except Exception as exc:  # noqa: BLE001 — every failure degrades to could_not_verify
        return {"url": url, "content": "", "error": f"{type(exc).__name__}: {exc}"}

    if not getattr(result, "success", False):
        err = getattr(result, "error_message", None) or "crawl failed"
        status = getattr(result, "status_code", None)
        return {"url": url, "content": "", "error": f"{err}" + (f" (HTTP {status})" if status else "")}

    if len(content) < _MIN_CONTENT_CHARS:
        return {"url": url, "content": "", "error": "page rendered but produced no readable text"}
    return {"url": url, "content": content}


@tool
async def fetch_page(url: str) -> dict[str, Any]:
    """Fetch and extract the readable content of a web page. Renders JavaScript, so it
    works on pages that return an empty shell to a plain HTTP fetch. Use this to read a
    page found via web_search in full before citing it.

    Args:
        url: The page URL to fetch.
    """
    return await cached_call("fetch_page", lambda: crawl_page_raw(url), url=url)


async def close_crawler() -> None:
    """Shut the shared browser down. Batch CLI processes exit on their own so this is not
    required, but tests and long-lived hosts should call it to avoid leaking Chromium."""
    global _crawler
    if _crawler is not None:
        try:
            await _crawler.close()
        except Exception as exc:  # noqa: BLE001 — teardown must never raise
            logger.debug("crawler close failed: %s", exc)
        _crawler = None

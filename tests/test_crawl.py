"""Tests for the Crawl4AI-backed `fetch_page` (app/tools/crawl.py).

Fully offline: the crawler itself is stubbed. What matters here is not that Chromium works
(that is verified by hand against live URLs) but that the RESULT CONTRACT holds, because
app/researcher.py's `_format_tool_result_as_note` and `_result_is_usable` both key off
`content` and a lane must never see an exception.
"""
from __future__ import annotations

import asyncio
import types

import pytest

import app.tools.crawl as crawl_mod


class _FakeMarkdown:
    """Mimics crawl4ai's StringCompatibleMarkdown: has .raw_markdown and is str()-able."""

    def __init__(self, raw: str, as_str: str | None = None):
        self.raw_markdown = raw
        self._as_str = as_str if as_str is not None else raw

    def __str__(self) -> str:
        return self._as_str


def _result(*, success=True, status_code=200, markdown="body text here",
            cleaned_html="<p>body</p>", error_message=""):
    return types.SimpleNamespace(
        success=success,
        status_code=status_code,
        error_message=error_message,
        url="https://x",
        cleaned_html=cleaned_html,
        markdown=_FakeMarkdown(markdown) if markdown is not None else None,
    )


class _FakeCrawler:
    def __init__(self, result, raises: BaseException | None = None, delay: float = 0.0):
        self._result = result
        self._raises = raises
        self._delay = delay
        self.calls: list[str] = []

    async def arun(self, url, config=None):
        self.calls.append(url)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises:
            raise self._raises
        return self._result

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset_crawler_state(monkeypatch):
    """The crawler is a module global; reset it between tests so one test's stub or cached
    failure can't leak into the next."""
    monkeypatch.setattr(crawl_mod, "_crawler", None)
    monkeypatch.setattr(crawl_mod, "_crawler_failed", None)
    yield
    crawl_mod._crawler = None
    crawl_mod._crawler_failed = None


def _install(monkeypatch, crawler):
    async def _get():
        return crawler

    monkeypatch.setattr(crawl_mod, "_get_crawler", _get)


async def test_success_returns_url_and_content(monkeypatch):
    _install(monkeypatch, _FakeCrawler(_result(markdown="Acme Family Office is a single-family office.")))
    out = await crawl_mod.crawl_page_raw("https://x")
    assert out == {"url": "https://x", "content": "Acme Family Office is a single-family office."}
    assert "error" not in out


async def test_prefers_raw_markdown_over_str(monkeypatch):
    r = _result()
    r.markdown = _FakeMarkdown("RAW", as_str="STRINGIFIED")
    _install(monkeypatch, _FakeCrawler(r))
    out = await crawl_mod.crawl_page_raw("https://x")
    assert out["content"] == "RAW"


async def test_falls_back_to_str_when_raw_markdown_empty(monkeypatch):
    """crawl4ai has moved this surface before (markdown_v2 was removed in 0.5), so the
    stringified form is a deliberate safety net rather than dead code."""
    r = _result()
    r.markdown = _FakeMarkdown("", as_str="STRINGIFIED")
    _install(monkeypatch, _FakeCrawler(r))
    out = await crawl_mod.crawl_page_raw("https://x")
    assert out["content"] == "STRINGIFIED"


async def test_falls_back_to_cleaned_html_when_no_markdown(monkeypatch):
    r = _result(markdown=None, cleaned_html="<p>fallback body</p>")
    _install(monkeypatch, _FakeCrawler(r))
    out = await crawl_mod.crawl_page_raw("https://x")
    assert out["content"] == "<p>fallback body</p>"


async def test_unsuccessful_crawl_degrades_with_error(monkeypatch):
    _install(monkeypatch, _FakeCrawler(_result(success=False, status_code=403,
                                               error_message="Blocked by anti-bot protection")))
    out = await crawl_mod.crawl_page_raw("https://x")
    assert out["content"] == ""
    assert "Blocked by anti-bot" in out["error"]
    assert "403" in out["error"]


async def test_exception_inside_arun_never_propagates(monkeypatch):
    """A lane must be able to treat any fetch failure as could_not_verify. Crawl4AI's own
    logger has been observed raising UnicodeEncodeError out of arun() on a cp1252 Windows
    console, so this is a real path, not defensive theatre."""
    _install(monkeypatch, _FakeCrawler(None, raises=UnicodeEncodeError("charmap", "x", 0, 1, "boom")))
    out = await crawl_mod.crawl_page_raw("https://x")
    assert out["content"] == ""
    assert "UnicodeEncodeError" in out["error"]


async def test_timeout_degrades_with_error(monkeypatch):
    monkeypatch.setattr(crawl_mod, "_OVERALL_TIMEOUT_S", 0.05)
    _install(monkeypatch, _FakeCrawler(_result(), delay=1.0))
    out = await crawl_mod.crawl_page_raw("https://x")
    assert out["content"] == ""
    assert "timed out" in out["error"]


async def test_rendered_but_empty_page_is_an_error_not_silent_success(monkeypatch):
    _install(monkeypatch, _FakeCrawler(_result(markdown="", cleaned_html="")))
    out = await crawl_mod.crawl_page_raw("https://x")
    assert out["content"] == ""
    assert "no readable text" in out["error"]


async def test_unavailable_crawler_degrades_without_raising(monkeypatch):
    async def _boom():
        raise RuntimeError("crawl4ai unavailable (ImportError: no module) — is it installed?")

    monkeypatch.setattr(crawl_mod, "_get_crawler", _boom)
    out = await crawl_mod.crawl_page_raw("https://x")
    assert out["content"] == ""
    assert "crawl4ai unavailable" in out["error"]


async def test_result_container_is_unwrapped(monkeypatch):
    """Depending on version, arun() can hand back a container wrapping the single result."""
    inner = _result(markdown="unwrapped body")
    _install(monkeypatch, _FakeCrawler([inner]))
    out = await crawl_mod.crawl_page_raw("https://x")
    assert out["content"] == "unwrapped body"


async def test_fetch_page_tool_caches_successful_fetch(monkeypatch, db_path):
    import dataclasses

    import app.db as db_module

    monkeypatch.setattr(
        db_module, "SETTINGS", dataclasses.replace(db_module.SETTINGS, db_path=db_path)
    )
    body = "cached body " * 200  # above _SHELL_CONTENT_CHARS so no networkidle retry
    crawler = _FakeCrawler(_result(markdown=body))
    _install(monkeypatch, crawler)

    first = await crawl_mod.fetch_page.ainvoke({"url": "https://cache-me"})
    second = await crawl_mod.fetch_page.ainvoke({"url": "https://cache-me"})
    assert first["content"] == body.strip()
    assert second["cache_hit"] is True
    assert len(crawler.calls) == 1, "second call must come from cache, not the browser"


async def test_fetch_page_tool_does_not_cache_failures(monkeypatch, db_path):
    import dataclasses

    import app.db as db_module

    monkeypatch.setattr(
        db_module, "SETTINGS", dataclasses.replace(db_module.SETTINGS, db_path=db_path)
    )
    crawler = _FakeCrawler(_result(success=False, error_message="boom"))
    _install(monkeypatch, crawler)

    await crawl_mod.fetch_page.ainvoke({"url": "https://fails"})
    after_first = len(crawler.calls)
    await crawl_mod.fetch_page.ainvoke({"url": "https://fails"})
    assert len(crawler.calls) > after_first, "a failed fetch must not be cached"


# --- adaptive networkidle retry for JS single-page apps ---


async def test_thin_render_triggers_networkidle_retry(monkeypatch):
    """Regression: adviserinfo.sec.gov (the SEC adviser record that settles SFO-vs-MFO) is
    an Angular SPA. With wait_until="domcontentloaded" it "succeeded" and returned 595 bytes
    of skip-links and header chrome, so the decisive registration data never reached the
    model and G1.Q4 stayed could_not_verify (found live 2026-08-12)."""
    waits: list[str] = []

    class _SpaCrawler:
        async def arun(self, url, config=None):
            waits.append(getattr(config, "wait_until", None))
            # First pass (domcontentloaded) yields a shell; networkidle yields the real page.
            if len(waits) == 1:
                return _result(markdown="skip to main content nav")
            return _result(markdown="REAL PAGE BODY " * 200)

        async def close(self):
            pass

    _install(monkeypatch, _SpaCrawler())
    out = await crawl_mod.crawl_page_raw("https://adviserinfo.sec.gov/firm/summary/151736")
    assert waits == ["domcontentloaded", "networkidle"]
    assert "REAL PAGE BODY" in out["content"]
    assert len(out["content"]) > crawl_mod._SHELL_CONTENT_CHARS


async def test_substantial_first_render_skips_the_retry(monkeypatch):
    """Retrying every page with networkidle would slow all fetches, so a healthy render
    must not escalate."""
    crawler = _FakeCrawler(_result(markdown="a real page body " * 200))
    _install(monkeypatch, crawler)
    out = await crawl_mod.crawl_page_raw("https://example.com/full")
    assert len(crawler.calls) == 1
    assert out["content"]


async def test_retry_keeps_first_result_when_escalation_is_no_better(monkeypatch):
    """If networkidle returns no more text, keep the original result rather than
    downgrading to whatever the retry produced."""
    class _NoBetter:
        def __init__(self):
            self.n = 0

        async def arun(self, url, config=None):
            self.n += 1
            if self.n == 1:
                return _result(markdown="short body")
            # Genuinely emptier: no markdown AND no cleaned_html to fall back to.
            return _result(markdown="", cleaned_html="")

        async def close(self):
            pass

    _install(monkeypatch, _NoBetter())
    out = await crawl_mod.crawl_page_raw("https://x")
    assert out["content"] == "short body"

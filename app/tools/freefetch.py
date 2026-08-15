"""Free-path page fetch: httpx GET + trafilatura extraction, no credit spend, no rate
limit. "Free fetch first" (enrichment_validation_dataset_plan.md's wave 1 credit
discipline + Layer V's V1 ring-fence, both quoting research_layer_plan.md §4.4's
original httpx+trafilatura rule).

Used by the Enrichment/Validation layers. `fetch_page_free_first`'s escalation target is
now Crawl4AI (app/tools/crawl.py) rather than Serper's paid scrape endpoint: the fallback
is a headless-browser render, which handles the JS-built team/contact pages this tier
exists to reach, and costs no API credits. Layer 1's own `fetch_page` tool is Crawl4AI
directly (no trafilatura tier), so the two layers now share one page-fetch backend even
though Enrichment keeps its cheap fast path in front of it."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import trafilatura

from app.toollog import logged

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FO-Intelligence-Agent/1.0; +research)"}
logger = logging.getLogger(__name__)

_MIN_CONTENT_CHARS = 200


# One pooled client for every free fetch, instead of a fresh AsyncClient per call.
#
# Constructing an AsyncClient builds an SSL context, and `ssl.create_default_context()`
# loads the entire system CA store — hundreds of certificates parsed synchronously, on
# the event loop. One call is a few milliseconds; Layer V's V1 ring-fence makes one per
# claim per lead, across three concurrent leads, and the loop spends all its time in
# `ssl.py:create_default_context` instead of running coroutines.
#
# Diagnosed from a live stall, not from review: a scheduled run sat for ten minutes with
# 0 leads processed, no tool calls, no log output and ~60% of a core burning. py-spy gave
# the answer directly —
#
#     create_default_context (ssl.py:707)
#     create_ssl_context (httpx/_config.py:40)
#     __init__ (httpx/_transports/default.py:297)
#     free_fetch_raw (app/tools/freefetch.py:30)
#     _default_v1_fetch (app/validation.py:747)
#
# app/llm.py already solved exactly this for the LLM path (`_get_shared_client`) after
# connection resets under batch concurrency; this is the same fix for the fetch path.
# No shutdown hook: the client's lifetime is the process's, and both the CLI and the
# scheduler service exit as a unit.
_shared_client: httpx.AsyncClient | None = None


def _get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers=_HEADERS,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _shared_client


# Above this many characters a page is not read at all.
#
# `trafilatura.extract` is synchronous, and its `repair_faulty_html` step does string
# surgery whose cost is not linear in page size. On one real page it consumed the whole
# service: the loop parked inside `repair_faulty_html` while memory climbed 809MB -> 2.1GB
# in eight seconds, on its way to an OOM. A firm's team or contact page is tens of KB;
# 5MB of markup is a sitemap, a data dump or a malformed page, and there is nothing in it
# this tier wants. Refusing it costs a fetch that was going to fail anyway.
_MAX_EXTRACT_CHARS = 5_000_000

# Wall-clock ceiling on one extraction, independent of size. The size guard catches the
# page that is obviously too big; this catches the one that is small but pathological.
_EXTRACT_TIMEOUT_S = 20.0


async def _extract_text(html: str) -> str | None:
    """Run trafilatura off the event loop, size-guarded and time-boxed.

    Two separate failures made this necessary, both diagnosed from live stack dumps of a
    wedged run rather than from review:

    1. It ran ON the event loop. Synchronous CPU-bound parsing there stops every other
       coroutine — the scheduler's other leads, the progress publisher, and the HTTP
       server itself, which stopped answering /api/scheduler/live entirely. A page that
       merely parses slowly should cost that page, not the whole service.
    2. It had no bound. One page sent memory to 2.1GB and climbing with no way out.

    A page that trips either guard is treated exactly like an unparseable one: None, and
    the caller escalates to the headless-browser tier, which is what that tier is for."""
    if len(html) > _MAX_EXTRACT_CHARS:
        logger.info("skipping extraction: %d chars exceeds the %d-char ceiling",
                    len(html), _MAX_EXTRACT_CHARS)
        return None
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                trafilatura.extract, html, include_comments=False, include_tables=False
            ),
            timeout=_EXTRACT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        # The worker thread is left to finish on its own — there is no way to cancel a
        # thread mid-call. It holds the GIL in bursts rather than continuously, so the
        # loop keeps running; what matters is that this coroutine is no longer waiting.
        logger.warning("extraction exceeded %ss — treating the page as unparseable",
                       _EXTRACT_TIMEOUT_S)
        return None
    except Exception:  # noqa: BLE001 — a parser crash is an unparseable page, not an error
        logger.debug("extraction raised", exc_info=True)
        return None


# Hard ceiling on how many bytes are pulled off the wire for one page.
#
# The guard has to sit HERE, not after the download. `resp.text` materialises the whole
# body and decodes it into a str; on an oversized response that is where the process
# dies:
#
#     free_fetch_raw (app/tools/freefetch.py) -> html = resp.text
#       httpx/_models.py:649 in text -> decoder.decode(self.content)
#         <frozen codecs>:322 in decode
#     MemoryError
#
# — five times in one run's log, with the process at 2.1GB. A size check on the decoded
# string can never fire, because reaching it is what kills the process. Streaming with a
# running total bounds the cost of a hostile or broken response at _MAX_DOWNLOAD_BYTES
# no matter what the server claims or sends.
_MAX_DOWNLOAD_BYTES = 5_000_000


async def _get_bounded(url: str) -> str | None:
    """GET `url`, refusing anything over `_MAX_DOWNLOAD_BYTES`. Returns decoded text, or
    None if the response was too large, errored, or could not be decoded.

    Content-Length is honoured when present as a cheap early exit, but not trusted: a
    missing or lying header is exactly the case that caused the OOM, so the running total
    over the streamed chunks is the real bound."""
    try:
        async with _get_shared_client().stream("GET", url) as resp:
            resp.raise_for_status()
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > _MAX_DOWNLOAD_BYTES:
                logger.info("refusing %s: content-length %s exceeds %d bytes",
                            url, declared, _MAX_DOWNLOAD_BYTES)
                return None
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > _MAX_DOWNLOAD_BYTES:
                    # Abandon mid-stream. The context manager closes the connection, so
                    # the remaining bytes are never pulled.
                    logger.info("abandoning %s: body exceeded %d bytes", url, _MAX_DOWNLOAD_BYTES)
                    return None
                chunks.append(chunk)
            encoding = resp.encoding or "utf-8"
    except httpx.HTTPError:
        return None
    except MemoryError:
        # Belt and braces: a body small enough to accept can still be pathological to
        # decode. A page that cannot be read is an unparseable page, not a crash.
        logger.warning("ran out of memory reading %s — treating it as unfetchable", url)
        return None

    try:
        return b"".join(chunks).decode(encoding, errors="replace")
    except (LookupError, MemoryError):
        return None


async def free_fetch_raw(url: str) -> dict[str, Any] | None:
    """Returns {"url","content"} on success (non-trivial extracted text), or None on any
    failure (connection error, non-2xx, oversized, unparseable, too little extracted
    text) so callers can fall back to a paid scrape without treating this as an error
    state."""
    html = await _get_bounded(url)
    if html is None:
        return None

    text = await _extract_text(html)
    if not text or len(text.strip()) < _MIN_CONTENT_CHARS:
        return None
    return {"url": url, "content": text.strip()}


@logged("fetch_raw_html")
async def fetch_raw_html(url: str) -> str | None:
    """Unextracted HTML — free, same GET as free_fetch_raw but without running it
    through trafilatura, which strips <script> blocks (including JSON-LD) as part of
    its job. Used by wave 1's JSON-LD principal-name parsing (plan §4: "Site team pages
    often carry JSON-LD Person/Organization blocks... parse the JSON-LD before the
    prose"). Returns None on any failure — never raises.

    Size-bounded for the same reason as `free_fetch_raw`: this reads raw HTML, so an
    oversized body is if anything more dangerous here — there is no extraction step to
    discard it, and the whole string is handed to the JSON-LD parser."""
    return await _get_bounded(url)


@logged("fetch_page_free_first")
async def fetch_page_free_first(url: str, fallback=None) -> dict[str, Any]:
    """Tries the cheap httpx+trafilatura path first, then escalates.

    `fallback` is an async `url -> dict` callable; it defaults to
    `app.tools.crawl.crawl_page_raw` (headless-browser render). Passing it explicitly is
    still supported so tests can inject a stub without patching the module.

    Tags the result with `extraction_method` either way so callers building a Claim can
    record how the content was actually obtained (plan §7: "extraction_method matters more
    than it looks"). Layer V's V1 ring-fence reads this tag to decide whether a fetch cost
    a paid credit — with Crawl4AI as the fallback, neither tier does, so V1's credit
    budget is now effectively only limited by wall time.
    """
    free = await free_fetch_raw(url)
    if free is not None:
        return {**free, "extraction_method": "httpx_trafilatura"}
    if fallback is None:
        # Imported lazily: app.tools.crawl imports crawl4ai, which pulls in Playwright.
        # Keeping it out of module scope means the Enrichment layer doesn't pay that
        # import cost (or fail) unless a free fetch actually missed.
        from app.tools.crawl import crawl_page_raw

        fallback = crawl_page_raw
    result = await fallback(url)
    return {**result, "extraction_method": result.get("extraction_method", "crawl4ai")}

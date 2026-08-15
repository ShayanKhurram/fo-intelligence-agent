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

from typing import Any

import httpx
import trafilatura

from app.toollog import logged

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FO-Intelligence-Agent/1.0; +research)"}
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


async def free_fetch_raw(url: str) -> dict[str, Any] | None:
    """Returns {"url","content"} on success (non-trivial extracted text), or None on any
    failure (connection error, non-2xx, unparseable, too little extracted text) so
    callers can fall back to a paid scrape without treating this as an error state."""
    try:
        resp = await _get_shared_client().get(url)
        resp.raise_for_status()
        html = resp.text
    except httpx.HTTPError:
        return None

    text = trafilatura.extract(html, include_comments=False, include_tables=False)
    if not text or len(text.strip()) < _MIN_CONTENT_CHARS:
        return None
    return {"url": url, "content": text.strip()}


@logged("fetch_raw_html")
async def fetch_raw_html(url: str) -> str | None:
    """Unextracted HTML — free, same GET as free_fetch_raw but without running it
    through trafilatura, which strips <script> blocks (including JSON-LD) as part of
    its job. Used by wave 1's JSON-LD principal-name parsing (plan §4: "Site team pages
    often carry JSON-LD Person/Organization blocks... parse the JSON-LD before the
    prose"). Returns None on any failure — never raises."""
    try:
        resp = await _get_shared_client().get(url)
        resp.raise_for_status()
        return resp.text
    except httpx.HTTPError:
        return None


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

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


async def free_fetch_raw(url: str) -> dict[str, Any] | None:
    """Returns {"url","content"} on success (non-trivial extracted text), or None on any
    failure (connection error, non-2xx, unparseable, too little extracted text) so
    callers can fall back to a paid scrape without treating this as an error state."""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=_HEADERS) as client:
            resp = await client.get(url)
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
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=_HEADERS) as client:
            resp = await client.get(url)
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

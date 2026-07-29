"""Free-path page fetch: httpx GET + trafilatura extraction, no credit spend, no rate
limit. "Free fetch first" (enrichment_validation_dataset_plan.md's wave 1 credit
discipline + Layer V's V1 ring-fence, both quoting research_layer_plan.md §4.4's
original httpx+trafilatura rule).

Deliberately scoped to the NEW Enrichment/Validation layers only — Layer 1's own
`fetch_page` tool (app/tools/serper.py) went through T13's review already and is left
untouched here rather than risk regressing already-tested behavior other code depends
on. If Layer 1 adopts a free-first fetch later, this is the function to reuse."""
from __future__ import annotations

from typing import Any

import httpx
import trafilatura

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


async def fetch_page_free_first(url: str, paid_fallback) -> dict[str, Any]:
    """`paid_fallback` is an async `url -> dict` callable, e.g.
    app.tools.serper.serper_scrape_raw. Tries the free path first; only spends the paid
    credit if it fails. Tags the result with `extraction_method` either way so callers
    building a Claim can record how the content was actually obtained (plan §7:
    "extraction_method matters more than it looks")."""
    free = await free_fetch_raw(url)
    if free is not None:
        return {**free, "extraction_method": "httpx_trafilatura"}
    result = await paid_fallback(url)
    return {**result, "extraction_method": "serper_scrape"}

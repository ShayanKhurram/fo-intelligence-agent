"""Serper.dev client — backs BOTH `web_search` (Google SERP via google.serper.dev) and
`fetch_page` (page scrape via scrape.serper.dev). This replaces the self-hosted
OrioSearch/SearXNG backend entirely: SearXNG returned non-empty but irrelevant results on
long-tail entity queries (verified live: family-office names came back as Japanese Q&A
pages, embroidery shops, math calculators), which is fatal for an obscure-entity
qualification pipeline, and OrioSearch's local Docker container was a single point of
failure that took down a whole batch when it went down. Serper is a cloud API with no
local process to keep alive.

The public `web_search` and `fetch_page` tools keep their identical return shapes so
nothing downstream (researcher.py note formatting, lane wiring) changes:
    web_search: {"results": [{"title","url","content","score"}], "query": str}
    fetch_page: {"url": str, "content": str}
Both degrade to an error shape on failure (missing key, HTTP error, JSON error) rather
than raising — callers must be able to treat could_not_verify uniformly and never have a
lane crash. When SERPER_API_KEY is unset, both degrade to an empty/error result with no
HTTP call."""
from __future__ import annotations

from typing import Any, Literal

import httpx
from langchain_core.tools import tool

from app.config import SETTINGS
from app.tools.keyrotation import KeyRotator
from app.tools.ratelimit import SERPER_BUCKET

# Process-global rotator, built once from SETTINGS.tools.serper_api_keys (comma-
# separated multi-key support, app/tools/keyrotation.py). A 429 on the current key
# rotates forward to the next configured key and retries the SAME request rather than
# giving up — a long batch run shouldn't stall because one key hit its rate/credit
# limit while others still have headroom.
_SERPER_ROTATOR = KeyRotator(SETTINGS.tools.serper_api_keys)


async def serper_search_raw(
    query: str, topic: str = "general", max_results: int = 5
) -> dict[str, Any]:
    """Raw Serper request + mapping to our result shape. Returns
    {"results": [{"title","url","content","score"?:None,"date"?}], "query": str} on
    success, or {"results": [], "query": query, "error": str} on any failure (missing
    key, HTTP error, JSON error). Never raises — a missing key degrades like any other
    failure so callers treat it as could_not_verify, never crash a lane. On a 429,
    rotates to the next configured key (if any) and retries before giving up."""
    if _SERPER_ROTATOR.current is None:
        return {"results": [], "query": query, "error": "SERPER_API_KEY not set"}

    base = SETTINGS.tools.serper_base_url
    endpoint = f"{base}/news" if topic == "news" else f"{base}/search"
    payload = {"q": query, "num": max_results}

    data: dict[str, Any] | None = None
    while True:
        key = _SERPER_ROTATOR.current
        if key is None:
            return {"results": [], "query": query, "error": "all Serper API keys exhausted (429)"}
        headers = {"X-API-KEY": key, "Content-Type": "application/json"}
        await SERPER_BUCKET.acquire()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
                if resp.status_code == 429:
                    if _SERPER_ROTATOR.rotate():
                        continue
                    return {"results": [], "query": query, "error": "all Serper API keys exhausted (429)"}
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            return {"results": [], "query": query, "error": str(exc)}
        except ValueError as exc:  # JSON decode failure
            return {"results": [], "query": query, "error": str(exc)}
        break

    raw_items = data.get("news", []) if topic == "news" else data.get("organic", [])
    results: list[dict[str, Any]] = []
    for item in raw_items[:max_results]:
        mapped: dict[str, Any] = {
            "title": item.get("title"),
            "url": item.get("link"),
            "content": item.get("snippet"),
            "score": None,
        }
        if "date" in item:
            mapped["date"] = item.get("date")
        results.append(mapped)
    return {"results": results, "query": query}


async def serper_scrape_raw(url: str) -> dict[str, Any]:
    """Raw Serper scrape request + mapping to our fetch_page result shape. Returns
    {"url": url, "content": str} on success, or {"url": url, "content": "", "error": str}
    on any failure (missing key, HTTP error, JSON error). Never raises — a missing key
    degrades like any other failure so callers treat it as could_not_verify, never crash
    a lane. Verified live contract: POST scrape.serper.dev with body {"url": ...} returns
    {"text": "<full page text>", "metadata": {...}, "jsonld": ..., "credits": N}; we map
    `text` -> `content`. On a 429, rotates to the next configured key (if any) and
    retries before giving up."""
    if _SERPER_ROTATOR.current is None:
        return {"url": url, "content": "", "error": "SERPER_API_KEY not set"}

    endpoint = SETTINGS.tools.serper_scrape_url
    payload = {"url": url}

    data: dict[str, Any] | None = None
    while True:
        key = _SERPER_ROTATOR.current
        if key is None:
            return {"url": url, "content": "", "error": "all Serper API keys exhausted (429)"}
        headers = {"X-API-KEY": key, "Content-Type": "application/json"}
        await SERPER_BUCKET.acquire()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
                if resp.status_code == 429:
                    if _SERPER_ROTATOR.rotate():
                        continue
                    return {"url": url, "content": "", "error": "all Serper API keys exhausted (429)"}
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            return {"url": url, "content": "", "error": str(exc)}
        except ValueError as exc:  # JSON decode failure
            return {"url": url, "content": "", "error": str(exc)}
        break

    return {"url": url, "content": data.get("text", "")}


@tool
async def web_search(
    query: str,
    topic: Literal["general", "news"] = "general",
    max_results: int = 5,
) -> dict[str, Any]:
    """Search the web via Google (Serper API). Returns raw organic results with
    title/url/content — never a synthesized answer. An empty result list means the
    search failed or genuinely found nothing; treat that as could_not_verify, not as
    proof of absence.

    Args:
        query: The search query.
        topic: "general" or "news".
        max_results: Maximum number of results to return.
    """
    return await serper_search_raw(query, topic=topic, max_results=max_results)


@tool
async def fetch_page(url: str) -> dict[str, Any]:
    """Fetch and extract the readable content of a web page (via Serper's scrape API).
    Use this to read a page found via web_search in full before citing it.

    Args:
        url: The page URL to fetch.
    """
    return await serper_scrape_raw(url)
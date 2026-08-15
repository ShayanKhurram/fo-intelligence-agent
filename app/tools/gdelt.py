"""GDELT DOC 2.0 client — news_search (plan §4.7). Keyless, free, dated. `seendate` is
the field that makes the activity_signals lane's recency claims possible — never drop it.

GDELT publishes no official rate limit, but the first live pilot run got 429 Too Many
Requests on every single call under 8-way batch concurrency — GDELT_BUCKET (a module-
global token bucket, same pattern as SEC/LinkedIn) plus a short retry keep this from
silently returning zero news data across an entire batch."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.tools.httpclient import shared_client
from langchain_core.tools import tool

from app.config import SETTINGS
from app.tools.cache import cached_call
from app.tools.ratelimit import GDELT_BUCKET
from app.toollog import logged

_MAX_ATTEMPTS = 3


@logged("news_search_raw")
async def news_search_raw(
    query: str, lookback_days: int = 365, max_records: int = 75
) -> dict[str, Any]:
    """Returns {"results": [{"title","url","seendate","domain"}], "query": str} or, on
    failure, {"results": [], "query": str, "error": str}."""
    start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime(
        "%Y%m%d%H%M%S"
    )
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "startdatetime": start,
        "maxrecords": str(max_records),
    }

    data = None
    last_error: str | None = None
    for attempt in range(_MAX_ATTEMPTS):
        await GDELT_BUCKET.acquire()
        try:
            client = shared_client(timeout=30.0)
            resp = await client.get(SETTINGS.tools.gdelt_base_url, params=params)
            if resp.status_code == 429:
                last_error = "429 Too Many Requests"
                if attempt < _MAX_ATTEMPTS - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                break
            resp.raise_for_status()
            data = resp.json()
            break
        except (httpx.HTTPError, ValueError) as exc:
            last_error = str(exc)
            break

    if data is None:
        return {"results": [], "query": query, "error": last_error or "GDELT request failed"}

    articles = data.get("articles", [])
    results = [
        {
            "title": a.get("title"),
            "url": a.get("url"),
            "seendate": a.get("seendate"),
            "domain": a.get("domain"),
        }
        for a in articles
    ]
    return {"results": results, "query": query}


@tool
async def news_search(
    query: str, lookback_days: int = 365, max_records: int = 75
) -> dict[str, Any]:
    """Search GDELT for dated news coverage of an entity. Use this for recency claims
    (recent exits/hires/commitments, scandal checks) — every result carries a `seendate`
    that is the dated evidence a recency claim needs.

    Args:
        query: Entity name or name + keyword (e.g. '"Acme Family Office" lawsuit').
        lookback_days: How far back to search, in days.
        max_records: Maximum number of articles to return.
    """
    return await cached_call(
        "news_search",
        lambda: news_search_raw(query, lookback_days=lookback_days, max_records=max_records),
        query=query,
        lookback_days=lookback_days,
        max_records=max_records,
    )

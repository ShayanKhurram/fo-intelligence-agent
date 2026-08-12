"""Small local cache for tool results — GDELT/EDGAR/ProPublica/LinkedIn/Snov/fetch_page
(plan §5). Backed by the `tool_cache` table in db.py; wrapped in asyncio.to_thread so a
blocking sqlite3 call never stalls the event loop.

`web_search` is the one deliberate exception: SERP results for the same query legitimately
change between runs, and it is the cheapest paid call, so it stays uncached. (An earlier
docstring here claimed OrioSearch's Redis handled web_search/fetch_page caching — that
backend was replaced by Serper long ago and the claim was stale, which is why `fetch_page`
went uncached for months.)

**Empty results are never cached.** See `_is_empty_result`: a tool that returns no rows is
usually reporting a transient failure (exhausted credits, a blocked proxy, a throttled
API), and caching that answer makes a temporary outage permanent. This was not theoretical
— an exhausted ScrapeOps balance was cached as a clean empty for 26/26 `linkedin_jobs`
rows, 54/62 `linkedin_lookup` rows and 474/674 `news_search` rows, so those lookups kept
returning nothing long after the underlying cause was understood (found live 2026-08-12)."""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Awaitable, Callable

from app.db import cache_get, cache_set, connection


def _cache_key(tool_name: str, **kwargs: Any) -> str:
    raw = json.dumps({"tool": tool_name, **kwargs}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sync_get(key: str) -> dict[str, Any] | None:
    with connection() as conn:
        return cache_get(conn, key)


def _sync_set(key: str, tool_name: str, response: dict[str, Any]) -> None:
    with connection() as conn:
        cache_set(conn, key, tool_name, response)


def _is_empty_result(result: dict[str, Any]) -> bool:
    """True if this result carries no usable payload. Mirrors the emptiness checks in
    `app/researcher.py:_result_is_usable` so the cache and the evidence-detection logic
    agree on what "nothing" means.

    A dict with neither a `results` list nor a `content` string (e.g. edgar_submissions,
    which returns named fields) is NOT considered empty — those legitimately return a flat
    record, and refusing to cache them would defeat the cache for the tools that need it
    most."""
    if "results" in result:
        return not result.get("results")
    if "content" in result:
        return not (result.get("content") or "").strip()
    return False


async def cached_call(
    tool_name: str,
    fn: Callable[[], Awaitable[dict[str, Any]]],
    **key_kwargs: Any,
) -> dict[str, Any]:
    key = _cache_key(tool_name, **key_kwargs)
    cached = await asyncio.to_thread(_sync_get, key)
    if cached is not None:
        return {**cached, "cache_hit": True}
    result = await fn()
    # Never persist an error OR an empty payload — both are far more often a transient
    # backend failure than a durable fact about the world (see module docstring).
    if not result.get("error") and not _is_empty_result(result):
        await asyncio.to_thread(_sync_set, key, tool_name, result)
    return result

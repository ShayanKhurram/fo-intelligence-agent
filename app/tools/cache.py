"""Small local cache for tools without their own cache layer — GDELT/EDGAR/ProPublica/
LinkedIn (plan §5). OrioSearch owns web_search/fetch_page caching via its own Redis, so
those two never touch this. Backed by the `tool_cache` table in db.py; wrapped in
asyncio.to_thread so a blocking sqlite3 call never stalls the event loop."""
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
    if not result.get("error"):
        await asyncio.to_thread(_sync_set, key, tool_name, result)
    return result

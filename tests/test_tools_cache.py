from __future__ import annotations

import dataclasses

import app.db as db_module
from app.tools.cache import cached_call


def _patch_db_path(monkeypatch, db_path: str) -> None:
    monkeypatch.setattr(db_module, "SETTINGS", dataclasses.replace(db_module.SETTINGS, db_path=db_path))


async def test_cached_call_hits_second_time(db_path, monkeypatch):
    _patch_db_path(monkeypatch, db_path)
    calls = []

    async def fn():
        calls.append(1)
        return {"results": [{"title": "x"}], "query": "q"}

    r1 = await cached_call("news_search", fn, query="acme")
    r2 = await cached_call("news_search", fn, query="acme")
    assert len(calls) == 1  # fn only actually ran once
    assert r1["results"] == r2["results"]
    assert r2.get("cache_hit") is True


async def test_cached_call_does_not_cache_errors(db_path, monkeypatch):
    _patch_db_path(monkeypatch, db_path)
    calls = []

    async def fn():
        calls.append(1)
        return {"results": [], "error": "boom"}

    await cached_call("news_search", fn, query="acme")
    await cached_call("news_search", fn, query="acme")
    assert len(calls) == 2  # error responses are never cached


async def test_cached_call_different_kwargs_are_different_keys(db_path, monkeypatch):
    _patch_db_path(monkeypatch, db_path)
    calls = []

    async def fn():
        calls.append(1)
        return {"results": [], "query": "q"}

    await cached_call("news_search", fn, query="acme")
    await cached_call("news_search", fn, query="beta")
    assert len(calls) == 2


# --- empty results are never cached (2026-08-12 cache-poisoning fix) ---


def test_is_empty_result_classification():
    from app.tools.cache import _is_empty_result

    assert _is_empty_result({"results": []})
    assert not _is_empty_result({"results": [{"a": 1}]})
    assert _is_empty_result({"url": "u", "content": ""})
    assert _is_empty_result({"url": "u", "content": "   "})
    assert not _is_empty_result({"url": "u", "content": "body"})
    # A flat record with neither key (e.g. edgar_submissions) is NOT empty — refusing to
    # cache those would defeat the cache for the tools that need it most.
    assert not _is_empty_result({"cik": "123", "name": "ACME"})


async def test_empty_results_are_not_cached(monkeypatch, db_path):
    """Regression: an exhausted ScrapeOps balance produced clean empties that were cached
    permanently — 26/26 linkedin_jobs rows, 54/62 linkedin_lookup, 474/674 news_search — so
    those lookups kept returning nothing long after the cause was fixed."""
    import dataclasses

    import app.db as db_module
    from app.tools.cache import cached_call

    monkeypatch.setattr(
        db_module, "SETTINGS", dataclasses.replace(db_module.SETTINGS, db_path=db_path)
    )
    calls = []

    async def _empty():
        calls.append(1)
        return {"results": []}

    await cached_call("probe_tool", _empty, q="x")
    await cached_call("probe_tool", _empty, q="x")
    assert len(calls) == 2, "an empty result must be retried, not served from cache"


async def test_non_empty_results_are_still_cached(monkeypatch, db_path):
    import dataclasses

    import app.db as db_module
    from app.tools.cache import cached_call

    monkeypatch.setattr(
        db_module, "SETTINGS", dataclasses.replace(db_module.SETTINGS, db_path=db_path)
    )
    calls = []

    async def _full():
        calls.append(1)
        return {"results": [{"a": 1}]}

    first = await cached_call("probe_tool2", _full, q="x")
    second = await cached_call("probe_tool2", _full, q="x")
    assert len(calls) == 1
    assert first["results"] == [{"a": 1}]
    assert second["cache_hit"] is True

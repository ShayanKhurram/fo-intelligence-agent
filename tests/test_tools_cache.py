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

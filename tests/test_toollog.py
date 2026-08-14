"""T35.2 — tool-call capture (PLAN.md T35). Offline: no network, no API keys. Exercises
the `logged` decorator + `tool_log_context` ContextVar binding against a tmp sqlite DB.
"""
from __future__ import annotations

import asyncio
import json

from app.db import connection, get_tool_calls, init_db
from app.toollog import logged, tool_log_context


def _fresh_db(tmp_path) -> str:
    path = str(tmp_path / "toollog.db")
    init_db(path)
    return path


async def test_decorated_tool_writes_one_row_inside_context(tmp_path):
    db_path = _fresh_db(tmp_path)

    @logged("fake_tool")
    async def fake_tool(query: str):
        return {"results": [{"x": 1}, {"x": 2}], "query": query}

    with tool_log_context("ent1", run_id="r1", wave="1", db_path=db_path):
        res = await fake_tool("family office")

    assert res == {"results": [{"x": 1}, {"x": 2}], "query": "family office"}
    with connection(db_path) as conn:
        rows = get_tool_calls(conn, "ent1")
        assert len(rows) == 1
        r = rows[0]
        assert r["tool"] == "fake_tool"
        assert r["entity_id"] == "ent1"
        assert r["run_id"] == "r1"
        assert r["wave"] == "1"
        assert r["ok"] is True
        assert r["duration_ms"] is not None and r["duration_ms"] >= 0
        assert r["result_summary"] == "2 results"
        # result_url holds a URL only — never a search query. The query lives in args.
        assert r["result_url"] is None
        assert r["args"]["query"] == "family office"
        assert r["cache_hit"] is False


async def test_no_context_bound_writes_nothing_and_returns_value(tmp_path):
    db_path = _fresh_db(tmp_path)

    @logged("fake_tool")
    async def fake_tool(query: str):
        return {"results": [], "query": query}

    res = await fake_tool("anything")
    assert res == {"results": [], "query": "anything"}
    with connection(db_path) as conn:
        assert get_tool_calls(conn, "ent1") == []


async def test_raising_tool_records_ok0_and_propagates(tmp_path):
    db_path = _fresh_db(tmp_path)

    @logged("boom_tool")
    async def boom(query: str):
        raise ValueError("kaboom")

    with tool_log_context("ent1", run_id="r1", db_path=db_path):
        try:
            await boom("q")
            assert False, "should have raised"
        except ValueError as exc:
            assert "kaboom" in str(exc)

    with connection(db_path) as conn:
        rows = get_tool_calls(conn, "ent1")
        assert len(rows) == 1
        assert rows[0]["ok"] is False
        assert "ValueError" in rows[0]["error"]
        assert "kaboom" in rows[0]["error"]
        assert rows[0]["result_summary"].startswith("error:")


async def test_error_dict_records_ok0_with_error_string(tmp_path):
    db_path = _fresh_db(tmp_path)

    @logged("err_tool")
    async def err_tool(query: str):
        return {"results": [], "query": query, "error": "boom"}

    with tool_log_context("ent1", run_id="r1", db_path=db_path):
        res = await err_tool("q")

    assert res["error"] == "boom"
    with connection(db_path) as conn:
        rows = get_tool_calls(conn, "ent1")
        assert len(rows) == 1
        assert rows[0]["ok"] is False
        assert rows[0]["error"] == "boom"
        assert rows[0]["result_summary"].startswith("error:")


async def test_cache_hit_recorded(tmp_path):
    db_path = _fresh_db(tmp_path)

    @logged("cached_tool")
    async def cached_tool(query: str):
        return {"results": [1, 2], "query": query, "cache_hit": True}

    with tool_log_context("ent1", run_id="r1", db_path=db_path):
        res = await cached_tool("q")

    assert res["cache_hit"] is True
    with connection(db_path) as conn:
        rows = get_tool_calls(conn, "ent1")
        assert len(rows) == 1
        assert rows[0]["cache_hit"] is True
        # cache_hit does NOT flip ok — a cached answer is still a successful answer
        assert rows[0]["ok"] is True


async def test_args_redacted_and_truncated(tmp_path):
    db_path = _fresh_db(tmp_path)

    @logged("redact_tool")
    async def redact_tool(query: str, api_key: str, token: str, note: str):
        return {"results": [], "query": query}

    big = "x" * 500
    with tool_log_context("ent1", run_id="r1", db_path=db_path):
        await redact_tool("q", api_key="secret123", token="tok456", note=big)

    with connection(db_path) as conn:
        rows = get_tool_calls(conn, "ent1")
        assert len(rows) == 1
        args = rows[0]["args"]  # parsed dict
        assert "api_key" not in args
        assert "token" not in args
        assert "query" in args
        assert args["query"] == "q"
        # the 500-char string arg is truncated to 200
        assert "note" in args
        assert len(args["note"]) == 200


async def test_concurrent_tasks_no_cross_attribution(tmp_path):
    db_path = _fresh_db(tmp_path)

    @logged("fake_tool")
    async def fake_tool(entity_id: str):
        await asyncio.sleep(0)  # yield so tasks interleave
        return {"results": [1], "query": entity_id}

    async def run_one(eid: str):
        with tool_log_context(eid, run_id="r1", wave="1", db_path=db_path):
            await fake_tool(eid)

    await asyncio.gather(run_one("entA"), run_one("entB"))

    with connection(db_path) as conn:
        a = get_tool_calls(conn, "entA")
        b = get_tool_calls(conn, "entB")
        assert len(a) == 1 and len(b) == 1
        assert a[0]["entity_id"] == "entA"
        assert b[0]["entity_id"] == "entB"
        # entA has no entB row and vice versa
        assert all(r["entity_id"] == "entA" for r in a)
        assert all(r["entity_id"] == "entB" for r in b)


async def test_none_return_is_logged_as_failure(tmp_path):
    """A None return means the call failed (e.g. fetch_raw_html on an httpx.HTTPError).
    It must be recorded ok=0 with an error string, not as a successful 'empty' result —
    an outage that looks like an absence is exactly the confusion this log exists to
    prevent. The wrapper still returns None to the caller unchanged."""
    db_path = _fresh_db(tmp_path)

    @logged("fetch_raw_html")
    async def fetch_raw_html(url: str):
        return None  # the real one returns None on a refused/failed GET

    with tool_log_context("ent1", run_id="r1", db_path=db_path):
        res = await fetch_raw_html("http://127.0.0.1:9/nope")

    assert res is None
    with connection(db_path) as conn:
        rows = get_tool_calls(conn, "ent1")
        assert len(rows) == 1
        r = rows[0]
        assert r["ok"] is False
        assert r["error"] == "returned None (call failed)"
        assert r["result_summary"] == "error: returned None (call failed)"
        # result_url still records the URL the call was directed at
        assert r["result_url"] == "http://127.0.0.1:9/nope"


async def test_non_serialisable_arg_is_dropped_not_stringified(tmp_path):
    """A callable argument (e.g. fetch_page_free_first's `fallback`) is not
    JSON-serialisable and must be dropped from the persisted args, not stored as a
    "<function ... at 0x...>" repr that pretends to be data."""
    db_path = _fresh_db(tmp_path)

    async def fallback(url: str):
        return {"url": url, "content": ""}

    @logged("fetch_page_free_first")
    async def fetch_page_free_first(url: str, fallback=None):
        return {"url": url, "content": "ok", "extraction_method": "stub"}

    with tool_log_context("ent1", run_id="r1", db_path=db_path):
        res = await fetch_page_free_first("http://x", fallback=fallback)

    assert res["content"] == "ok"
    with connection(db_path) as conn:
        rows = get_tool_calls(conn, "ent1")
        assert len(rows) == 1
        args = rows[0]["args"]
        assert "fallback" not in args
        assert args["url"] == "http://x"
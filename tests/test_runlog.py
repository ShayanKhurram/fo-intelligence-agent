"""T35.1 — run identity (PLAN.md T35). `run_batch` opens a `layer1` run, closes it
done/failed in a try/finally; the helpers in app.db persist + read it back. Offline —
no network, no API keys (run_lead is monkeypatched the way test_runner.py does it)."""
from __future__ import annotations

import asyncio

from app.db import connection, get_run, list_runs, upsert_entity
from app.runner import BatchResult, run_batch


async def _stub_lead_ok(entity_id, db_path=None):
    return {"cost_usd": 0.0}


async def _stub_lead_raise(entity_id, db_path=None):
    raise RuntimeError("simulated crash")


async def test_run_batch_writes_one_done_run(db_path, monkeypatch):
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Acme FO")

    import app.runner as runner_mod
    monkeypatch.setattr(runner_mod, "run_lead", _stub_lead_ok)

    result: BatchResult = await run_batch(
        entity_ids=["E1"], db_path=db_path, resume=False, skip_preflight=True
    )

    assert result.run_id is not None
    with connection(db_path) as conn:
        rows = list_runs(conn)
        assert len(rows) == 1
        run = rows[0]
        assert run["run_id"] == result.run_id
        assert run["kind"] == "layer1"
        assert run["status"] == "done"
        assert run["ended_at"] is not None
        assert run["entity_count"] == 1
        # notes carry the outcome counters
        assert run["notes"]["processed"] == 1
        assert run["notes"]["failed"] == 0
        assert run["notes"]["retried"] == 0
        assert run["notes"]["budget_aborted"] is False
        # get_run returns the same row, notes parsed
        again = get_run(conn, result.run_id)
        assert again is not None
        assert again["status"] == "done"
        assert again["notes"] == run["notes"]


async def test_per_lead_crash_does_not_fail_the_run(db_path, monkeypatch):
    """A per-lead crash is caught inside process_one and must not fail the run — the run
    ends 'done' with the lead counted in notes.failed, never left 'running'."""
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Acme FO")

    import app.runner as runner_mod
    monkeypatch.setattr(runner_mod, "run_lead", _stub_lead_raise)
    result = await run_batch(
        entity_ids=["E1"], db_path=db_path, resume=False, skip_preflight=True
    )
    # Per-lead crash is caught -> run is 'done', the lead is in result.failed.
    with connection(db_path) as conn:
        run = get_run(conn, result.run_id)
        assert run["status"] == "done"
        assert run["notes"]["failed"] == 1
        # never left running
        assert run["ended_at"] is not None


async def test_run_batch_run_failed_on_unhandled_exception(db_path, monkeypatch):
    """An exception that escapes gather (not a per-lead crash) must close the run as
    failed, never leave it 'running'."""
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Acme FO")

    import app.runner as runner_mod
    # Force an unhandled exception that escapes gather: patch asyncio.gather (as used
    # by run_batch) to raise, closing any coroutines it was handed so we don't leak
    # 'coroutine never awaited' warnings.
    real_gather = asyncio.gather

    def boom_gather(*aws):
        for a in aws:
            close = getattr(a, "close", None)
            if callable(close):
                close()
        raise RuntimeError("gather blew up")

    monkeypatch.setattr(asyncio, "gather", boom_gather)
    try:
        await run_batch(
            entity_ids=["E1"], db_path=db_path, resume=False, skip_preflight=True
        )
        assert False, "should have raised"
    except RuntimeError:
        pass
    finally:
        monkeypatch.setattr(asyncio, "gather", real_gather)

    with connection(db_path) as conn:
        rows = list_runs(conn)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert rows[0]["ended_at"] is not None


async def test_list_runs_newest_first(db_path):
    with connection(db_path) as conn:
        # Three runs with distinct started_at: sqlite defaults started_at to 'now' on
        # insert, so successive inserts are ordered by their real insert time. Insert
        # them with explicit run_ids and small sleeps to guarantee ordering.
        from app.db import start_run, finish_run
        r1 = start_run(conn, "layer1", entity_count=1)
    await asyncio.sleep(0.01)
    with connection(db_path) as conn:
        r2 = start_run(conn, "layer1", entity_count=2)
    await asyncio.sleep(0.01)
    with connection(db_path) as conn:
        r3 = start_run(conn, "layer1", entity_count=3)
    with connection(db_path) as conn:
        rows = list_runs(conn)
        assert [r["run_id"] for r in rows] == [r3, r2, r1]


async def test_get_run_unknown_returns_none(db_path):
    with connection(db_path) as conn:
        assert get_run(conn, "does-not-exist") is None
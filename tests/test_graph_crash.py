"""Crash-persistence tests — added per DEBUG_PLAN.md: a lead that crashes mid-graph used
to leave NO trace (the trace is only written by the Verdict node, which a crashed lead
never reaches) AND an empty `last_error` in its checkpoint row (because `str(exc)` is `''`
for httpcore.ReadError). These tests pin the two fixes:

1. `app/graph.run_lead` streams the graph (astream, stream_mode="values") so the last
   completed state is always in hand if a later node raises; on exception it writes a
   partial trace ending in a `lead_crash` event, then re-raises.
2. `app/runner.process_one` records `last_error` as `f"{type(exc).__name__}: {exc}"`
   so the exception class is always present even when `str(exc)` is empty.
"""
from __future__ import annotations

import pytest

from app.db import connection, get_checkpoint, get_lead_trace, upsert_entity
from app.graph import run_lead
from app.runner import run_batch


async def _patch_supervisor_to_crash(monkeypatch, message: str = "distinctive crash marker") -> None:
    """Replace `app.graph.supervisor_node` with an async node that always raises.

    `app.graph` binds `supervisor_node` into its own namespace at import time
    (`from app.supervisor import ... supervisor_node ...`), so the patch must target
    `app.graph.supervisor_node` — patching `app.supervisor.supervisor_node` would not
    affect the reference `build_graph` already captured."""
    import app.graph as graph_mod

    async def boom(state):
        raise RuntimeError(message)

    monkeypatch.setattr(graph_mod, "supervisor_node", boom)


async def test_run_lead_persists_partial_trace_on_crash(db_path, fake_model, monkeypatch):
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Acme FO")

    await _patch_supervisor_to_crash(monkeypatch, "distinctive crash marker")

    with pytest.raises(RuntimeError, match="distinctive crash marker"):
        await run_lead("E1", db_path=db_path)

    with connection(db_path) as conn:
        trace = get_lead_trace(conn, "E1")
    assert trace is not None, "expected a partial trace to be persisted at crash time"
    last = trace[-1]
    assert last["event"] == "lead_crash"
    assert "distinctive crash marker" in last["error"]
    assert "RuntimeError" in last["error"]


async def test_run_batch_crash_records_nonempty_last_error_with_type_name(
    db_path, fake_model, monkeypatch
):
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Acme FO")

    await _patch_supervisor_to_crash(monkeypatch, "distinctive crash marker")

    result = await run_batch(
        entity_ids=["E1"], db_path=db_path, resume=False, skip_preflight=True
    )
    assert result.failed == ["E1"]

    with connection(db_path) as conn:
        cp = get_checkpoint(conn, "E1")
    assert cp is not None
    assert cp["status"] == "failed"
    assert cp["last_error"], "last_error must be non-empty even when str(exc) is ''"
    assert "RuntimeError" in cp["last_error"]
    assert "distinctive crash marker" in cp["last_error"]

    # The crash trace should also have been persisted (run_lead writes it before re-raising).
    with connection(db_path) as conn:
        trace = get_lead_trace(conn, "E1")
    assert trace is not None
    assert trace[-1]["event"] == "lead_crash"
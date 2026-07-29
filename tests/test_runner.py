from __future__ import annotations

from langchain_core.messages import AIMessage, SystemMessage

from app.db import connection, get_resumable_leads, upsert_checkpoint, upsert_entity
from app.runner import BatchResult, run_batch


def _sys_contains(text: str):
    def pred(messages):
        return any(isinstance(m, SystemMessage) and text in str(m.content) for m in messages)

    return pred


async def test_run_batch_processes_all_leads_and_checkpoints(db_path, fake_model):
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Acme FO")
        upsert_entity(conn, "E2", "Beta FO")

    fake_model.route(
        _sys_contains("You are the supervisor"),
        AIMessage(content="", tool_calls=[{"id": "1", "name": "research_complete", "args": {}}]),
        AIMessage(content="", tool_calls=[{"id": "2", "name": "research_complete", "args": {}}]),
    )

    result: BatchResult = await run_batch(
        entity_ids=["E1", "E2"], db_path=db_path, resume=False, skip_preflight=True
    )

    assert set(result.processed) == {"E1", "E2"}
    assert result.failed == []
    with connection(db_path) as conn:
        rows = {r["entity_id"]: r["status"] for r in conn.execute("SELECT entity_id, status FROM lead_checkpoints")}
    assert rows == {"E1": "verdict_done", "E2": "verdict_done"}


async def test_run_batch_skips_lead_at_max_attempts(db_path, fake_model):
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Acme FO")
        upsert_checkpoint(conn, "E1", status="failed", attempts=2)  # == default max_lead_attempts

    result = await run_batch(entity_ids=["E1"], db_path=db_path, resume=False, skip_preflight=True)
    assert result.failed == ["E1"]
    assert fake_model.calls == []  # never even ran the graph


async def test_run_batch_marks_crashed_lead_failed_not_stalled(db_path, fake_model, monkeypatch):
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Acme FO")
        upsert_entity(conn, "E2", "Beta FO")

    async def boom(entity_id, db_path=None):
        if entity_id == "E1":
            raise RuntimeError("simulated crash")
        return {"cost_usd": 0.0, "verdict": "reject"}

    import app.runner as runner_mod

    monkeypatch.setattr(runner_mod, "run_lead", boom)
    result = await run_batch(entity_ids=["E1", "E2"], db_path=db_path, resume=False, skip_preflight=True)
    assert result.failed == ["E1"]
    assert result.processed == ["E2"]


async def test_resume_picks_up_running_and_failed_leads(db_path):
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Acme FO")
        upsert_checkpoint(conn, "E1", status="running", attempts=1)
        resumable = get_resumable_leads(conn)
    assert resumable == ["E1"]

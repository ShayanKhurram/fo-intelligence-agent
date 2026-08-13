"""A timed-out lead must be re-queued, not rejected.

Regression corpus from the 30-lead batch of 2026-08-14: 11 leads were rejected
`G1.Q1:unanswered;G1.Q3:unanswered` after all three lanes hit the 240s cap with
`claim_count: 0` — BILTMORE FAMILY OFFICE (CRD 167174, $3.47B RAUM) among them, whose
recorded finding was that the pipeline could not establish it exists. The rejection cost
~$0 and looked identical to a considered verdict.
"""
from __future__ import annotations

import pytest

from app.db import connection, get_checkpoint, get_resumable_leads, upsert_checkpoint, upsert_entity
from app.state import new_supervisor_state, trace_event
from app.verdict import (
    _rejected_only_because_research_never_ran,
    _timed_out_lanes,
    persist_verdict,
    run_verdict,
)

_UNANSWERED = "G1.Q1:unanswered;G1.Q3:unanswered"


def _state(*, timed_out=("identity_and_type", "people", "activity_signals"), claims=None):
    st = new_supervisor_state("e1")
    st["claims"] = list(claims or [])
    st["trace"] = [
        trace_event("researcher", "lane_timeout", lane=lane, timeout_seconds=240)
        for lane in timed_out
    ]
    return st


def _gates(reason_code=_UNANSWERED):
    return {"reject": True, "reason_code": reason_code, "force_low": False, "checks": {}, "labels": []}


def test_timed_out_lanes_read_from_the_trace():
    assert _timed_out_lanes(_state()) == ["identity_and_type", "people", "activity_signals"]
    assert _timed_out_lanes(_state(timed_out=())) == []


def test_retry_when_every_lane_timed_out_and_nothing_was_answered():
    assert _rejected_only_because_research_never_ran(_state(), _gates()) is True


def test_no_retry_when_no_lane_timed_out():
    """A lead researched properly and found wanting must still be rejected."""
    assert _rejected_only_because_research_never_ran(_state(timed_out=()), _gates()) is False


def test_no_retry_when_a_lane_produced_claims():
    """A lane that timed out AFTER answering did real work; that evidence stands."""
    claim = {"question_id": "G1.Q1", "answer": "exists", "status": "confirmed",
             "confidence": "high", "produced_by": "research"}
    assert _rejected_only_because_research_never_ran(_state(claims=[claim]), _gates()) is False


def test_no_retry_when_a_reason_is_substantive():
    """A timeout elsewhere must not rescue a lead rejected on a real finding."""
    gates = _gates("G1.Q3:contradicted")
    assert _rejected_only_because_research_never_ran(_state(), gates) is False
    mixed = _gates("G1.Q1:unanswered;V5_firm_is_fo:G1.Q3")
    assert _rejected_only_because_research_never_ran(_state(), mixed) is False


@pytest.mark.asyncio
async def test_run_verdict_returns_retry_instead_of_reject(monkeypatch):
    import app.verdict as v

    monkeypatch.setattr(v, "evaluate_hard_gates", lambda claims: _gates())
    out = await run_verdict(_state())
    assert out["verdict"] is None
    assert out["retry_reason"] == "lane_timeout"
    assert any(e.get("event") == "retry_research_incomplete" for e in out["trace"])


@pytest.mark.asyncio
async def test_persist_writes_no_decision_and_no_rejection_on_retry(db_path, monkeypatch):
    import app.verdict as v

    monkeypatch.setattr(v, "evaluate_hard_gates", lambda claims: _gates())
    st = _state()
    result = await run_verdict(st)
    merged = {**st, **result, "trace": st["trace"] + result["trace"]}
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "BILTMORE FAMILY OFFICE, LLC")
        persist_verdict(conn, merged)
        assert conn.execute("SELECT COUNT(*) FROM rejections WHERE entity_id='e1'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM decisions WHERE entity_id='e1'").fetchone()[0] == 0
        # the evidence of WHY it is retryable is still persisted
        assert conn.execute("SELECT COUNT(*) FROM lead_traces WHERE entity_id='e1'").fetchone()[0] == 1


def test_retry_status_is_resumable(db_path):
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "BILTMORE FAMILY OFFICE, LLC")
        upsert_checkpoint(conn, "e1", status="retry", attempts=1)
        assert "e1" in get_resumable_leads(conn)
        assert get_checkpoint(conn, "e1")["status"] == "retry"


def test_verdict_done_is_not_resumable(db_path):
    with connection(db_path) as conn:
        upsert_entity(conn, "e2", "SOMEBODY ELSE")
        upsert_checkpoint(conn, "e2", status="verdict_done", attempts=1)
        assert "e2" not in get_resumable_leads(conn)

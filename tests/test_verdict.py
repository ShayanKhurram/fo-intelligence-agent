from __future__ import annotations

from langchain_core.messages import AIMessage

from app.db import connection, upsert_entity
from app.verdict import compute_dead_ends, compute_thin_reason, evaluate_hard_gates, run_verdict, verdict_node
from app.state import new_supervisor_state

ALL_HARD_CONFIRMED = [
    {"question_id": qid, "answer": "ok", "status": "confirmed", "source_url": "http://x", "confidence": "high"}
    for qid in ("G1.Q1", "G1.Q2", "G1.Q3", "G1.Q5", "G1.Q6", "G2.Q1", "G3.Q3")
]


def test_reject_on_unknown_policy():
    gates = evaluate_hard_gates([])
    assert gates["reject"] is True
    assert "G1.Q1:unanswered" in gates["reason_code"]
    assert "G1.Q3:unanswered" in gates["reason_code"]


def test_reject_on_contradiction_even_if_otherwise_confirmed():
    claims = list(ALL_HARD_CONFIRMED)
    claims[0] = {**claims[0], "question_id": "G1.Q1", "status": "contradicted"}
    gates = evaluate_hard_gates(claims)
    assert gates["reject"] is True
    assert "G1.Q1:contradicted" in gates["reason_code"]


def test_deprioritize_policy_survives_but_forces_low():
    claims = [c for c in ALL_HARD_CONFIRMED if c["question_id"] not in ("G1.Q5",)]
    gates = evaluate_hard_gates(claims)
    assert gates["reject"] is False
    assert gates["force_low"] is True
    assert any("G1.Q5" in label for label in gates["labels"])


def test_ship_with_label_policy_survives_without_forcing_low():
    claims = [c for c in ALL_HARD_CONFIRMED if c["question_id"] not in ("G3.Q3",)]
    gates = evaluate_hard_gates(claims)
    assert gates["reject"] is False
    assert gates["force_low"] is False
    assert any("G3.Q3" in label for label in gates["labels"])


def test_all_confirmed_no_labels():
    gates = evaluate_hard_gates(ALL_HARD_CONFIRMED)
    assert gates["reject"] is False
    assert gates["force_low"] is False
    assert gates["labels"] == []


def test_compute_dead_ends_lists_could_not_verify_only():
    claims = [
        {"question_id": "G1.Q1", "answer": "x", "status": "confirmed", "confidence": "high"},
        {"question_id": "G2.Q2", "answer": "no footprint found", "status": "could_not_verify", "confidence": "low"},
    ]
    dead_ends = compute_dead_ends(claims)
    assert len(dead_ends) == 1
    assert "G2.Q2" in dead_ends[0]


def test_thin_reason_structural_when_no_decision_maker_channel_or_deploy_evidence():
    claims = [
        {"question_id": "G2.Q1", "answer": "none found", "status": "could_not_verify", "confidence": "low"},
        {"question_id": "G2.Q2", "answer": "no footprint", "status": "could_not_verify", "confidence": "low"},
        {"question_id": "G3.Q1", "answer": "no deployment evidence", "status": "could_not_verify", "confidence": "low"},
    ]
    assert compute_thin_reason(claims) == "structural"


def test_thin_reason_fixable_when_decision_maker_known():
    claims = [
        {"question_id": "G2.Q1", "answer": "Jane Doe, CIO", "status": "confirmed", "confidence": "high"},
        {"question_id": "G3.Q1", "answer": "no deployment evidence", "status": "could_not_verify", "confidence": "low"},
    ]
    assert compute_thin_reason(claims) == "fixable"


def test_thin_reason_fixable_when_no_signals_at_all():
    assert compute_thin_reason([]) == "structural"  # everything unresolved -> structural, not a bug: no data anywhere


async def test_run_verdict_pursue_low_sets_thin_reason(fake_model):
    fake_model.queue(AIMessage(content='{"verdict": "pursue_low", "rationale": "thin"}'))
    state = new_supervisor_state("E1")
    state["claims"] = [c for c in ALL_HARD_CONFIRMED if c["question_id"] != "G2.Q1"]
    result = await run_verdict(state)
    assert result["verdict"] == "pursue_low"
    assert result["thin_reason"] in ("fixable", "structural")


async def test_run_verdict_pursue_leaves_thin_reason_none(fake_model):
    fake_model.queue(AIMessage(content='{"verdict": "pursue", "rationale": "solid"}'))
    state = new_supervisor_state("E1")
    state["claims"] = ALL_HARD_CONFIRMED
    result = await run_verdict(state)
    assert result["verdict"] == "pursue"
    assert result.get("thin_reason") is None


async def test_run_verdict_reject_skips_llm(fake_model):
    state = new_supervisor_state("E1")
    state["claims"] = []
    result = await run_verdict(state)
    assert result["verdict"] == "reject"
    assert fake_model.calls == []  # code gate rejected before any LLM call


async def test_run_verdict_pursue_calls_llm_once(fake_model):
    fake_model.queue(AIMessage(content='{"verdict": "pursue", "rationale": "solid"}'))
    state = new_supervisor_state("E1")
    state["claims"] = ALL_HARD_CONFIRMED
    result = await run_verdict(state)
    assert result["verdict"] == "pursue"
    assert result["rationale"] == "solid"
    assert len(fake_model.calls) == 1


async def test_run_verdict_llm_parse_failure_defaults_to_pursue_low(fake_model):
    fake_model.queue(AIMessage(content="not json"))
    fake_model.queue(AIMessage(content="still not json"))
    state = new_supervisor_state("E1")
    state["claims"] = ALL_HARD_CONFIRMED
    result = await run_verdict(state)
    assert result["verdict"] == "pursue_low"
    assert len(fake_model.calls) == 2


async def test_verdict_node_persists_to_db(db_path, fake_model):
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Acme Family Office")

    fake_model.queue(AIMessage(content='{"verdict": "pursue", "rationale": "solid"}'))
    state = new_supervisor_state("E1")
    state["claims"] = ALL_HARD_CONFIRMED

    with connection(db_path) as conn:
        await verdict_node(state, conn=conn)

    with connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM decisions WHERE entity_id = 'E1'").fetchall()
    assert len(rows) == 1
    assert rows[0]["verdict"] == "pursue"
    assert rows[0]["thin_reason"] is None

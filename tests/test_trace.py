"""Tests for the full reasoning/tool-call trace — added because none of a lead's
internal reasoning was persisted anywhere before this (see PROJECT_LOG.md)."""
from __future__ import annotations

from langchain_core.messages import AIMessage

from app.db import connection, get_lead_trace, upsert_entity
from app.researcher import run_researcher_lane
from app.state import LeadBrief, LeadBudget, new_supervisor_state
from app.supervisor import supervisor_tools_node
from app.trace_viewer import format_trace
from app.verdict import verdict_node


async def test_researcher_lane_emits_trace_events(fake_model):
    fake_model.queue(AIMessage(content="", tool_calls=[{"id": "1", "name": "think_tool", "args": {"reflection": "checking"}}]))
    fake_model.queue(AIMessage(content="done", tool_calls=[]))
    fake_model.queue(AIMessage(content='[{"question_id":"G1.Q1","answer":"exists","status":"confirmed","source_url":"http://x","source_class":"web","confidence":"high"}]'))
    result = await run_researcher_lane("identity_and_type", "confirm existence", {"canonical_name": "Acme", "aliases": [], "injected_facts": {}})
    kinds = [(e["phase"], e["event"]) for e in result["trace"]]
    assert ("researcher", "ai_message") in kinds
    assert ("researcher_tool", "tool_call") in kinds
    assert ("compress", "compress_output") in kinds


async def test_researcher_lane_timeout_emits_trace_event(monkeypatch):
    import asyncio

    import app.researcher as researcher_mod

    async def hang(*args, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(researcher_mod.asyncio, "wait_for", hang)
    result = await run_researcher_lane("people", "find someone", {"canonical_name": "Acme", "aliases": [], "injected_facts": {}})
    assert result["lane_status"] == "capped"
    assert result["trace"][0]["event"] == "lane_timeout"


async def test_supervisor_tools_emits_think_and_dispatch_skipped_trace():
    state = new_supervisor_state("E1")
    state["lead_brief"] = LeadBrief(entity_id="E1", canonical_name="Acme", budget=LeadBudget())
    state["lanes_dispatched"] = {"identity_and_type": 2}  # at cap
    state["supervisor_messages"] = [
        AIMessage(
            content="",
            tool_calls=[
                {"id": "1", "name": "think_tool", "args": {"reflection": "hi"}},
                {"id": "2", "name": "conduct_research", "args": {"lane": "identity_and_type", "instructions": "x"}},
            ],
        )
    ]
    delta = await supervisor_tools_node(state)
    kinds = [e["event"] for e in delta["trace"]]
    assert "think" in kinds
    assert "dispatch_skipped" in kinds


async def test_verdict_trace_persisted_to_db(db_path, fake_model):
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Acme Family Office")

    fake_model.queue(AIMessage(content='{"verdict": "pursue", "rationale": "solid"}'))
    state = new_supervisor_state("E1")
    state["claims"] = [
        {"question_id": qid, "answer": "ok", "status": "confirmed", "confidence": "high"}
        for qid in ("G1.Q1", "G1.Q2", "G1.Q3", "G1.Q5", "G1.Q6", "G2.Q1", "G3.Q3")
    ]

    with connection(db_path) as conn:
        await verdict_node(state, conn=conn)

    with connection(db_path) as conn:
        trace = get_lead_trace(conn, "E1")
    assert trace is not None
    assert trace[-1]["event"] == "verdict_output"
    assert trace[-1]["final_verdict"] == "pursue"


def test_format_trace_does_not_crash_on_all_event_kinds():
    trace = [
        {"phase": "supervisor", "event": "ai_message", "iteration": 1, "content": "thinking", "tool_calls": [{"name": "think_tool", "args": {"reflection": "x"}}]},
        {"phase": "supervisor", "event": "think", "reflection": "hi"},
        {"phase": "supervisor", "event": "dispatch", "lane": "people", "instructions": "find someone"},
        {"phase": "supervisor", "event": "dispatch_skipped", "lane": "people", "already_dispatched": 2},
        {"phase": "researcher_tool", "event": "tool_call", "lane": "people", "tool": "web_search", "args": {"query": "x"}, "result": {"results": []}},
        {"phase": "researcher_tool", "event": "tool_call", "lane": "people", "tool": "web_search", "args": {"query": "x"}, "result": {"error": "boom"}},
        {"phase": "compress", "event": "compress_output", "lane": "people", "attempt": 0, "claims": [{"question_id": "G2.Q1", "status": "confirmed", "confidence": "high", "answer": "ok"}]},
        {"phase": "compress", "event": "compress_parse_failed", "lane": "people"},
        {"phase": "supervisor", "event": "lane_complete", "lane": "people", "lane_status": "ok", "claim_count": 1},
        {"phase": "researcher", "event": "lane_timeout", "lane": "people", "timeout_seconds": 240},
        {"phase": "researcher", "event": "lane_crash", "lane": "people", "error": "boom"},
        {"phase": "supervisor", "event": "research_complete_blocked", "unanswered": ["G1.Q1"]},
        {"phase": "supervisor", "event": "research_complete_accepted", "budget_exhausted": True},
        {"phase": "verdict", "event": "code_gate_reject", "reason_code": "G1.Q1:unanswered"},
        {"phase": "verdict", "event": "verdict_output", "llm_verdict": "pursue", "final_verdict": "pursue", "force_low": False, "rationale": "good"},
        {"phase": "mystery", "event": "something_unhandled", "foo": "bar"},
    ]
    output = format_trace("E1", "Acme", trace)
    assert "E1" in output
    assert "Acme" in output
    assert "16 events" in output

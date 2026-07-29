"""Full graph smoke tests: Parser -> supervisor loop -> Verdict -> DB persistence.

The three researcher lanes run genuinely concurrently (asyncio.gather inside
supervisor_tools_node), so a flat FIFO response queue can't deterministically predict
which lane consumes which queued reply. These tests use FakeChatModel.route() —
content-addressed responses keyed off each call's system prompt — so the test stays
correct regardless of scheduling order. See app/llm.py's FakeChatModel docstring."""
from __future__ import annotations

from langchain_core.messages import AIMessage, SystemMessage

from app.db import add_entity_source, connection, upsert_entity
from app.graph import build_graph
from app.state import new_supervisor_state


def _sys_contains(text: str):
    def pred(messages):
        return any(isinstance(m, SystemMessage) and text in str(m.content) for m in messages)

    return pred


def _sys_contains_all(*texts: str):
    def pred(messages):
        return any(isinstance(m, SystemMessage) and all(t in str(m.content) for t in texts) for m in messages)

    return pred


async def test_full_graph_pursue_path(db_path, fake_model):
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Acme Family Office", aliases=["Acme FO"])
        add_entity_source(conn, "E1", "adv_index", {"present": True, "client_count": 1})

    fake_model.route(
        _sys_contains("You are the supervisor"),
        AIMessage(
            content="",
            tool_calls=[
                {"id": "s1a", "name": "think_tool", "args": {"reflection": "dispatch everything"}},
                {"id": "s1b", "name": "conduct_research", "args": {"lane": "identity_and_type", "instructions": "confirm existence"}},
                {"id": "s1c", "name": "conduct_research", "args": {"lane": "people", "instructions": "find decision maker"}},
                {"id": "s1d", "name": "conduct_research", "args": {"lane": "activity_signals", "instructions": "scandal check"}},
            ],
        ),
        AIMessage(content="", tool_calls=[{"id": "s2a", "name": "research_complete", "args": {}}]),
    )
    fake_model.route(_sys_contains("You are the identity_and_type researcher"), AIMessage(content="done", tool_calls=[]))
    fake_model.route(
        _sys_contains_all("compress a researcher", "G1.Q1"),
        AIMessage(
            content='['
            '{"question_id":"G1.Q1","answer":"exists","status":"confirmed","source_url":"http://x","source_class":"web","confidence":"high"},'
            '{"question_id":"G1.Q2","answer":"Acme Family Office LLC","status":"confirmed","source_url":"http://x","source_class":"web","confidence":"high"},'
            '{"question_id":"G1.Q3","answer":"affirmative FO evidence","status":"confirmed","source_url":"http://x","source_class":"web","confidence":"high"},'
            '{"question_id":"G1.Q5","answer":"not RIA-in-costume","status":"confirmed","source_url":"http://x","source_class":"web","confidence":"medium"},'
            '{"question_id":"G1.Q6","answer":"active","status":"confirmed","source_url":"http://x","source_class":"web","confidence":"medium"}'
            "]"
        ),
    )
    fake_model.route(_sys_contains("You are the people researcher"), AIMessage(content="no leads found", tool_calls=[]))
    fake_model.route(
        _sys_contains_all("compress a researcher", "G2.Q1"),
        AIMessage(content='[{"question_id":"G2.Q1","answer":"Jane Doe, CIO","status":"confirmed","source_url":"http://x","source_class":"web","confidence":"medium"}]'),
    )
    fake_model.route(_sys_contains("You are the activity_signals researcher"), AIMessage(content="no leads", tool_calls=[]))
    fake_model.route(
        _sys_contains_all("compress a researcher", "G3.Q1"),
        AIMessage(content='[{"question_id":"G3.Q3","answer":"clean","status":"confirmed","source_url":"http://x","source_class":"news","confidence":"medium"}]'),
    )
    fake_model.route(
        _sys_contains("final judgment pass"),
        AIMessage(content='{"verdict": "pursue", "rationale": "clears all hard gates with solid soft-gate evidence"}'),
    )

    graph = build_graph(db_path)
    result = await graph.ainvoke(new_supervisor_state("E1"))

    assert result["verdict"] == "pursue"
    assert result["gate_results"]["reject"] is False
    assert result["dead_ends"] == []
    claim_ids = {c["question_id"] for c in result["claims"]}
    assert {"G1.Q1", "G1.Q2", "G1.Q3", "G1.Q5", "G1.Q6", "G2.Q1", "G3.Q3", "G1.Q4"}.issubset(claim_ids)

    with connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM decisions WHERE entity_id = 'E1'").fetchall()
    assert len(rows) == 1
    assert rows[0]["verdict"] == "pursue"


async def test_full_graph_reject_path_never_calls_verdict_llm(db_path, fake_model):
    """No entity_sources rows -> Parser attaches no pre-answered claims -> the supervisor's
    first research_complete request should be blocked (HARD gates unanswered), then once
    budget exhausts (max_iterations default=2) Verdict rejects on code gates alone."""
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Ghost Entity")

    fake_model.route(
        _sys_contains("You are the supervisor"),
        AIMessage(content="", tool_calls=[{"id": "1", "name": "research_complete", "args": {}}]),
        AIMessage(content="", tool_calls=[{"id": "2", "name": "research_complete", "args": {}}]),
    )

    graph = build_graph(db_path)
    result = await graph.ainvoke(new_supervisor_state("E1"))

    assert result["verdict"] == "reject"
    with connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM rejections WHERE entity_id = 'E1'").fetchall()
    assert len(rows) == 1

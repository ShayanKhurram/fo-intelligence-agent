from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage

from app.questions import QUESTIONS_BY_LANE
from app.researcher import (
    _lane_system_prompt,
    compress_to_claims_node,
    researcher_tools_node,
    run_researcher_lane,
)


def _researcher_state(**overrides):
    base = {
        "lane": "identity_and_type",
        "instructions": "",
        "lead_brief_slim": {"canonical_name": "Acme", "aliases": [], "injected_facts": {}},
        "researcher_messages": [],
        "raw_notes": [],
        "tool_calls_used": 0,
        "had_real_evidence": False,
        "claims": [],
        "lane_status": "ok",
        "cost_usd": 0.0,
        "trace": [],
    }
    base.update(overrides)
    return base


async def test_researcher_lane_happy_path(fake_model):
    fake_model.queue(AIMessage(content="", tool_calls=[{"id": "1", "name": "think_tool", "args": {"reflection": "checking"}}]))
    fake_model.queue(AIMessage(content="done", tool_calls=[]))
    fake_model.queue(
        AIMessage(
            content='[{"question_id":"G1.Q1","answer":"exists","status":"confirmed",'
            '"source_url":"http://x","source_class":"web","confidence":"high"}]'
        )
    )
    result = await run_researcher_lane(
        "identity_and_type", "confirm existence", {"canonical_name": "Acme", "aliases": [], "injected_facts": {}}
    )
    assert result["lane_status"] == "ok"
    assert result["tool_calls_used"] == 1
    assert len(result["claims"]) == 1
    assert result["claims"][0]["question_id"] == "G1.Q1"


async def test_researcher_lane_respects_tool_call_cap(fake_model, monkeypatch):
    import dataclasses

    import app.researcher as researcher_mod

    capped = dataclasses.replace(researcher_mod.SETTINGS.researcher, max_react_tool_calls=1)
    monkeypatch.setattr(
        researcher_mod, "SETTINGS", dataclasses.replace(researcher_mod.SETTINGS, researcher=capped)
    )
    # researcher keeps wanting to call think_tool; cap should force it to compress after 1
    for _ in range(3):
        fake_model.queue(AIMessage(content="", tool_calls=[{"id": "x", "name": "think_tool", "args": {"reflection": "again"}}]))
    fake_model.queue(AIMessage(content="[]"))
    result = await run_researcher_lane("people", "find someone", {"canonical_name": "Acme", "aliases": [], "injected_facts": {}})
    assert result["tool_calls_used"] == 1
    assert result["lane_status"] == "ok"


async def test_researcher_lane_compress_parse_failure_falls_back(fake_model):
    fake_model.queue(AIMessage(content="done", tool_calls=[]))
    fake_model.queue(AIMessage(content="not valid json"))
    fake_model.queue(AIMessage(content="still not valid json"))
    result = await run_researcher_lane(
        "activity_signals", "check activity", {"canonical_name": "Acme", "aliases": [], "injected_facts": {}}
    )
    assert result["lane_status"] == "failed"
    assert all(c["status"] == "could_not_verify" for c in result["claims"])
    assert all(c["source_class"] == "uncompressed_notes" for c in result["claims"])
    assert any("UNCOMPRESSED FALLBACK" in n for n in result["raw_notes"])
    # The real parse-failure reason must be legible in both the fallback claim answer and
    # the compress_parse_failed trace event's `error` field — not the old generic-only
    # string. "not valid json" triggers a json.JSONDecodeError, whose type name must appear.
    for c in result["claims"]:
        assert "JSONDecodeError" in c["answer"], c["answer"]
        assert "Expecting" in c["answer"], c["answer"]
    parse_failed_events = [e for e in result["trace"] if e.get("event") == "compress_parse_failed"]
    assert len(parse_failed_events) == 1
    err = parse_failed_events[0].get("error", "")
    assert "JSONDecodeError" in err, err
    assert "Expecting" in err, err


async def test_researcher_lane_ignores_unknown_tool_name_gracefully(fake_model):
    # a tool call for a name not in this lane's toolset must degrade, not crash
    fake_model.queue(AIMessage(content="", tool_calls=[{"id": "1", "name": "edgar_search", "args": {"query": "x"}}]))
    fake_model.queue(AIMessage(content="done", tool_calls=[]))
    fake_model.queue(AIMessage(content="[]"))
    result = await run_researcher_lane("people", "find someone", {"canonical_name": "Acme", "aliases": [], "injected_facts": {}})
    assert result["lane_status"] == "ok"


def _fake_tool(name, result):
    async def ainvoke(args):
        return result
    return SimpleNamespace(name=name, ainvoke=ainvoke)


async def test_researcher_tools_node_had_real_evidence_stays_false_on_all_error_batch(monkeypatch):
    import app.researcher as researcher_mod
    monkeypatch.setattr(
        researcher_mod, "LANE_TOOLS",
        {"identity_and_type": [_fake_tool("web_search", {"error": "boom"}), _fake_tool("fetch_page", {"url": "http://x", "content": ""})]},
    )
    state = _researcher_state(
        had_real_evidence=False,
        researcher_messages=[AIMessage(content="", tool_calls=[
            {"id": "a", "name": "web_search", "args": {"query": "q"}},
            {"id": "b", "name": "fetch_page", "args": {"url": "http://x"}},
        ])],
    )
    delta = await researcher_tools_node(state)
    assert delta["had_real_evidence"] is False


async def test_researcher_tools_node_had_real_evidence_true_with_one_usable_result(monkeypatch):
    import app.researcher as researcher_mod
    usable = {"results": [{"url": "http://x", "title": "t", "content": "short"}], "query": "q"}
    monkeypatch.setattr(
        researcher_mod, "LANE_TOOLS",
        {"identity_and_type": [_fake_tool("web_search", {"error": "boom"}), _fake_tool("edgar_search", usable)]},
    )
    state = _researcher_state(
        had_real_evidence=False,
        researcher_messages=[AIMessage(content="", tool_calls=[
            {"id": "a", "name": "web_search", "args": {"query": "q"}},
            {"id": "b", "name": "edgar_search", "args": {"query": "q"}},
        ])],
    )
    delta = await researcher_tools_node(state)
    assert delta["had_real_evidence"] is True


async def test_researcher_tools_node_had_real_evidence_preserves_prior_true(monkeypatch):
    # If the lane already had evidence, an all-error batch must not flip it back to False.
    import app.researcher as researcher_mod
    monkeypatch.setattr(
        researcher_mod, "LANE_TOOLS",
        {"identity_and_type": [_fake_tool("web_search", {"error": "boom"})]},
    )
    state = _researcher_state(
        had_real_evidence=True,
        researcher_messages=[AIMessage(content="", tool_calls=[
            {"id": "a", "name": "web_search", "args": {"query": "q"}},
        ])],
    )
    delta = await researcher_tools_node(state)
    assert delta["had_real_evidence"] is True


def _compress_claims_json(tag_unclassified=None):
    # One could_not_verify claim left unclassified (source_class null), one already
    # classified by the LLM (source_class "news") that must be left alone.
    import json as _json
    items = [
        {"question_id": "G3.Q1", "answer": "nothing found", "status": "could_not_verify",
         "source_url": None, "source_class": tag_unclassified, "confidence": "low"},
        {"question_id": "G3.Q3", "answer": "clean", "status": "confirmed",
         "source_url": "http://x", "source_class": "news", "confidence": "medium"},
    ]
    return AIMessage(content=_json.dumps(items))


async def test_compress_tags_tool_unavailable_when_no_real_evidence(fake_model):
    fake_model.queue(_compress_claims_json())
    state = _researcher_state(lane="activity_signals", had_real_evidence=False, raw_notes=["some notes"])
    delta = await compress_to_claims_node(state)
    by_q = {c["question_id"]: c for c in delta["claims"]}
    assert by_q["G3.Q1"]["source_class"] == "tool_unavailable"
    # An LLM-set source_class must not be overwritten.
    assert by_q["G3.Q3"]["source_class"] == "news"


async def test_compress_tags_no_evidence_found_when_real_evidence_present(fake_model):
    fake_model.queue(_compress_claims_json())
    state = _researcher_state(lane="activity_signals", had_real_evidence=True, raw_notes=["some notes"])
    delta = await compress_to_claims_node(state)
    by_q = {c["question_id"]: c for c in delta["claims"]}
    assert by_q["G3.Q1"]["source_class"] == "no_evidence_found"
    assert by_q["G3.Q3"]["source_class"] == "news"


async def test_compress_does_not_overwrite_llm_set_source_class(fake_model):
    # LLM explicitly set source_class on the could_not_verify claim itself.
    fake_model.queue(_compress_claims_json(tag_unclassified="llm_said_so"))
    state = _researcher_state(lane="activity_signals", had_real_evidence=False, raw_notes=["some notes"])
    delta = await compress_to_claims_node(state)
    by_q = {c["question_id"]: c for c in delta["claims"]}
    assert by_q["G3.Q1"]["source_class"] == "llm_said_so"


def test_lane_system_prompt_identity_and_type_names_direct_api_tools_and_hard_gates():
    prompt = _lane_system_prompt(_researcher_state(lane="identity_and_type"))
    assert "edgar_submissions" in prompt
    assert "fetch_page" in prompt
    assert "nonprofit_lookup" in prompt
    # Every HARD-gate question_id for this lane must appear in the prompt text.
    hard_ids = [q.question_id for q in QUESTIONS_BY_LANE["identity_and_type"] if q.gate == "HARD"]
    for qid in hard_ids:
        assert qid in prompt, qid


def test_lane_system_prompt_lists_hard_gates_for_every_lane():
    for lane, questions in QUESTIONS_BY_LANE.items():
        prompt = _lane_system_prompt(_researcher_state(lane=lane))
        hard_ids = [q.question_id for q in questions if q.gate == "HARD"]
        if hard_ids:
            for qid in hard_ids:
                assert qid in prompt, (lane, qid)
        else:
            # A lane with no HARD gates must not emit the HARD-gate instruction block.
            assert "HARD-gate questions" not in prompt

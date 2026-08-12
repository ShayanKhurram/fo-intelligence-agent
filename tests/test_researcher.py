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
        {"question_id": "G3.Q2", "answer": "recent hire announced", "status": "confirmed",
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
    assert by_q["G3.Q2"]["source_class"] == "news"


async def test_compress_tags_no_evidence_found_when_real_evidence_present(fake_model):
    fake_model.queue(_compress_claims_json())
    state = _researcher_state(lane="activity_signals", had_real_evidence=True, raw_notes=["some notes"])
    delta = await compress_to_claims_node(state)
    by_q = {c["question_id"]: c for c in delta["claims"]}
    assert by_q["G3.Q1"]["source_class"] == "no_evidence_found"
    assert by_q["G3.Q2"]["source_class"] == "news"


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


# --- 2026-08-12 fixes: system-only message list, seed persistence, placeholder gap class ---


def test_evidence_gap_tagging_overrides_placeholder_source_class():
    """The compress model emits source_class="unknown", which is truthy — a plain falsy
    check treated it as a real classification and skipped gap tagging entirely, destroying
    the tool_unavailable vs no_evidence_found distinction. That distinction is the only
    signal separating "our tooling is down" from "this firm has no public footprint"."""
    from app.researcher import _tag_evidence_gaps
    from app.state import Claim

    claims = [
        Claim(question_id="G1.Q1", answer="unknown", status="could_not_verify", source_class="unknown", confidence="low"),
        Claim(question_id="G1.Q2", answer="unknown", status="could_not_verify", source_class="none", confidence="low"),
        Claim(question_id="G1.Q3", answer="unknown", status="could_not_verify", source_class=None, confidence="low"),
        Claim(question_id="G1.Q4", answer="unknown", status="could_not_verify", source_class="", confidence="low"),
    ]
    _tag_evidence_gaps(claims, had_real_evidence=False)
    assert [c.source_class for c in claims] == ["tool_unavailable"] * 4


def test_evidence_gap_tagging_uses_no_evidence_found_when_tools_worked():
    from app.researcher import _tag_evidence_gaps
    from app.state import Claim

    claims = [Claim(question_id="G1.Q1", answer="unknown", status="could_not_verify",
                    source_class="unknown", confidence="low")]
    _tag_evidence_gaps(claims, had_real_evidence=True)
    assert claims[0].source_class == "no_evidence_found"


def test_evidence_gap_tagging_never_overwrites_a_real_class():
    from app.researcher import _tag_evidence_gaps
    from app.state import Claim

    claims = [
        Claim(question_id="G1.Q1", answer="x", status="could_not_verify",
              source_class="uncompressed_notes", confidence="low"),
        Claim(question_id="G1.Q2", answer="x", status="confirmed", source_class="unknown",
              confidence="high"),
    ]
    _tag_evidence_gaps(claims, had_real_evidence=False)
    assert claims[0].source_class == "uncompressed_notes"
    assert claims[1].source_class == "unknown", "only could_not_verify claims are tagged"


async def test_researcher_node_seeds_a_user_turn_and_persists_the_seed(monkeypatch):
    """Ollama Cloud treats a system-only message list as a model-LOAD request: HTTP 200,
    finish_reason="load", empty content, no tool calls, 0/0 tokens. Nothing raises, so every
    lane silently produced zero notes and marked all questions could_not_verify.

    The seed must ALSO be persisted: researcher_messages uses the add_messages reducer, so
    returning only the response meant turn 2+ ran with no system prompt at all."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    import app.researcher as researcher_mod

    captured = {}

    class _Model:
        async def ainvoke(self, messages, tools=None):
            captured["messages"] = list(messages)
            return AIMessage(content="", tool_calls=[], response_metadata={"cost_usd": 0.0})

    monkeypatch.setattr(researcher_mod, "get_model", lambda tier: _Model())

    state = {
        "lane": "identity_and_type",
        "instructions": "check identity",
        "lead_brief_slim": {"canonical_name": "Acme FO", "aliases": [], "injected_facts": {}},
        "researcher_messages": [],
        "raw_notes": [],
        "tool_calls_used": 0,
        "had_real_evidence": False,
        "claims": [],
        "lane_status": "ok",
        "cost_usd": 0.0,
        "trace": [],
    }
    out = await researcher_mod.researcher_node(state)

    sent = captured["messages"]
    assert isinstance(sent[0], SystemMessage)
    assert any(isinstance(m, HumanMessage) for m in sent), (
        "a system-only list makes Ollama Cloud return an empty load response"
    )

    returned = out["researcher_messages"]
    assert isinstance(returned[0], SystemMessage), "the system prompt must survive to turn 2"
    assert any(isinstance(m, HumanMessage) for m in returned)


async def test_supervisor_node_seeds_and_persists_a_user_turn(monkeypatch):
    """Same root cause as the researcher: without a user turn the supervisor never emitted
    conduct_research, so no lane was ever dispatched and every lead ran out its iteration
    budget and rejected on unanswered HARD gates at ~$0."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    import app.supervisor as supervisor_mod
    from app.state import LeadBrief, new_supervisor_state

    captured = {}

    class _Model:
        async def ainvoke(self, messages, tools=None):
            captured["messages"] = list(messages)
            return AIMessage(content="", tool_calls=[], response_metadata={"cost_usd": 0.0})

    monkeypatch.setattr(supervisor_mod, "get_model", lambda tier: _Model())

    state = new_supervisor_state("e1")
    state["lead_brief"] = LeadBrief(entity_id="e1", canonical_name="Acme FO")
    out = await supervisor_mod.supervisor_node(state)

    sent = captured["messages"]
    assert isinstance(sent[0], SystemMessage)
    assert any(isinstance(m, HumanMessage) for m in sent)
    assert any(isinstance(m, HumanMessage) for m in out["supervisor_messages"])


# --- note formatting must preserve structured fields (2026-08-12) ---


async def test_note_preserves_structured_result_fields():
    """The formatter used to emit only title/url/date/snippet, silently discarding every
    other field. Consequence: `adv_lookup` results reached the model as a bare URL twice —
    sec_number, branches_count and name_match all dropped — so the tool built specifically
    to settle G1.Q4/G1.Q5 delivered nothing and G1.Q4 stayed could_not_verify."""
    from app.researcher import _format_tool_result_as_note

    result = {
        "query": "Pathstone Family Office",
        "total_matches": 1,
        "exact_matches": 1,
        "results": [
            {
                "firm_name": "PATHSTONE",
                "sec_number": "801-70776",
                "is_registered_investment_adviser": True,
                "branches_count": 162,
                "name_match": "exact",
                "url": "https://adviserinfo.sec.gov/firm/summary/151736",
            }
        ],
    }
    note, cost = await _format_tool_result_as_note("adv_lookup", result)
    assert "801-70776" in note
    assert "branches_count=162" in note
    assert "name_match=exact" in note
    assert "exact_matches=1" in note, "set-level counters belong on the header line"
    assert cost == 0.0, "structured fields must never go through the paid summarizer"


async def test_note_preserves_edgar_company_name_and_form_type():
    """edgar_search hits lost company_name and form_type, so the model cited EDGAR archive
    URLs without knowing which company or filing they belonged to."""
    from app.researcher import _format_tool_result_as_note

    result = {
        "query": "Pathstone",
        "results": [
            {
                "cik": "0001518042",
                "company_name": "PATHSTONE FAMILY OFFICE LLC",
                "form_type": ["13F-HR"],
                "filed_at": "2026-05-14",
                "url": "https://www.sec.gov/Archives/edgar/data/x.txt",
            }
        ],
    }
    note, _ = await _format_tool_result_as_note("edgar_search", result)
    assert "PATHSTONE FAMILY OFFICE LLC" in note
    assert "13F-HR" in note
    assert "2026-05-14" in note


async def test_note_caps_field_count_and_value_length():
    """A wide record must not be able to flood a lane's context."""
    from app.researcher import _format_tool_result_as_note

    row = {f"k{i}": f"v{i}" for i in range(40)}
    row["long"] = "x" * 5000
    note, _ = await _format_tool_result_as_note("some_tool", {"query": "q", "results": [row]})
    assert "x" * 5000 not in note
    assert note.count("=") <= 60


async def test_note_handles_non_dict_rows_without_crashing():
    from app.researcher import _format_tool_result_as_note

    note, _ = await _format_tool_result_as_note("odd_tool", {"query": "q", "results": ["bare string"]})
    assert "bare string" in note


async def test_note_still_summarizes_free_text_snippets(monkeypatch):
    """Long prose still goes through the cheap summarizer; only structured fields bypass it."""
    import app.researcher as researcher_mod
    from app.researcher import _format_tool_result_as_note

    async def _fake_summarize(text):
        return "SUMMARIZED", 0.01

    monkeypatch.setattr(researcher_mod, "_summarize_text", _fake_summarize)
    result = {"query": "q", "results": [{"url": "http://x", "content": "y" * 500, "score": 3}]}
    note, cost = await _format_tool_result_as_note("web_search", result)
    assert "SUMMARIZED" in note
    assert "score=3" in note
    assert cost == 0.01


# --- contradiction discipline in the compress step (2026-08-12) ---


def _c(qid, answer, status="contradicted", source_url="http://a"):
    from app.state import Claim

    return Claim(question_id=qid, answer=answer, status=status,
                 source_url=source_url, confidence="medium")


def test_uncited_contradiction_is_downgraded():
    """142 of the ledger's 203 contradicted claims came from this step, versus ZERO from V4 —
    the check actually named for contradictions. The samples were plain confirmations, e.g.
    "Co-Managing Members Mary C. McNutt and Michelle J. Blass" stored as contradicted purely
    because two people were named. Claiming a conflict now requires citing two sources."""
    from app.researcher import _downgrade_unsupported_contradictions

    claims = [_c("G2.Q1", "Yes - Co-Managing Members Mary C. McNutt and Michelle J. Blass.")]
    downgraded = _downgrade_unsupported_contradictions(claims)
    assert claims[0].status == "confirmed"
    assert len(downgraded) == 1
    assert downgraded[0][0] == "G2.Q1"


def test_genuinely_cited_contradiction_survives():
    """Two distinct URLs in the answer = the model demonstrated the conflict. Left alone."""
    from app.researcher import _downgrade_unsupported_contradictions

    claims = [_c("G1.Q6", "Dissolved per https://a.example/x but actively filing per "
                          "https://b.example/y")]
    assert _downgrade_unsupported_contradictions(claims) == []
    assert claims[0].status == "contradicted"


def test_same_url_twice_is_not_two_sources():
    from app.researcher import _downgrade_unsupported_contradictions

    claims = [_c("G1.Q6", "conflict per https://a.example/x and https://a.example/x")]
    _downgrade_unsupported_contradictions(claims)
    assert claims[0].status == "confirmed"


def test_uncited_contradiction_without_source_url_becomes_could_not_verify():
    """Never flip to a status stronger than the evidence supports."""
    from app.researcher import _downgrade_unsupported_contradictions

    claims = [_c("G1.Q3", "sources disagree", source_url=None)]
    _downgrade_unsupported_contradictions(claims)
    assert claims[0].status == "could_not_verify"


def test_non_contradicted_claims_are_untouched():
    from app.researcher import _downgrade_unsupported_contradictions

    claims = [_c("G1.Q1", "Yes it exists", status="confirmed"),
              _c("G1.Q4", "unknown", status="could_not_verify", source_url=None)]
    assert _downgrade_unsupported_contradictions(claims) == []
    assert [c.status for c in claims] == ["confirmed", "could_not_verify"]


def test_compress_prompt_states_the_not_a_contradiction_cases():
    """The prompt is the other half of the fix — a code guard alone would keep fighting a
    model that thinks two co-principals conflict."""
    from app.researcher import _COMPRESS_SYSTEM_TEMPLATE

    t = _COMPRESS_SYSTEM_TEMPLATE.lower()
    assert "mutually exclusive" in t
    assert "co-managing" in t or "senior role" in t
    assert "silence is not" in t

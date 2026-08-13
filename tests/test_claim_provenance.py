"""T25.1 — the retrieval path is stamped on researcher claims in code, never
model-asserted.

`researcher_tools_node` builds a `{url: tool_name}` provenance map for every usable
result; `compress_to_claims_node` stamps each parsed claim's `extraction_method` from
that map, falling back to `researcher_<lane>` when a claim cites a URL no tool reported
or has no source_url at all. An `extraction_method` the model already supplied is never
overwritten.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from app.researcher import (
    _stamp_extraction_method,
    _urls_in_result,
    compress_to_claims_node,
    researcher_tools_node,
)
from app.state import Claim


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
        "url_provenance": {},
        "lane_status": "ok",
        "cost_usd": 0.0,
        "trace": [],
    }
    base.update(overrides)
    return base


def _fake_tool(name, result):
    async def ainvoke(args):
        return result
    return SimpleNamespace(name=name, ainvoke=ainvoke)


# --- _urls_in_result: top-level + nested-in-results ---

def test_urls_in_result_collects_top_level_url_and_source_url():
    assert _urls_in_result({"url": "http://a", "content": "x"}) == {"http://a"}
    assert _urls_in_result({"source_url": "http://b", "content": "x"}) == {"http://b"}


def test_urls_in_result_collects_urls_inside_results_list():
    result = {"query": "q", "results": [
        {"url": "http://a", "title": "t"},
        {"source_url": "http://b"},
        {"title": "no url here"},
    ]}
    assert _urls_in_result(result) == {"http://a", "http://b"}


def test_urls_in_result_ignores_non_dict_and_non_string_urls():
    assert _urls_in_result("not a dict") == set()
    assert _urls_in_result({"url": None, "results": [{"url": 123}]}) == set()
    assert _urls_in_result({"url": ""}) == set()


# --- _stamp_extraction_method: the three cases ---

def test_stamp_uses_tool_name_when_url_is_in_provenance():
    claims = [Claim(question_id="G1.Q1", answer="x", status="confirmed",
                    source_url="http://sec.gov/x", confidence="high")]
    _stamp_extraction_method(claims, {"http://sec.gov/x": "edgar_search"}, "identity_and_type")
    assert claims[0].extraction_method == "edgar_search"


def test_stamp_falls_back_to_researcher_lane_for_unknown_url():
    claims = [Claim(question_id="G1.Q1", answer="x", status="confirmed",
                    source_url="http://elsewhere/y", confidence="high")]
    _stamp_extraction_method(claims, {"http://sec.gov/x": "edgar_search"}, "identity_and_type")
    assert claims[0].extraction_method == "researcher_identity_and_type"


def test_stamp_falls_back_to_researcher_lane_for_claim_with_no_source_url():
    claims = [Claim(question_id="G1.Q1", answer="nothing", status="could_not_verify",
                    source_url=None, confidence="low")]
    _stamp_extraction_method(claims, {"http://sec.gov/x": "edgar_search"}, "activity_signals")
    assert claims[0].extraction_method == "researcher_activity_signals"


def test_stamp_never_overwrites_a_model_supplied_extraction_method():
    claims = [Claim(question_id="G1.Q1", answer="x", status="confirmed",
                    source_url="http://sec.gov/x", confidence="high",
                    extraction_method="model_said_so")]
    _stamp_extraction_method(claims, {"http://sec.gov/x": "edgar_search"}, "identity_and_type")
    assert claims[0].extraction_method == "model_said_so"


# --- integration: researcher_tools_node builds provenance, compress stamps from it ---

async def test_researcher_tools_node_records_url_provenance_for_usable_results(monkeypatch):
    import app.researcher as researcher_mod
    edgar_result = {"query": "q", "results": [
        {"url": "http://sec.gov/x", "company_name": "X", "form_type": ["13F-HR"],
         "filed_at": "2026", "cik": "1"}]}
    monkeypatch.setattr(
        researcher_mod, "LANE_TOOLS",
        {"identity_and_type": [_fake_tool("edgar_search", edgar_result)]},
    )
    state = _researcher_state(
        researcher_messages=[AIMessage(content="", tool_calls=[
            {"id": "1", "name": "edgar_search", "args": {"query": "q"}}])],
    )
    delta = await researcher_tools_node(state)
    assert delta["url_provenance"] == {"http://sec.gov/x": "edgar_search"}


async def test_researcher_tools_node_does_not_record_provenance_for_unusable_results(monkeypatch):
    import app.researcher as researcher_mod
    # errored result -> not usable -> no provenance, even though it carries a url.
    monkeypatch.setattr(
        researcher_mod, "LANE_TOOLS",
        {"identity_and_type": [_fake_tool("edgar_search", {"error": "boom", "url": "http://x"})]},
    )
    state = _researcher_state(
        researcher_messages=[AIMessage(content="", tool_calls=[
            {"id": "1", "name": "edgar_search", "args": {"query": "q"}}])],
    )
    delta = await researcher_tools_node(state)
    assert delta["url_provenance"] == {}


async def test_compress_stamps_extraction_method_from_url_provenance(fake_model, monkeypatch):
    import app.researcher as researcher_mod
    edgar_result = {"query": "q", "results": [
        {"url": "http://sec.gov/x", "company_name": "X", "form_type": ["13F-HR"],
         "filed_at": "2026", "cik": "1"}]}
    monkeypatch.setattr(
        researcher_mod, "LANE_TOOLS",
        {"identity_and_type": [_fake_tool("edgar_search", edgar_result)]},
    )
    tools_state = _researcher_state(
        researcher_messages=[AIMessage(content="", tool_calls=[
            {"id": "1", "name": "edgar_search", "args": {"query": "q"}}])],
    )
    tools_delta = await researcher_tools_node(tools_state)

    # One claim citing the URL the tool reported, one citing a URL no tool reported,
    # and one could_not_verify claim with no source_url.
    fake_model.queue(AIMessage(content=json.dumps([
        {"question_id": "G1.Q1", "answer": "exists", "status": "confirmed",
         "source_url": "http://sec.gov/x", "source_class": "edgar", "confidence": "high"},
        {"question_id": "G1.Q2", "answer": "other", "status": "confirmed",
         "source_url": "http://elsewhere/y", "source_class": "web", "confidence": "medium"},
        {"question_id": "G1.Q3", "answer": "unknown", "status": "could_not_verify",
         "source_url": None, "source_class": "unknown", "confidence": "low"},
    ])))
    compress_state = _researcher_state(
        lane="identity_and_type", had_real_evidence=True, raw_notes=["some notes"],
        url_provenance=tools_delta["url_provenance"],
    )
    delta = await compress_to_claims_node(compress_state)
    by_q = {c["question_id"]: c for c in delta["claims"]}
    assert by_q["G1.Q1"]["extraction_method"] == "edgar_search"
    assert by_q["G1.Q2"]["extraction_method"] == "researcher_identity_and_type"
    assert by_q["G1.Q3"]["extraction_method"] == "researcher_identity_and_type"
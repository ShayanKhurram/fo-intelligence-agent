from __future__ import annotations

import pytest

from app.db import add_entity_source, connection, upsert_entity
from app.parser import build_lead_brief, compute_injected_facts


def test_build_lead_brief_unknown_entity_raises(db_path):
    with connection(db_path) as conn:
        with pytest.raises(ValueError):
            build_lead_brief(conn, "nope")


def test_build_lead_brief_attaches_full_question_battery(db_path):
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Acme Family Office", aliases=["Acme FO"])
        brief, _ = build_lead_brief(conn, "E1")
    assert brief.canonical_name == "Acme Family Office"
    assert brief.aliases == ["Acme FO"]
    assert len(brief.questions) == 11
    assert brief.budget.max_tool_calls == 8


def test_adv_index_preanswers_sfo_mfo_question(db_path):
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Acme Family Office")
        add_entity_source(conn, "E1", "adv_index", {"present": True, "client_count": 1, "crd": "999"}, url="http://adv")
        brief, pre_answered = build_lead_brief(conn, "E1")

    assert brief.injected_facts["adv_present"] is True
    assert brief.injected_facts["adv_client_count"] == 1
    claims_by_q = {c.question_id: c for c in pre_answered}
    assert "G1.Q4" in claims_by_q
    assert "SFO" in claims_by_q["G1.Q4"].answer
    assert claims_by_q["G1.Q4"].status == "confirmed"
    assert claims_by_q["G1.Q4"].source_class == "adv_index"


def test_adv_client_count_above_one_yields_mfo(db_path):
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Acme Family Office")
        add_entity_source(conn, "E1", "adv_index", {"present": True, "client_count": 5})
        _, pre_answered = build_lead_brief(conn, "E1")
    claim = next(c for c in pre_answered if c.question_id == "G1.Q4")
    assert "MFO" in claim.answer


def test_13f_filing_preanswers_capital_deployment(db_path):
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Acme Family Office")
        add_entity_source(
            conn, "E1", "13f_filing",
            {"quarter": "2026Q1", "value_usd": 50_000_000, "prior_quarter": "2025Q4", "prior_value_usd": 40_000_000},
            url="http://13f",
        )
        brief, pre_answered = build_lead_brief(conn, "E1")

    assert brief.injected_facts["filing_13f_qoq_delta_pct"] == 25.0
    claim = next(c for c in pre_answered if c.question_id == "G3.Q1")
    assert claim.status == "confirmed"
    assert claim.confidence == "high"


def test_source_class_count_aggregates_distinct_classes(db_path):
    with connection(db_path) as conn:
        upsert_entity(conn, "E1", "Acme Family Office")
        add_entity_source(conn, "E1", "adv_index", {"present": True, "client_count": 1})
        add_entity_source(conn, "E1", "conference_sighting", {"conference": "FOX", "date": "2026-01-01"})
        add_entity_source(conn, "E1", "conference_sighting", {"conference": "FOX2", "date": "2026-02-01"})
        brief, _ = build_lead_brief(conn, "E1")
    assert brief.injected_facts["source_class_count"] == 2


def test_compute_injected_facts_ignores_unrelated_source_classes():
    sources = [
        {"source_class": "web_page", "payload": {}, "url": "http://x", "retrieved_at": "2026-01-01T00:00:00Z"},
    ]
    facts = compute_injected_facts(sources)
    assert "adv_present" not in facts
    assert facts["source_class_count"] == 1

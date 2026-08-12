"""T18.5 — standing proof that discovery data actually reaches the model.

These tests assert on the **rendered lane system prompt string**, not on intermediate
dicts. That string is literally what the LLM sees, so it is the only assertion that
cannot pass while the data is still stranded somewhere upstream in the DB. If a future
change re-breaks the adv_name/fec_employer -> injected_facts -> prompt path, this file
fails loudly.
"""
from __future__ import annotations

import json

from app.db import connection
from app.ingest import ingest_discovery_file
from app.parser import DISCOVERY_CLASS_EXTRACTORS, build_lead_brief
from app.researcher import _lane_system_prompt


# adv_name fixture — exact field shape emitted by
# C:/Users/HP/scraping_data_task_!/fo_discovery/connectors/c2_form_adv.py:357
ADV_NAME_RECORD = {
    "entity_name_raw": "MATTER FAMILY OFFICE",
    "discovery_class": "adv_name",
    "discovery_source_id": "crd:123456",
    "discovery_url": "https://adviserinfo.sec.gov/firm/123456",
    "retrieved_at": "2026-08-12T19:09:36+00:00",
    "state": "MO",
    "address_raw": "1 Main St, St Louis, MO, 63105",
    "people": [],
    "signals": {
        "crd": "123456",
        "raum_usd": 4200000000.0,
        "hnw_clients": 66.0,
        "retail_clients": 0.0,
        "hnw_raum_share": 0.9,
        "status": "Approved",
        "website": "https://matterfamilyoffice.com",
        "adv_match": True,
    },
    "raw_payload": {
        "legal_name": "MATTER FAMILY OFFICE LLC",
        "primary_business_name": "MATTER FAMILY OFFICE",
        "crd": "123456",
        "country": "United States",
    },
}

# fec_employer fixture — real record shape from data/fo_discovery_c3_fec_20260813.jsonl
FEC_EMPLOYER_RECORD = {
    "entity_name_raw": "WISE FAMILY WEALTH",
    "discovery_class": "fec_employer",
    "discovery_source_id": "fec:employer:WISE FAMILY WEALTH",
    "discovery_url": "https://api.open.fec.gov/v1/schedules/schedule_a/",
    "retrieved_at": "2026-08-12T19:18:27.281494+00:00",
    "state": "TN",
    "address_raw": "NASHVILLE, TN",
    "people": [
        {
            "name": "WHITNEY, MICHAEL",
            "title": "FINANCIAL ADVISOR",
            "source_url": "https://api.open.fec.gov/v1/schedules/schedule_a/",
        }
    ],
    "signals": {
        "contribution_count": 45,
        "distinct_contributors": 1,
        "first_contribution": "2025-07-11",
        "last_contribution": "2026-05-21",
        "contribution_date": "2026-05-21",
        "name_variants": ["WISE FAMILY WEALTH"],
        "states_seen": ["TN"],
    },
    "raw_payload": {
        "normalized_name": "WISE FAMILY WEALTH",
        "variants": ["WISE FAMILY WEALTH"],
        "cycles": ["2026"],
    },
}

# Values that MUST appear verbatim in the rendered lane system prompt. If any of these
# is missing from the prompt, the discovery data did not reach the model.
#
# The adv facts and the FEC counts/dates reach the prompt through the
# `json.dumps(injected_facts)` render, so they are asserted as the EXACT rendered
# key/value pair — a bare value like `123456` would also match the adv_source_url
# `https://adviserinfo.sec.gov/firm/123456` and let a stranded `adv_crd` pass silently.
# The pair form is what makes these assertions load-bearing; see
# test_contract_assertions_fail_when_a_fact_is_dropped for the proof.
EXPECTED_VISIBLE_ADV = [
    '"adv_crd": "123456"',
    '"adv_raum_usd": 4200000000.0',
    '"adv_website": "https://matterfamilyoffice.com"',
    '"adv_status": "Approved"',
]

EXPECTED_VISIBLE_FEC = [
    '"fec_contribution_count": 45',
    '"fec_last_contribution": "2026-05-21"',
]

# These do NOT come through the JSON — they render in the unverified-leads block, so
# they stay as plain substring assertions and live in their own list to keep the two
# channels asserted distinctly.
EXPECTED_VISIBLE_FEC_LEADS = [
    "WHITNEY, MICHAEL",
    "FINANCIAL ADVISOR",
]

FEC_LEAD_LINE = "- person: WHITNEY, MICHAEL (FINANCIAL ADVISOR) [via fec_employer]"


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _minimal_researcher_state(lane, instructions, brief):
    return {
        "lane": lane,
        "instructions": instructions,
        "lead_brief_slim": brief,
        "researcher_messages": [],
        "raw_notes": [],
        "tool_calls_used": 0,
        "had_real_evidence": False,
        "claims": [],
        "lane_status": "ok",
        "cost_usd": 0.0,
        "trace": [],
    }


def _slim_from_brief(brief):
    # Built the same way supervisor.py:215 builds lead_brief_slim.
    return {
        "canonical_name": brief.canonical_name,
        "aliases": brief.aliases,
        "injected_facts": brief.injected_facts,
        "unverified_leads": brief.unverified_leads,
    }


def test_adv_name_discovery_data_reaches_researcher_prompt(db_path, tmp_path):
    jsonl = tmp_path / "adv.jsonl"
    _write_jsonl(jsonl, [ADV_NAME_RECORD])
    with connection(db_path) as conn:
        order = ingest_discovery_file(conn, jsonl)
        assert len(order) == 1
        brief, _ = build_lead_brief(conn, order[0])

    state = _minimal_researcher_state(
        lane="identity_and_type",
        instructions="test",
        brief=_slim_from_brief(brief),
    )
    prompt = _lane_system_prompt(state)
    for expected in EXPECTED_VISIBLE_ADV:
        assert expected in prompt, f"adv_name discovery value {expected!r} missing from prompt"


def test_fec_employer_discovery_data_reaches_researcher_prompt(db_path, tmp_path):
    jsonl = tmp_path / "fec.jsonl"
    _write_jsonl(jsonl, [FEC_EMPLOYER_RECORD])
    with connection(db_path) as conn:
        order = ingest_discovery_file(conn, jsonl)
        assert len(order) == 1
        brief, _ = build_lead_brief(conn, order[0])

    state = _minimal_researcher_state(
        lane="people",
        instructions="test",
        brief=_slim_from_brief(brief),
    )
    prompt = _lane_system_prompt(state)
    # injected_facts channel (JSON render) — load-bearing pair assertions.
    for expected in EXPECTED_VISIBLE_FEC:
        assert expected in prompt, f"fec_employer injected_fact {expected!r} missing from prompt"
    # unverified-leads channel — distinct from the JSON facts. The `NOT answers` label
    # is the only thing stopping the model from treating a discovery seed as a confirmed
    # fact, so it is asserted explicitly, along with the exact rendered lead line.
    for expected in EXPECTED_VISIBLE_FEC_LEADS:
        assert expected in prompt, f"fec_employer lead value {expected!r} missing from prompt"
    assert "NOT answers" in prompt
    assert FEC_LEAD_LINE in prompt


def test_no_leads_block_when_no_unverified_leads(db_path, tmp_path):
    # A 13f_filing-only entity has no non-preanswered source classes, so no unverified
    # leads and therefore NO leads block at all — not even a stray header.
    jsonl = tmp_path / "13f.jsonl"
    _write_jsonl(
        jsonl,
        [
            {
                "entity_name_raw": "Acme Family Office LLC",
                "discovery_class": "edgar_13f",
                "discovery_url": "http://sec.gov/x",
                "state": "NY",
                "signals": {"aum_13f": 1000.0, "position_count": 5},
                "raw_payload": {"year": 2026, "qtr": 1, "primary_doc": {"tableValueTotal": "1000"}},
            }
        ],
    )
    with connection(db_path) as conn:
        order = ingest_discovery_file(conn, jsonl)
        assert len(order) == 1
        brief, _ = build_lead_brief(conn, order[0])

    state = _minimal_researcher_state(
        lane="identity_and_type",
        instructions="test",
        brief=_slim_from_brief(brief),
    )
    prompt = _lane_system_prompt(state)
    assert "UNVERIFIED LEADS" not in prompt


def test_contract_assertions_fail_when_a_fact_is_dropped(db_path, tmp_path):
    """Mutation proof: if `adv_crd` is stranded out of injected_facts, the adv contract
    assertion MUST fail. This is what stops EXPECTED_VISIBLE_ADV regressing to a bare
    value that matches the adv_source_url by accident."""
    jsonl = tmp_path / "adv.jsonl"
    _write_jsonl(jsonl, [ADV_NAME_RECORD])
    with connection(db_path) as conn:
        order = ingest_discovery_file(conn, jsonl)
        assert len(order) == 1
        brief, _ = build_lead_brief(conn, order[0])

    # Sabotage: drop adv_crd exactly as a regression would.
    brief.injected_facts.pop("adv_crd", None)
    # The LeadBrief is a pydantic model; mutating its dict field propagates into the slim
    # dict we build below, which is what the prompt renders.
    state = _minimal_researcher_state(
        lane="identity_and_type",
        instructions="test",
        brief=_slim_from_brief(brief),
    )
    prompt = _lane_system_prompt(state)
    assert '"adv_crd": "123456"' not in prompt


def test_every_priority_class_has_an_extractor():
    for cls in ("adv_name", "fec_employer", "13f_filing", "5500_filing"):
        assert cls in DISCOVERY_CLASS_EXTRACTORS, f"{cls!r} has no registered extractor"
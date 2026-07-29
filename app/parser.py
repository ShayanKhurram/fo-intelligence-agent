"""Deterministic Parser node — plan §4.1. Replaces ODR's clarify_with_user +
write_research_brief. No LLM call: the LeadBrief is assembled entirely from the DB, so
it is free and reproducible. Also pre-answers whatever the DB already settles, so the
supervisor never spends a tool call re-deriving a fact we already have on file."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from app.db import get_entity, get_entity_sources
from app.questions import QUESTION_BATTERY
from app.state import Claim, LeadBrief, LeadBudget, SupervisorState


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return rows[-1] if rows else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def compute_injected_facts(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate entity_sources rows into the injected-facts dict (plan §4.1 step 2)."""
    facts: dict[str, Any] = {}

    adv_rows = [s for s in sources if s["source_class"] == "adv_index"]
    if adv := _latest(adv_rows):
        facts["adv_present"] = bool(adv["payload"].get("present"))
        facts["adv_client_count"] = adv["payload"].get("client_count")
        facts["adv_crd"] = adv["payload"].get("crd")

    filing_13f = [s for s in sources if s["source_class"] == "13f_filing"]
    if f13 := _latest(filing_13f):
        facts["filing_13f_value_usd"] = f13["payload"].get("value_usd")
        facts["filing_13f_quarter"] = f13["payload"].get("quarter")
        prior = f13["payload"].get("prior_value_usd")
        current = f13["payload"].get("value_usd")
        if prior is not None and current is not None and prior != 0:
            facts["filing_13f_qoq_delta_pct"] = round(
                (current - prior) / abs(prior) * 100, 2
            )

    filing_5500 = [s for s in sources if s["source_class"] == "5500_filing"]
    if f5500 := _latest(filing_5500):
        facts["filing_5500_participant_count"] = f5500["payload"].get("participant_count")
        facts["filing_5500_plan_year"] = f5500["payload"].get("plan_year")

    conf_rows = [s for s in sources if s["source_class"] == "conference_sighting"]
    if conf_rows:
        facts["conference_sightings"] = [
            {
                "conference": r["payload"].get("conference"),
                "date": r["payload"].get("date"),
                "role": r["payload"].get("role"),
            }
            for r in conf_rows
        ]

    domain_rows = [s for s in sources if s["source_class"] == "domain_check"]
    if dom := _latest(domain_rows):
        facts["domain"] = dom["payload"].get("domain")
        facts["domain_mx_present"] = dom["payload"].get("mx_present")

    facts["source_class_count"] = len({s["source_class"] for s in sources})
    return facts


def _pre_answer_claims(sources: list[dict[str, Any]], facts: dict[str, Any]) -> list[Claim]:
    """Plan §4.1 step 5 — pre-answer what the DB already answers. Conservative: only
    emits a claim when the underlying data unambiguously settles that question."""
    claims: list[Claim] = []

    adv_rows = [s for s in sources if s["source_class"] == "adv_index"]
    if (adv := _latest(adv_rows)) is not None and facts.get("adv_client_count") is not None:
        client_count = facts["adv_client_count"]
        answer = "SFO" if client_count <= 1 else "MFO"
        claims.append(
            Claim(
                question_id="G1.Q4",
                answer=f"{answer} (ADV client_count={client_count})",
                status="confirmed",
                source_url=adv.get("url"),
                source_class="adv_index",
                retrieved_at=_parse_dt(adv["retrieved_at"]),
                confidence="medium",
                produced_by="parser",
            )
        )

    filing_13f = [s for s in sources if s["source_class"] == "13f_filing"]
    if (f13 := _latest(filing_13f)) is not None and facts.get("filing_13f_value_usd") is not None:
        claims.append(
            Claim(
                question_id="G3.Q1",
                answer=(
                    f"13F value ${facts['filing_13f_value_usd']:,} "
                    f"({facts.get('filing_13f_quarter', 'unknown quarter')})"
                ),
                status="confirmed",
                source_url=f13.get("url"),
                source_class="13f_filing",
                retrieved_at=_parse_dt(f13["retrieved_at"]),
                confidence="high",
                produced_by="parser",
            )
        )

    filing_5500 = [s for s in sources if s["source_class"] == "5500_filing"]
    if (f5500 := _latest(filing_5500)) is not None:
        claims.append(
            Claim(
                question_id="G1.Q6",
                answer=(
                    f"Active per Form 5500 filing for plan year "
                    f"{facts.get('filing_5500_plan_year', 'unknown')}"
                ),
                status="confirmed",
                source_url=f5500.get("url"),
                source_class="5500_filing",
                retrieved_at=_parse_dt(f5500["retrieved_at"]),
                confidence="medium",
                produced_by="parser",
            )
        )

    conf_rows = [s for s in sources if s["source_class"] == "conference_sighting"]
    if conf_rows:
        latest_conf = _latest(conf_rows)
        claims.append(
            Claim(
                question_id="G3.Q2",
                answer=(
                    f"Conference sighting: {latest_conf['payload'].get('conference')} "
                    f"on {latest_conf['payload'].get('date')}"
                ),
                status="confirmed",
                source_url=latest_conf.get("url"),
                source_class="conference_sighting",
                retrieved_at=_parse_dt(latest_conf["retrieved_at"]),
                confidence="medium",
                produced_by="parser",
            )
        )

    return claims


def build_lead_brief(
    conn: sqlite3.Connection, entity_id: str, budget: LeadBudget | None = None
) -> tuple[LeadBrief, list[Claim]]:
    entity = get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"Unknown entity_id: {entity_id!r} — not found in entities table")

    sources = get_entity_sources(conn, entity_id)
    injected_facts = compute_injected_facts(sources)
    pre_answered = _pre_answer_claims(sources, injected_facts)

    brief = LeadBrief(
        entity_id=entity_id,
        canonical_name=entity["canonical_name"],
        aliases=entity["aliases"],
        injected_facts=injected_facts,
        questions=list(QUESTION_BATTERY),
        budget=budget or LeadBudget(),
    )
    return brief, pre_answered


def parser_node(state: SupervisorState, *, conn: sqlite3.Connection) -> dict[str, Any]:
    """LangGraph node entrypoint. `conn` is bound via functools.partial when the graph
    is assembled (app/graph.py) since node functions take only `state`."""
    brief, pre_answered = build_lead_brief(conn, state["entity_id"])
    return {
        "lead_brief": brief,
        "claims": [c.model_dump(mode="json") for c in pre_answered],
    }

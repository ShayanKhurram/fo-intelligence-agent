"""Deterministic Parser node — plan §4.1. Replaces ODR's clarify_with_user +
write_research_brief. No LLM call: the LeadBrief is assembled entirely from the DB, so
it is free and reproducible. Also pre-answers whatever the DB already settles, so the
supervisor never spends a tool call re-deriving a fact we already have on file."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Callable

from app.db import get_entity, get_entity_sources
from app.questions import QUESTION_BATTERY
from app.state import Claim, LeadBrief, LeadBudget, SupervisorState


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return rows[-1] if rows else None


def _first_payload_non_null(rows: list[dict[str, Any]], key: str) -> Any:
    """First non-null value of `key` found in the `payload` dicts of `rows`, in order."""
    for r in rows:
        v = (r.get("payload") or {}).get(key)
        if v is not None:
            return v
    return None


# Calibrated against the 87 ADV MFOs delivered by c2_form_adv.py: hnw_clients median 66.5,
# p10 8, max 1834. One family can plausibly generate 5-10 separate ADV client records
# (trusts, LLCs, individuals counted separately), so 15 clears that ceiling.
MAX_SINGLE_FAMILY_CLIENT_RECORDS = 15


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Discovery-class extractor registry (plan T18.2). Each registered function
# receives the rows of ONE source_class and returns a dict of facts to merge
# into `injected_facts`. A class with no entry produces nothing (and that is
# now a loud gap to fix via a new entry, not a silent {}). The five original
# branches are refactored here with byte-identical output keys and values.
# ---------------------------------------------------------------------------
DISCOVERY_CLASS_EXTRACTORS: dict[str, Callable[[list[dict[str, Any]]], dict[str, Any]]] = {}


def _extract_adv_index(rows: list[dict[str, Any]]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if adv := _latest(rows):
        facts["adv_present"] = bool(adv["payload"].get("present"))
        facts["adv_client_count"] = adv["payload"].get("client_count")
        facts["adv_crd"] = adv["payload"].get("crd")
    return facts


def _extract_13f_filing(rows: list[dict[str, Any]]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if f13 := _latest(rows):
        facts["filing_13f_value_usd"] = f13["payload"].get("value_usd")
        facts["filing_13f_quarter"] = f13["payload"].get("quarter")
        prior = f13["payload"].get("prior_value_usd")
        current = f13["payload"].get("value_usd")
        if prior is not None and current is not None and prior != 0:
            facts["filing_13f_qoq_delta_pct"] = round(
                (current - prior) / abs(prior) * 100, 2
            )
    return facts


def _extract_5500_filing(rows: list[dict[str, Any]]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if f5500 := _latest(rows):
        facts["filing_5500_participant_count"] = f5500["payload"].get("participant_count")
        facts["filing_5500_plan_year"] = f5500["payload"].get("plan_year")
    return facts


def _extract_conference_sighting(rows: list[dict[str, Any]]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if rows:
        facts["conference_sightings"] = [
            {
                "conference": r["payload"].get("conference"),
                "date": r["payload"].get("date"),
                "role": r["payload"].get("role"),
            }
            for r in rows
        ]
    return facts


def _extract_domain_check(rows: list[dict[str, Any]]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if dom := _latest(rows):
        facts["domain"] = dom["payload"].get("domain")
        facts["domain_mx_present"] = dom["payload"].get("mx_present")
    return facts


def _extract_adv_name(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """ADV firm record (c2_form_adv.py). Reads the latest row's signals."""
    facts: dict[str, Any] = {}
    adv = _latest(rows)
    if adv is None:
        return facts
    signals = adv["payload"].get("signals") or {}
    facts["adv_present"] = True
    crd = signals.get("crd")
    if crd is not None:
        facts["adv_crd"] = crd
    raum = signals.get("raum_usd")
    if raum is not None:
        facts["adv_raum_usd"] = raum
    hnw = signals.get("hnw_clients")
    if hnw is not None:
        facts["adv_hnw_clients"] = hnw
    retail = signals.get("retail_clients")
    if retail is not None:
        facts["adv_retail_clients"] = retail
    status = signals.get("status")
    if status is not None:
        facts["adv_status"] = status
    website = signals.get("website")
    if website is not None:
        facts["adv_website"] = website
    # adv_source_url comes from the row's url COLUMN, not a payload key.
    url = adv.get("url")
    if url is not None:
        facts["adv_source_url"] = url
    state = _first_payload_non_null(rows, "state")
    if state is not None:
        facts["hq_state"] = state
    address = _first_payload_non_null(rows, "address_raw")
    if address is not None:
        facts["hq_address_raw"] = address
    return facts


def _extract_fec_employer(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """FEC employer record. FEC fans out one row per contribution, so this aggregates
    across ALL rows of the class rather than reading only the latest."""
    facts: dict[str, Any] = {}
    if not rows:
        return facts
    contribution_counts: list[Any] = []
    distinct_contributors: list[Any] = []
    first_contributions: list[str] = []
    last_contributions: list[str] = []
    states_seen: set[str] = set()
    for r in rows:
        signals = (r.get("payload") or {}).get("signals") or {}
        cc = signals.get("contribution_count")
        if cc is not None:
            contribution_counts.append(cc)
        dc = signals.get("distinct_contributors")
        if dc is not None:
            distinct_contributors.append(dc)
        fc = signals.get("first_contribution")
        if fc:
            first_contributions.append(fc)
        lc = signals.get("last_contribution")
        if lc:
            last_contributions.append(lc)
        for st in (signals.get("states_seen") or []):
            if isinstance(st, str):
                states_seen.add(st)
    if contribution_counts:
        facts["fec_contribution_count"] = max(contribution_counts)
    if distinct_contributors:
        facts["fec_distinct_contributors"] = max(distinct_contributors)
    if first_contributions:
        facts["fec_first_contribution"] = min(first_contributions)
    if last_contributions:
        facts["fec_last_contribution"] = max(last_contributions)
    if states_seen:
        facts["fec_states_seen"] = sorted(states_seen)
    latest = _latest(rows)
    if latest is not None and latest.get("url") is not None:
        facts["fec_source_url"] = latest.get("url")
    state = _first_payload_non_null(rows, "state")
    if state is not None:
        facts["hq_state"] = state
    address = _first_payload_non_null(rows, "address_raw")
    if address is not None:
        facts["hq_address_raw"] = address
    return facts


DISCOVERY_CLASS_EXTRACTORS = {
    "adv_index": _extract_adv_index,
    "13f_filing": _extract_13f_filing,
    "5500_filing": _extract_5500_filing,
    "conference_sighting": _extract_conference_sighting,
    "domain_check": _extract_domain_check,
    "adv_name": _extract_adv_name,
    "fec_employer": _extract_fec_employer,
}


def compute_injected_facts(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate entity_sources rows into the injected-facts dict (plan §4.1 step 2)."""
    facts: dict[str, Any] = {}
    by_class: dict[str, list[dict[str, Any]]] = {}
    for s in sources:
        by_class.setdefault(s["source_class"], []).append(s)
    # Iterate the registry in its fixed insertion order, not the order rows happen to
    # come back from the DB. Three keys are written by more than one extractor
    # (adv_present: adv_index + adv_name; hq_state/hq_address_raw: adv_name +
    # fec_employer), so row-order iteration would resolve collisions
    # non-deterministically. Registry order defines precedence deterministically.
    for cls, extractor in DISCOVERY_CLASS_EXTRACTORS.items():
        rows = by_class.get(cls)
        if rows:
            facts.update(extractor(rows))
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

    # G1.Q4 from an adv_name row (c2_form_adv.py connector output). Settle SFO/MFO from
    # ADV registration when the evidence is unambiguous: an Approved/120-Day-Approval
    # firm with HNW client count at or above the single-family ceiling is an MFO. Below
    # threshold, missing, or inactive -> emit nothing and let the researcher settle it.
    # Does not modify the adv_index branch above.
    adv_name_rows = [s for s in sources if s["source_class"] == "adv_name"]
    if (adv_name := _latest(adv_name_rows)) is not None:
        adv_signals = adv_name["payload"].get("signals") or {}
        adv_status = adv_signals.get("status")
        hnw_clients = adv_signals.get("hnw_clients")
        if (
            adv_status in ("Approved", "120-Day Approval")
            and isinstance(hnw_clients, (int, float))
            and not isinstance(hnw_clients, bool)
            and hnw_clients >= MAX_SINGLE_FAMILY_CLIENT_RECORDS
        ):
            claims.append(
                Claim(
                    question_id="G1.Q4",
                    answer=f"MFO (ADV-registered, {hnw_clients} HNW clients)",
                    status="confirmed",
                    source_url=adv_name.get("url"),
                    source_class="adv_name",
                    retrieved_at=_parse_dt(adv_name["retrieved_at"]),
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
    unverified_leads = _build_unverified_leads(sources)

    brief = LeadBrief(
        entity_id=entity_id,
        canonical_name=entity["canonical_name"],
        aliases=entity["aliases"],
        injected_facts=injected_facts,
        unverified_leads=unverified_leads,
        questions=list(QUESTION_BATTERY),
        budget=budget or LeadBudget(),
    )
    return brief, pre_answered


# source_classes whose rows are already pre-answered facts (T18.3). Rows in any
# OTHER class contribute search-seed leads instead, via _build_unverified_leads.
_PREANSWERED_SOURCE_CLASSES = (
    "13f_filing",
    "5500_filing",
    "adv_index",
    "conference_sighting",
    "domain_check",
)


def _build_unverified_leads(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Discovery rows that are search seeds, not facts. Each person named in a
    non-preanswered class's `people` list yields a person lead; an adv_name row's
    signals.website yields a website lead. Dedup on (kind, value), first-seen order,
    capped at 10."""
    leads: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in sources:
        cls = row.get("source_class")
        if cls in _PREANSWERED_SOURCE_CLASSES:
            continue
        payload = row.get("payload") or {}
        url = row.get("url")
        if cls == "adv_name":
            website = (payload.get("signals") or {}).get("website")
            if website:
                key = ("website", str(website))
                if key not in seen:
                    seen.add(key)
                    leads.append(
                        {
                            "kind": "website",
                            "value": website,
                            "title": None,
                            "source_class": "adv_name",
                            "source_url": url,
                        }
                    )
        for person in (payload.get("people") or []):
            if not isinstance(person, dict):
                continue
            name = person.get("name")
            if not name:
                continue
            key = ("person", str(name))
            if key not in seen:
                seen.add(key)
                leads.append(
                    {
                        "kind": "person",
                        "value": name,
                        "title": person.get("title"),
                        "source_class": cls,
                        "source_url": url,
                    }
                )
    return leads[:10]


def parser_node(state: SupervisorState, *, conn: sqlite3.Connection) -> dict[str, Any]:
    """LangGraph node entrypoint. `conn` is bound via functools.partial when the graph
    is assembled (app/graph.py) since node functions take only `state`."""
    brief, pre_answered = build_lead_brief(conn, state["entity_id"])
    return {
        "lead_brief": brief,
        "claims": [c.model_dump(mode="json") for c in pre_answered],
    }

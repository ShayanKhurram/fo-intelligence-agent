"""Maps the FO Intelligence Agent pipeline's real output (data/foia.db's `claims` +
`enrichment_runs` tables) into micro_rag_plan.md §3.1's `records` row shape. Pure
functions, no DB writes here — see ingest.py for the write path. This is the boundary
between "our pipeline's schema" and "the micro-RAG's schema"; kept as its own module so
that boundary is a single, auditable place."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any


def _latest_by_field(claims: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for c in claims:
        key = c.get("field_name")
        if key:
            latest[key] = c  # last write wins, same convention as app.dataset
    return latest


_UNRELIABLE_STATUSES = ("could_not_verify", "removed_failed_validation", "contradicted")


def _settled_value(claims_by_field: dict[str, dict[str, Any]], field: str) -> Any:
    c = claims_by_field.get(field)
    if c is None or c["status"] in _UNRELIABLE_STATUSES:
        return None
    return c["answer"]


def _settled_status(claims_by_field: dict[str, dict[str, Any]], field: str) -> str:
    c = claims_by_field.get(field)
    return c["status"] if c is not None else "could_not_verify"


def _parse_check_size_range(raw: Any) -> tuple[int | None, int | None]:
    """check_size_range is free text (e.g. "$1M-$5M", "$500K to $2M") — best-effort
    numeric parse; returns (None, None) rather than guessing on unparseable text."""
    if not raw or not isinstance(raw, str):
        return None, None

    def _to_number(token: str) -> int | None:
        token = token.strip().upper().replace("$", "").replace(",", "")
        m = re.match(r"^([\d.]+)\s*([KMB]?)$", token)
        if not m:
            return None
        value = float(m.group(1))
        mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "": 1}[m.group(2)]
        return int(value * mult)

    parts = re.split(r"-|to", raw, maxsplit=1)
    if len(parts) != 2:
        return None, None
    return _to_number(parts[0]), _to_number(parts[1])


def _type_final(claims_by_field: dict[str, dict[str, Any]], claims: list[dict[str, Any]]) -> str:
    g1q4 = next((c for c in claims if c.get("question_id") == "G1.Q4"), None)
    if g1q4 is None or g1q4["status"] in ("could_not_verify", "removed_failed_validation", "contradicted"):
        return "type_unconfirmed"
    ans = str(g1q4["answer"]).upper()
    if "MFO" in ans:
        return "MFO"
    if "SFO" in ans:
        return "SFO"
    return "type_unconfirmed"


def _most_recent_signal_date(claims: list[dict[str, Any]]) -> date | None:
    """Latest retrieved_at across every dated-signal claim still standing — the plan's
    `most_recent_signal_date` column. Never fabricated: None if nothing dated survives."""
    signal_fields = {"why_now_trigger", "recent_investments", "recent_fund_commitments", "recent_key_hires", "recent_news"}
    dates: list[date] = []
    for c in claims:
        if c.get("field_name") not in signal_fields:
            continue
        if c["status"] in ("could_not_verify", "removed_failed_validation"):
            continue
        raw = c.get("retrieved_at")
        if not raw:
            continue
        try:
            dates.append(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date())
        except ValueError:
            continue
    return max(dates) if dates else None


def _urgency_tier(claims_by_field: dict[str, dict[str, Any]]) -> str | None:
    trigger = _settled_value(claims_by_field, "why_now_trigger")
    if trigger is None:
        return None
    return {"fresh_liquidity": "high", "concentration_pain": "high", "access_window": "medium"}.get(str(trigger), "low")


def _record_confidence(claims: list[dict[str, Any]]) -> str:
    """A blunt overall signal: does the record have any cross-class VERIFIED claim, or
    is everything single_source/format_only? Never a fabricated precision number —
    just the honest shape of the evidence backing this record."""
    statuses = {c["status"] for c in claims if c.get("field_name")}
    if "verified" in statuses:
        return "verified_evidence_present"
    if statuses & {"single_source", "confirmed", "format_only"}:
        return "single_source_only"
    return "thin"


def build_record(entity_id: str, canonical_name: str, outcome: str, claims: list[dict[str, Any]],
                  hq_state: str | None = None) -> dict[str, Any]:
    """Maps one entity's real claim ledger to micro_rag_plan.md §3.1's `records` row.
    Every field is either a real settled value or None/could_not_verify — nothing here
    invents a value the pipeline didn't already produce."""
    by_field = _latest_by_field(claims)

    mandates_raw = _settled_value(by_field, "investing_mandates")
    fit_tags_raw = _settled_value(by_field, "fit_tags")
    check_min, check_max = _parse_check_size_range(_settled_value(by_field, "check_size_range"))
    overlap_raw = _settled_value(by_field, "public_list_overlap")

    return {
        "record_id": entity_id,
        "entity_name": canonical_name,
        "entity_type": _type_final(by_field, claims),
        "hq_state": hq_state,
        "hq_country": "US",
        "aum_usd": _settled_value(by_field, "aum_usd"),
        "aum_basis": _settled_value(by_field, "aum_basis"),
        "aum_as_of": _settled_value(by_field, "aum_as_of"),
        "mandates": mandates_raw if isinstance(mandates_raw, list) else ([mandates_raw] if mandates_raw else []),
        "fit_tags": fit_tags_raw if isinstance(fit_tags_raw, list) else ([fit_tags_raw] if fit_tags_raw else []),
        "check_size_min": check_min,
        "check_size_max": check_max,
        "principal_name": _settled_value(by_field, "principal_name"),
        "principal_title": _settled_value(by_field, "principal_title"),
        "principal_email": _settled_value(by_field, "principal_email"),
        "principal_email_status": _settled_status(by_field, "principal_email"),
        "principal_phone": _settled_value(by_field, "principal_phone"),
        "principal_phone_status": _settled_status(by_field, "principal_phone"),
        "most_recent_signal_date": _most_recent_signal_date(claims),
        "urgency_tier": _urgency_tier(by_field),
        "activity_status": "active" if _settled_value(by_field, "recent_investments") else None,
        "actionability_score": None,  # computed by app.dataset._score_claims at selection time; not re-derived here
        "discovery_class_primary": next(
            (c["field_name"][len("discovery_class_"):] for c in sorted(claims, key=lambda c: c.get("field_name") or "")
             if (c.get("field_name") or "").startswith("discovery_class_")),
            None,
        ),
        "public_list_overlap": overlap_raw if isinstance(overlap_raw, list) else ([overlap_raw] if overlap_raw else []),
        "record_confidence": _record_confidence(claims),
        "outcome": outcome,
    }

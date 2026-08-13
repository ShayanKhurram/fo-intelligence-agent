"""Layer D — Dataset assembly (enrichment_validation_dataset_plan.md §6). Selection
(quota.py's logic, §6.1) -> six-sheet assembly (§6.2) -> XLSX/CSV/manifest emission
(§6.3). Everything reads from the `claims` spine (app/db.py) — "the records sheet is
literally a pivot of the ledger" (plan §6.2).

No scoring formula for `actionability_score` / `urgency_tier_rank` is specified
anywhere in this repo (the plan's §6.1 pseudocode references them as already-computed
fields on a `survivors` object that doesn't exist elsewhere) — `_score_claims` and
`_urgency_tier_rank` below are a documented, defensible proxy: see their docstrings.
"""
from __future__ import annotations

import csv
import json
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from app.db import (
    get_audit_rejected_values,
    get_claims,
    get_entity,
    get_findings,
    get_rejections,
    write_production_record,
)
from app.validation import _MULTI_VALUED_FIELDS, decide_type_final

MAX_PER_CLASS = 15  # 30% of 50 — plan §6.1

_STATUS_WEIGHTS: dict[str, int] = {
    "verified": 3, "confirmed": 2, "single_source": 2, "format_only": 1, "pattern_inferred": 1,
}

# Column order: identity -> type -> principal contact -> why-now -> mandates -> signals
# -> integrity (plan §6.3). Any field present in the ledger but not listed here is
# appended at the end, never dropped.
_COLUMN_ORDER: list[str] = [
    # identity
    "entity_id", "canonical_name", "domain",
    # type
    "type_final", "aum_usd", "aum_basis", "aum_as_of", "headcount",
    # principal contact
    "principal_name", "principal_title", "principal_email", "principal_phone",
    "principal_linkedin", "corporate_linkedin",
    "secondary_contact_name", "secondary_contact_email", "secondary_contact_phone",
    # why-now
    "important_insight", "recent_investments", "recent_fund_commitments",
    "recent_key_hires", "recent_news",
    # mandates
    "investing_mandates", "investing_thesis", "sector_focus", "stage_focus",
    "geography_focus", "check_size_range", "direct_vs_fund", "do_not_pitch",
    "fit_tags", "background",
    # integrity
    "public_list_overlap",
]

_HIGH_VALUE_FIELDS: frozenset[str] = frozenset({
    "principal_name", "principal_email", "principal_phone", "aum_usd", "important_insight",
})

# T26 — provenance tiers for multi-valued fields. An extraction_method PREFIX maps to a
# rank; lower is better. `_provenance_rank` does longest-prefix match. The point: a
# researcher-verified `projected_G2.Q1` principal must not be concatenated in the same
# cell as `derived_fec_employer` donors (campaign contributors who named the firm as their
# employer). `projected_` is a Layer-1 researched, source-cited finding; `derived_` is a raw
# discovery-feed payload value; everything else (serper_xray, serper_organic, jsonld,
# site_scrape, and anything unrecognised) is a search-snippet / scrape tier. `None` ranks
# 2 — unknown provenance is not evidence of quality.
_PROVENANCE_TIER: dict[str, int] = {
    "projected_": 0,
    "derived_": 1,
}


def _provenance_rank(extraction_method: str | None) -> int:
    """Longest-prefix match against `_PROVENANCE_TIER`; unmatched and `None` -> 2."""
    if extraction_method is None:
        return 2
    best = 2
    best_prefix_len = -1
    for prefix, rank in _PROVENANCE_TIER.items():
        if extraction_method.startswith(prefix) and len(prefix) > best_prefix_len:
            best = rank
            best_prefix_len = len(prefix)
    return best


@dataclass
class ProductionCandidate:
    entity_id: str
    canonical_name: str
    type_final: str
    outcome: str
    claims: list[dict[str, Any]] = field(default_factory=list)
    actionability_score: float = 0.0
    verified_cell_count: int = 0
    urgency_tier_rank: int = 0
    discovery_class_primary: str | None = None
    discovery_class_count: int = 0


def _score_claims(claims: list[dict[str, Any]]) -> tuple[float, int]:
    """actionability_score + verified_cell_count. Sum of per-status weights across
    every field-bearing (field_name is not None) settled claim — "verified" (cross-
    class corroborated) outweighs a merely "confirmed"/"single_source" claim, since
    that's the entire point of the cross-class rule (plan §5): a record with more
    independently-corroborated cells is more actionable than one with the same cell
    count all from one source."""
    score = 0.0
    verified = 0
    for c in claims:
        if not c.get("field_name"):
            continue
        score += _STATUS_WEIGHTS.get(c["status"], 0)
        if c["status"] == "verified":
            verified += 1
    return score, verified


def _urgency_tier_rank(claims: list[dict[str, Any]]) -> int:
    """0 = no dated signal at all, 1 = a low-confidence dated signal only, 2 = a
    medium/high-confidence dated signal. Used purely as a selection tiebreaker."""
    signal_fields = {"important_insight", "recent_investments", "recent_fund_commitments"}
    signals = [
        c for c in claims
        if c.get("field_name") in signal_fields and c["status"] not in ("could_not_verify", "removed_failed_validation")
    ]
    if not signals:
        return 0
    return 2 if any(c.get("confidence") in ("high", "medium") for c in signals) else 1


def _discovery_class_info(claims: list[dict[str, Any]]) -> tuple[str | None, int]:
    classes = sorted(
        c["field_name"][len("discovery_class_"):]
        for c in claims
        if (c.get("field_name") or "").startswith("discovery_class_")
    )
    return (classes[0] if classes else None, len(classes))


def build_candidate(entity: dict[str, Any], claims: list[dict[str, Any]], outcome: str, type_final: str | None = None) -> ProductionCandidate:
    # type_final defaults to None = "derive it from the ledger", distinct from a
    # literal "type_unconfirmed" = "we decided it is unconfirmed". Layer V (which
    # has already run the rule and persisted nothing) can still pass an explicit
    # value and have it win — that path is what the existing tests exercise.
    if type_final is None:
        type_final = decide_type_final(claims)
    score, verified = _score_claims(claims)
    primary, count = _discovery_class_info(claims)
    return ProductionCandidate(
        entity_id=entity["entity_id"], canonical_name=entity["canonical_name"],
        type_final=type_final, outcome=outcome, claims=claims,
        actionability_score=score, verified_cell_count=verified,
        urgency_tier_rank=_urgency_tier_rank(claims),
        discovery_class_primary=primary, discovery_class_count=count,
    )


def select_50(
    survivors: list[ProductionCandidate], n: int = 50, max_per_class: int = MAX_PER_CLASS
) -> tuple[list[ProductionCandidate], dict[str, int], list[ProductionCandidate]]:
    """§6.1 — ranked, then quota-bound. Returns (selected, per_class_counts,
    excluded_by_quota). `excluded_by_quota` is returned (not just logged) so the caller
    can persist it to `production_records` — "log it — 'excluded to preserve source
    diversity' is a defensible, documented judgment call" (plan §6.1)."""
    ranked = sorted(
        survivors,
        key=lambda r: (
            r.actionability_score,
            r.type_final == "SFO",
            r.verified_cell_count,
            r.urgency_tier_rank,
            r.discovery_class_count,
        ),
        reverse=True,
    )
    selected: list[ProductionCandidate] = []
    excluded: list[ProductionCandidate] = []
    per_class: Counter[str] = Counter()
    for r in ranked:
        if len(selected) >= n:
            break
        cls = r.discovery_class_primary or "unknown"
        if per_class[cls] >= max_per_class:
            excluded.append(r)
            continue
        selected.append(r)
        per_class[cls] += 1
    return selected, dict(per_class), excluded


def persist_selection(
    conn, selected: list[ProductionCandidate], excluded: list[ProductionCandidate]
) -> None:
    for rank, r in enumerate(selected, start=1):
        write_production_record(conn, r.entity_id, rank=rank, primary_class=r.discovery_class_primary,
                                 excluded_by_quota=False)
    for r in excluded:
        write_production_record(conn, r.entity_id, rank=None, primary_class=r.discovery_class_primary,
                                 excluded_by_quota=True)


def gather_survivors(conn, entity_outcomes: list[tuple[str, str] | tuple[str, str, str]]) -> list[ProductionCandidate]:
    """`entity_outcomes` is [(entity_id, outcome[, type_final]), ...] for ship/
    ship_with_caveats entities — the caller (app.enrichment.run_pipeline's summary,
    or a fresh DB scan) is the source of truth for which entities reached which
    outcome, since that isn't itself persisted as a queryable column anywhere. A
    2-tuple (entity_id, outcome) means "derive type_final from the ledger"; a
    3-tuple supplies it explicitly and wins. Either way `type_final` is now produced
    from the claim ledger, never invented by the caller — the column used to ship
    empty because no persisted value existed and every caller had to guess."""
    candidates = []
    for item in entity_outcomes:
        entity_id, outcome = item[0], item[1]
        type_final = item[2] if len(item) > 2 else None
        entity = get_entity(conn, entity_id)
        if entity is None:
            continue
        claims = get_claims(conn, entity_id)
        candidates.append(build_candidate(entity, claims, outcome, type_final))
    return candidates


# ============================================================================
# 6.2 — sheet assembly
# ============================================================================


def _records_rows(selected: list[ProductionCandidate]) -> tuple[list[str], list[dict[str, Any]]]:
    """One row per entity, one column per field_name, plus `<field>_status` and
    `<field>_source_class` companion columns for every high-value field — plan §6.3:
    "populated, or blank with could_not_verify status column. Never silently empty."

    Two things this function gets deliberately right, both learned the hard way:

    1. **Every high-value field ALWAYS gets its columns**, even when no selected record has
       a claim for it. Columns used to be derived purely from fields present in the ledger,
       so when none of the shipped records had an email the `principal_email` and
       `principal_email_status` columns vanished from the sheet entirely. That is exactly
       the "silently empty" outcome the plan forbids — and it is worse than a blank cell,
       because a consumer cannot distinguish "we looked and could not verify it" from "this
       field is not part of the dataset". Observed on the current 5-record output, where
       0/5 records carried any contact field.

    2. **`<field>_source_class` sits next to the value**, so a reader can judge a cell
       without cross-referencing the `provenance` sheet. A `principal_email` sourced from
       `snov` is a different proposition from one scraped off `site_scrape`, and the whole
       point of the claim ledger is that the distinction stays attached to the value.
       (`provenance` remains the full audit trail — every field, plus confirming_url,
       confirming_class and verification_method.)
    """
    present_fields: set[str] = set()
    for c in selected:
        present_fields.update(cl["field_name"] for cl in c.claims if cl.get("field_name"))

    # High-value fields are guaranteed columns whether or not anyone has one.
    all_fields = present_fields | set(_HIGH_VALUE_FIELDS)
    ordered = [f for f in _COLUMN_ORDER if f in all_fields]
    ordered += sorted(all_fields - set(ordered))

    columns: list[str] = ["entity_id", "canonical_name", "type_final", "lead_origin_source_class", "outcome"]
    for f in ordered:
        if f in ("entity_id", "canonical_name", "type_final", "lead_origin_source_class"):
            continue
        columns.append(f)
        if f in _HIGH_VALUE_FIELDS:
            columns.append(f"{f}_status")
            columns.append(f"{f}_source_class")

    rows = []
    for c in selected:
        latest: dict[str, dict[str, Any]] = {}
        # Multi-valued field -> {field: {rank: [answers in first-seen order]}}. T26: build
        # the cell from ONLY the best (lowest) rank present for that field, not from
        # every eligible claim — otherwise a researcher-verified `projected_G2.Q1`
        # principal gets concatenated in the same cell as `derived_fec_employer` donors.
        multi: dict[str, dict[int, list[str]]] = {}
        for cl in c.claims:
            fname = cl.get("field_name")
            if not fname:
                continue
            latest[fname] = cl  # last write wins, same convention as elsewhere
            # Multi-valued fields must not lose their other values to last-write-wins: a
            # firm with two co-managing members has two real principal_name claims, and
            # keeping only the last one silently discards a decision-maker. See
            # app.validation._MULTI_VALUED_FIELDS for why V4 no longer calls these a
            # contradiction.
            if fname in _MULTI_VALUED_FIELDS and cl.get("status") not in (
                "removed_failed_validation", "superseded",
            ):
                answer = cl.get("answer")
                if answer not in (None, ""):
                    rank = _provenance_rank(cl.get("extraction_method"))
                    bucket = multi.setdefault(fname, {}).setdefault(rank, [])
                    if str(answer) not in bucket:
                        bucket.append(str(answer))
        # lead_origin_source_class is a candidate-level attribute (which discovery feed
        # this lead came from), NOT a claim field_name — so it follows the type_final
        # precedent: a literal header entry + a literal row entry, never derived from
        # the claim field_name pivot. Same value that drives the select_50 per-class
        # quota (c.discovery_class_primary); 'unknown' when no discovery_class_* claim.
        row: dict[str, Any] = {"entity_id": c.entity_id, "canonical_name": c.canonical_name,
                                "type_final": c.type_final,
                                "lead_origin_source_class": c.discovery_class_primary or "unknown",
                                "outcome": c.outcome}
        for f in ordered:
            if f in ("entity_id", "canonical_name", "type_final", "lead_origin_source_class"):
                continue
            claim = latest.get(f)
            if f in _MULTI_VALUED_FIELDS and multi.get(f):
                # T26: emit ONLY the best (lowest) tier present for this field, in
                # first-seen order within the tier. A "; " rather than a list so the
                # XLSX cell and the Postgres TEXT column both stay a plain string.
                # Co-principals arriving in the same tier both survive; a researcher's
                # projected principal is no longer buried among discovery-feed donors.
                tiers = multi[f]
                best = min(tiers)
                row[f] = "; ".join(tiers[best])
            else:
                row[f] = claim["answer"] if claim and claim["status"] != "removed_failed_validation" else None
            if f in _HIGH_VALUE_FIELDS:
                row[f"{f}_status"] = claim["status"] if claim else "could_not_verify"
                # A blanked value keeps its source_class: the release rule killed the value,
                # not the record of where it came from (the value itself is preserved in
                # audit_rejected_values).
                row[f"{f}_source_class"] = claim.get("source_class") if claim else None
        rows.append(row)
    return columns, rows


def _provenance_rows(selected: list[ProductionCandidate]) -> list[dict[str, Any]]:
    """One row per (record, field) — unpivoted, full provenance."""
    rows = []
    for c in selected:
        for cl in c.claims:
            if not cl.get("field_name"):
                continue
            rows.append({
                "entity_id": c.entity_id,
                "field_name": cl["field_name"],
                "answer": cl["answer"],
                "status": cl["status"],
                "source_url": cl.get("source_url"),
                "source_class": cl.get("source_class"),
                "extraction_method": cl.get("extraction_method"),
                "confidence": cl.get("confidence"),
                "verification_method": cl.get("verification_method"),
                "confirming_url": cl.get("confirming_url"),
                "confirming_class": cl.get("confirming_class"),
                "verified_at": cl.get("verified_at"),
            })
    return rows


def _source_class_report_rows(selected: list[ProductionCandidate]) -> list[dict[str, Any]]:
    per_class: dict[str, dict[str, Any]] = {}
    for c in selected:
        cls = c.discovery_class_primary or "unknown"
        bucket = per_class.setdefault(cls, {"discovery_class": cls, "entity_count": 0, "score_sum": 0.0})
        bucket["entity_count"] += 1
        bucket["score_sum"] += c.actionability_score
    rows = []
    for cls, bucket in sorted(per_class.items()):
        rows.append({
            "discovery_class": cls,
            "entity_count": bucket["entity_count"],
            "avg_actionability_score": round(bucket["score_sum"] / bucket["entity_count"], 2) if bucket["entity_count"] else 0,
        })
    return rows


_DATA_DICTIONARY: list[dict[str, str]] = [
    {"field": "aum_usd", "description": "Assets under management, USD floor derived from 13F tableValueTotal.", "inclusion_standard": "derived (wave -1); never fabricated if no 13F filing exists."},
    {"field": "aum_basis", "description": "How aum_usd was computed — currently always '13f_floor' (a floor, not a total AUM figure).", "inclusion_standard": "always paired with aum_usd."},
    {"field": "principal_name", "description": "Named decision-maker (principal, CIO, or similar).", "inclusion_standard": "wave 1 actionability-core field; a record without one is gated before wave 2 spend."},
    {"field": "principal_email", "description": "Contact email for the principal or the firm.", "inclusion_standard": "release rule: an email that fails verification is blanked, status='removed_failed_validation', logged to audit_rejected_values."},
    {"field": "principal_phone", "description": "Contact phone.", "inclusion_standard": "format-valid only unless independently corroborated — see status='format_only' vs 'verified'."},
    {"field": "important_insight", "description": "The reason this is a live opportunity now (concentration_pain | fresh_liquidity | access_window).", "inclusion_standard": "derived from 13F QoQ deltas or a future-dated conference sighting; never guessed."},
    {"field": "type_final", "description": "SFO | MFO | type_unconfirmed, from the G1.Q4 claim's final validated status.", "inclusion_standard": "type_unconfirmed if G1.Q4 was never settled or was contradicted."},
    {"field": "lead_origin_source_class", "description": "Discovery feed class this lead originally came from (13f_filing | fec_employer | 5500_filing | ppp_loans); 'unknown' if the lead carries no discovery_class_* claim.", "inclusion_standard": "derived from the discovery_class_* claims written by wave -1; the same value that drives the select_50 per-class quota."},
]

_STATUS_VOCAB_NOTES: list[dict[str, str]] = [
    {"status": "verified", "meaning": "Confirmed by >=2 independent, cross-class sources (a search-index hit alone never counts)."},
    {"status": "single_source", "meaning": "Confirmed, but only ever seen from one source_class — not independently corroborated."},
    {"status": "pattern_inferred", "meaning": "Format-plausible but unconfirmed (e.g. a pattern-guessed email)."},
    {"status": "format_only", "meaning": "Passes a format check only (e.g. phone number shape) — NOT line-verified."},
    {"status": "could_not_verify", "meaning": "Genuinely absent, or the tool that would have found it was unavailable — see extraction_method."},
    {"status": "removed_failed_validation", "meaning": "Killed by the release rule; original value is in audit_rejected_values, not here."},
]


# ============================================================================
# 6.3 — emission
# ============================================================================


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent.parent, timeout=5,
        ).decode().strip()
    except Exception:
        return "unknown"


def write_workbook(
    conn,
    selected: list[ProductionCandidate],
    per_class: dict[str, int],
    excluded: list[ProductionCandidate],
    output_dir: str | Path,
    *,
    budget_spent: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Writes the six-sheet XLSX, records.csv, and run_manifest.json to `output_dir`.
    Returns {"xlsx": path, "csv": path, "manifest": path}."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    columns, record_rows = _records_rows(selected)
    provenance_rows = _provenance_rows(selected)
    audit_rows = get_audit_rejected_values(conn)
    rejected_rows = get_rejections(conn)
    source_class_rows = _source_class_report_rows(selected)

    wb = Workbook()
    ws = wb.active
    ws.title = "records"
    ws.append(columns)
    for row in record_rows:
        ws.append([row.get(col) for col in columns])

    ws2 = wb.create_sheet("provenance")
    prov_cols = ["entity_id", "field_name", "answer", "status", "source_url", "source_class",
                 "extraction_method", "confidence", "verification_method", "confirming_url",
                 "confirming_class", "verified_at"]
    ws2.append(prov_cols)
    for row in provenance_rows:
        ws2.append([_stringify(row.get(c)) for c in prov_cols])

    ws3 = wb.create_sheet("audit_rejected_values")
    audit_cols = ["entity_id", "field_name", "rejected_value", "reason_code", "evidence_url", "rejected_at"]
    ws3.append(audit_cols)
    for row in audit_rows:
        ws3.append([_stringify(row.get(c)) for c in audit_cols])

    ws4 = wb.create_sheet("rejected_records")
    rej_cols = ["entity_id", "stage", "reason_code", "created_at"]
    ws4.append(rej_cols)
    for row in rejected_rows:
        ws4.append([row.get(c) for c in rej_cols])
    for r in excluded:
        ws4.append([r.entity_id, "quota", "excluded to preserve source diversity", None])

    ws5 = wb.create_sheet("source_class_report")
    sc_cols = ["discovery_class", "entity_count", "avg_actionability_score"]
    ws5.append(sc_cols)
    for row in source_class_rows:
        ws5.append([row.get(c) for c in sc_cols])
    ws5.append([])
    ws5.append(["quota cap per class", MAX_PER_CLASS])
    for cls, count in sorted(per_class.items()):
        ws5.append([f"selected: {cls}", count])

    ws6 = wb.create_sheet("data_dictionary")
    ws6.append(["field", "description", "inclusion_standard"])
    for row in _DATA_DICTIONARY:
        ws6.append([row["field"], row["description"], row["inclusion_standard"]])
    ws6.append([])
    ws6.append(["status", "meaning", ""])
    for row in _STATUS_VOCAB_NOTES:
        ws6.append([row["status"], row["meaning"], ""])

    xlsx_path = output_dir / "production_dataset.xlsx"
    wb.save(xlsx_path)

    csv_path = output_dir / "records.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in record_rows:
            writer.writerow({k: _stringify(v) for k, v in row.items()})

    manifest = {
        "git_sha": _git_sha(),
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "selected_count": len(selected),
        "excluded_by_quota_count": len(excluded),
        "per_class_counts": per_class,
        "budget_spent": budget_spent or {},
    }
    manifest_path = output_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    return {"xlsx": str(xlsx_path), "csv": str(csv_path), "manifest": str(manifest_path)}


def _stringify(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return value

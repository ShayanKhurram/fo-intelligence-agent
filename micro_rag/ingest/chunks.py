"""Facet chunk builder — micro_rag_plan.md §3.2. Six natural-language chunks per
record (identity, mandate, people, activity, why_now, summary), never generic sliding
windows: this is a record-oriented dataset, and a mid-field chunk severs a claim from
its provenance.

**Contact fields are never embedded** (plan §3.2's explicit, load-bearing rule):
`principal_email`/`principal_phone` must never appear in ANY chunk's `content` string.
`_assert_no_contact_leak` enforces this in code, not just by convention, since this is
"the single worst failure this product could have" per the plan's own words.
"""
from __future__ import annotations

from typing import Any


def _assert_no_contact_leak(text: str, record: dict[str, Any]) -> None:
    for field in ("principal_email", "principal_phone"):
        value = record.get(field)
        if value and str(value).strip() and str(value) in text:
            raise ValueError(f"contact field {field!r} leaked into an embedded chunk — refusing to build it")


def _fmt(value: Any, default: str = "not confirmed") -> str:
    if value is None or value == "" or value == []:
        return default
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _identity_chunk(record: dict[str, Any]) -> str:
    parts = [
        f"{record['entity_name']} is a {_fmt(record['entity_type'], 'family office of unconfirmed type')}.",
    ]
    if record.get("hq_state"):
        parts.append(f"Headquartered in {record['hq_state']}, {record.get('hq_country') or 'US'}.")
    if record.get("aum_usd"):
        basis = f" ({record['aum_basis']})" if record.get("aum_basis") else ""
        parts.append(f"Assets under management: approximately ${record['aum_usd']:,}{basis}.")
    if record.get("public_list_overlap"):
        parts.append(f"Appears on public family-office lists: {_fmt(record['public_list_overlap'])}.")
    return " ".join(parts)


def _mandate_chunk(record: dict[str, Any]) -> str:
    parts = [f"{record['entity_name']}'s investing mandate."]
    if record.get("mandates"):
        parts.append(f"Focus areas: {_fmt(record['mandates'])}.")
    if record.get("fit_tags"):
        parts.append(f"Fit tags: {_fmt(record['fit_tags'])}.")
    if record.get("check_size_min") or record.get("check_size_max"):
        lo = f"${record['check_size_min']:,}" if record.get("check_size_min") else "unspecified"
        hi = f"${record['check_size_max']:,}" if record.get("check_size_max") else "unspecified"
        parts.append(f"Typical check size: {lo} to {hi}.")
    if len(parts) == 1:
        parts.append("No investing thesis or mandate details have been confirmed for this record.")
    return " ".join(parts)


def _people_chunk(record: dict[str, Any]) -> str:
    """Never includes principal_email/principal_phone — those stay structural-only."""
    if not record.get("principal_name"):
        return f"No named decision-maker has been confirmed for {record['entity_name']}."
    parts = [f"{record['principal_name']} is the named decision-maker at {record['entity_name']}."]
    if record.get("principal_title"):
        parts.append(f"Title: {record['principal_title']}.")
    return " ".join(parts)


def _activity_chunk(record: dict[str, Any]) -> str:
    if not record.get("activity_status") and not record.get("most_recent_signal_date"):
        return f"No recent activity signal has been confirmed for {record['entity_name']}."
    parts = [f"{record['entity_name']} activity."]
    if record.get("most_recent_signal_date"):
        parts.append(f"Most recent dated signal: {record['most_recent_signal_date']}.")
    return " ".join(parts)


def _why_now_chunk(record: dict[str, Any]) -> str:
    if not record.get("urgency_tier"):
        return f"No specific why-now trigger has been confirmed for {record['entity_name']}."
    return f"{record['entity_name']} has an urgency tier of {record['urgency_tier']}, based on a dated signal in the record."


def _summary_chunk(record: dict[str, Any]) -> str:
    aum_text = f"${record['aum_usd']:,}" if record.get("aum_usd") else "not confirmed"
    return (
        f"{record['entity_name']} ({_fmt(record['entity_type'])}) — "
        f"AUM {aum_text}, "
        f"principal {_fmt(record.get('principal_name'))}, "
        f"record confidence: {record.get('record_confidence')}."
    )


_FACET_BUILDERS = {
    "identity": _identity_chunk,
    "mandate": _mandate_chunk,
    "people": _people_chunk,
    "activity": _activity_chunk,
    "why_now": _why_now_chunk,
    "summary": _summary_chunk,
}


def build_chunks(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns [{"chunk_id","facet","content"}] — 6 chunks per record. Raises if a
    contact field leaks into any chunk's text (see _assert_no_contact_leak)."""
    chunks = []
    for facet, builder in _FACET_BUILDERS.items():
        content = builder(record)
        _assert_no_contact_leak(content, record)
        chunks.append({
            "chunk_id": f"{record['record_id']}::{facet}",
            "record_id": record["record_id"],
            "facet": facet,
            "content": content,
        })
    return chunks

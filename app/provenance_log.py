"""T35.4 — the provenance log builder (PLAN.md T35). Read-only over the DB: composes,
for one lead x output field, a JSON record explaining exactly how that cell's value was
obtained — which wave, which tool call, which URL, which validation checks, what else
was found and rejected, or WHY the cell is blank. Rendered on a web page later, so the
output is JSON that survives ``json.dumps`` with no custom encoder.

The non-negotiable correctness property (PLAN.md T35): the log's ``value`` for a cell
must be the same value the row shipped. So ``value`` / ``status`` / ``alternatives``
come from ``app.dataset.resolve_cell`` — the ONE function the row also uses — never
re-derived here. This is the same no-drift move T27.2 made by importing
``_provenance_rank`` rather than re-implementing it.

This module WRITES NOTHING. No LLM, no network. T35.5 persists the records this builds.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from app.db import (
    get_audit_rejected_values,
    get_claims,
    get_entity,
    get_findings,
    get_run,
    get_tool_calls,
)
from app.dataset import _HIGH_VALUE_FIELDS, _COLUMN_ORDER, resolve_cell

# ---------------------------------------------------------------------------
# how.summary — a deterministic one-sentence template per extraction_method.
#
# NO LLM call, ever. A reader who does not know this codebase must be able to read the
# sentence and understand what was done. ``projected_<qid>`` is prefix-matched and names
# the Layer-1 question. An unknown method falls back to
# ``f"{extraction_method} via {source_class}"``. The lookup MUST NEVER raise — a missing
# key, a None source_class, an odd format arg all degrade to the fallback. Templates may
# reference winner fields via str.format; a SafeDict makes missing keys render empty
# instead of raising.
# ---------------------------------------------------------------------------

_METHOD_NARRATIVES: dict[str, str] = {
    "derived_13f": (
        "Assets under management derived as a floor from the firm's most recent 13F "
        "filing's reported portfolio value."
    ),
    "derived_13f_qoq": (
        "Assets under management derived from the quarter-over-quarter change in the "
        "firm's 13F filings."
    ),
    "adv_raum": (
        "Assets under management taken from the SEC ADV filing's Regulatory Assets Under "
        "Management (RAUM) figure."
    ),
    "derived_5500": (
        "Participant count derived from the firm's most recent Form 5500 annual report."
    ),
    "derived_conference": (
        "A conference sighting derived from a public conference attendee or speaker list."
    ),
    "derived_entity_sources": (
        "Derived from the entity's ingested discovery-feed source records."
    ),
    "derived_public_list_match": (
        "Derived by matching the firm against a public list of known family offices."
    ),
    "jsonld": (
        "The principal's name was parsed from the JSON-LD structured-data block on the "
        "firm's own website."
    ),
    "serper_xray": (
        "Found via a Serper Google search targeting the firm's domain (the x-ray query "
        "pattern)."
    ),
    "serper_news": "Found via a Serper Google News search.",
    "gdelt_docapi": "Found via the GDELT DOC 2.0 news API.",
    "snov_emails_by_name_domain": (
        "A Snov.io name-targeted email lookup returned this address for the principal on "
        "the firm's domain."
    ),
    "snov_domain_search": (
        "A Snov.io domain-wide email search returned this address for the firm's domain."
    ),
    "snov_no_match": (
        "A Snov.io email lookup ran but found no address for this principal on the firm's "
        "domain."
    ),
    "snov_error": (
        "A Snov.io email lookup failed (credentials missing or an API error)."
    ),
    "site_scrape": (
        "Scraped from the firm's own website via a headless-browser render."
    ),
    "httpx_trafilatura": (
        "Fetched from the firm's own website via a plain HTTP request and extracted with "
        "trafilatura."
    ),
    "llm_wave2_extraction": (
        "Extracted by the language model in enrichment wave 2 from fetched page content."
    ),
}

# Maps a winner's extraction_method to the raw tool functions that produce it, so a
# tool_calls row can be attributed to a field by method when no URL match is possible
# (e.g. a Serper search carries no result_url but the extraction_method names the tool).
_METHOD_TOOLS: dict[str, tuple[str, ...]] = {
    "serper_xray": ("serper_search_raw",),
    "serper_news": ("serper_search_raw",),
    "gdelt_docapi": ("news_search_raw",),
    "snov_emails_by_name_domain": ("snov_emails_by_name_domain_raw",),
    "snov_domain_search": ("snov_domain_search_raw",),
    "snov_no_match": ("snov_emails_by_name_domain_raw", "snov_domain_search_raw"),
    "snov_error": ("snov_emails_by_name_domain_raw", "snov_domain_search_raw"),
    "jsonld": ("fetch_raw_html",),
    "httpx_trafilatura": ("fetch_page_free_first", "fetch_raw_html"),
    "site_scrape": ("fetch_page_free_first", "fetch_raw_html"),
}


class _SafeDict(dict):
    """A dict that returns an empty string for any missing key, so str.format_map never
    raises KeyError on a template that references a field the winner does not carry."""

    def __missing__(self, key: str) -> str:  # noqa: D401
        return ""


def _host(url: str | None) -> str | None:
    """The host of a URL, casefolded with a leading 'www.' stripped — so
    'https://www.acme.com/x' and 'https://ACME.com/y' compare equal. None for a
    non-URL / empty string."""
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.casefold()
    if host.startswith("www."):
        host = host[4:]
    return host


def _method_summary(winner: dict[str, Any] | None) -> str:
    """Deterministic one-sentence narrative for how the winner was obtained. Never
    raises — every odd shape degrades to the fallback."""
    if winner is None:
        return ""
    method = winner.get("extraction_method")
    source_class = winner.get("source_class")
    if method and method.startswith("projected_"):
        qid = method[len("projected_"):]
        return f"Researched in Layer 1 as question {qid} and projected onto this field."
    template = _METHOD_NARRATIVES.get(method) if method else None
    if template is None:
        # Unknown method — degrade to the fallback. A None source_class yields just the
        # method name rather than "via None".
        return f"{method} via {source_class}" if source_class else (method or "")
    try:
        return template.format_map(_SafeDict(winner))
    except Exception:  # noqa: BLE001 — never raise on a template
        return template


def _matched_tool_calls(
    tool_calls: list[dict[str, Any]],
    winner: dict[str, Any] | None,
    *,
    cap: int = 20,
) -> list[dict[str, Any]]:
    """The tool_calls rows attributed to this field: matched by URL (the call's
    result_url equals the winner's source_url, or shares its host), else by method (the
    winner's extraction_method names the tool via _METHOD_TOOLS). Excluded otherwise.
    Newest first, capped at ``cap`` per field (a web page renders this list)."""
    if not winner:
        return []
    winner_url = winner.get("source_url")
    winner_host = _host(winner_url)
    method = winner.get("extraction_method")
    method_tools = _METHOD_TOOLS.get(method, ()) if method else ()
    matched: list[dict[str, Any]] = []
    # tool_calls are newest-first already (get_tool_calls orders by called_at DESC, id DESC)
    for tc in tool_calls:
        tc_url = tc.get("result_url")
        matched_by: str | None = None
        if tc_url and winner_url and (tc_url == winner_url or _host(tc_url) == winner_host):
            matched_by = "url"
        elif tc.get("tool") in method_tools:
            matched_by = "method"
        if matched_by is None:
            continue
        matched.append({
            "tool": tc.get("tool"),
            "args": tc.get("args"),
            "ok": tc.get("ok"),
            "cache_hit": tc.get("cache_hit"),
            "duration_ms": tc.get("duration_ms"),
            "called_at": tc.get("called_at"),
            "result_summary": tc.get("result_summary"),
            "matched_by": matched_by,
        })
        if len(matched) >= cap:
            break
    return matched


def _blank_reason(
    conn,
    entity_id: str,
    field: str,
    *,
    winner: dict[str, Any] | None,
    has_field_claim: bool,
    has_matched_tool_call: bool,
    run_tool_call_count: int = 0,
) -> dict[str, Any] | None:
    """Why a blank cell is blank, in precedence order (first match wins). Distinguishing
    'the tool failed' from 'there was nothing there' from 'nothing was ever tried' is a
    large part of why this log exists — all four states read identically as an empty
    cell on the sheet (PLAN.md T35).

    ``run_tool_call_count`` is the count of ALL tool calls the run made for this lead
    (not just the ones attributable to this field). It is what separates
    'never_attempted' (no calls ran at all) from 'searched_not_found' (calls ran but
    none matched this field) when there is no claim and no matched call — saying nothing
    was attempted when calls did run is exactly the false statement this log must not
    produce (D3)."""
    # 1. The release rule killed the value; the original value is in audit_rejected_values.
    if winner is not None and winner.get("status") == "removed_failed_validation":
        audit = get_audit_rejected_values(conn, entity_id)
        # newest matching row for this field — rows come back ordered by rejected_at
        row = next((r for r in audit if r.get("field_name") == field), None)
        if row is not None:
            return {
                "code": "removed_failed_validation",
                "detail": (
                    f"The value '{row.get('rejected_value')}' was found but blanked by "
                    f"the release rule ({row.get('reason_code')})."
                ),
                "rejected_value": row.get("rejected_value"),
                "reason_code": row.get("reason_code"),
            }
        return {
            "code": "removed_failed_validation",
            "detail": "The value was found but blanked by the release rule.",
            "rejected_value": None,
            "reason_code": None,
        }
    # 2. The tool that would have found it was unavailable (T32's tool_unavailable
    #    source_class), or the extraction method is an explicit _error form.
    method = winner.get("extraction_method") if winner else None
    source_class = winner.get("source_class") if winner else None
    if source_class == "tool_unavailable" or (method and method.endswith("_error")):
        detail_target = winner.get("source_url") or method or source_class or ""
        return {
            "code": "tool_unavailable",
            "detail": f"The tool that would have found this value was unavailable ({detail_target}).",
        }
    # 3. Something was attempted (a claim exists, or a matched tool call ran) but produced
    #    no usable value — a genuine "looked and could not verify".
    if has_field_claim or has_matched_tool_call:
        return {
            "code": "searched_not_found",
            "detail": "A search or lookup ran but found no usable value for this field.",
        }
    # 4. No claim and no matched tool call for this field. Say only what is known: if
    #    the run made tool calls for this lead (for other fields), this field was not
    #    attempted directly but the lead was worked — 'searched_not_found', not
    #    'never_attempted'. Only when the run made zero tool calls for the lead is it
    #    genuinely 'never_attempted'.
    if run_tool_call_count > 0:
        return {
            "code": "searched_not_found",
            "detail": (
                f"{run_tool_call_count} tool call(s) ran for this lead in this run, "
                f"none of them attributable to this field."
            ),
        }
    return {
        "code": "never_attempted",
        "detail": "No claim or tool call for this field exists — it was never attempted.",
    }


def _field_set(claims: list[dict[str, Any]], fields: list[str] | None) -> list[str]:
    """Every field present in the entity's claims, plus every high-value field (a
    high-value field that nobody ever touched must still appear, with
    blank_reason.code='never_attempted' — "never silently empty" is the plan's rule and
    the whole reason the columns are guaranteed in the sheet). ``fields=`` narrows it.
    Ordered by _COLUMN_ORDER then alphabetically, matching the sheet's column order."""
    present = {c["field_name"] for c in claims if c.get("field_name")}
    all_fields = present | set(_HIGH_VALUE_FIELDS)
    if fields is not None:
        all_fields = all_fields & set(fields)
    ordered = [f for f in _COLUMN_ORDER if f in all_fields]
    ordered += sorted(all_fields - set(ordered))
    return ordered


def build_field_records(
    conn,
    entity_id: str,
    *,
    run_id: str,
    fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """One schema_version-1 record per field for one entity, in the run ``run_id``. See
    PLAN.md T35 for the shape. Read-only over the DB. Never raises on a lead with zero
    claims — it returns one record per high-value field, each ``never_attempted``."""
    entity = get_entity(conn, entity_id)
    canonical_name = entity["canonical_name"] if entity else entity_id
    claims = get_claims(conn, entity_id)
    findings = get_findings(conn, entity_id)
    tool_calls = get_tool_calls(conn, entity_id, run_id)

    records: list[dict[str, Any]] = []
    for field in _field_set(claims, fields):
        res = resolve_cell(claims, field)
        winner = res.winner
        # D1: how/verification/confidence/status describe the PRODUCING claim, not the
        # last-write winner. For a multi-valued field the value can come from entirely
        # different claims than the winner (a researcher's projected principal winning
        # the cell while a later-written FEC donor is the row's status representative).
        # Attributing the shipped value to the winner would misattribute it to a claim
        # that did not produce it — the single failure mode this log exists to prevent.
        # Fall back to the winner only when producers is empty (a blanked or claim-less
        # cell), so a removed_failed_validation winner still reports its own status.
        source_claim = res.producers[0] if res.producers else winner
        status = source_claim["status"] if source_claim else "could_not_verify"
        shipped = res.value is not None
        # checks: findings on this field, or on the winner's claim (the row's status
        # representative — a finding attached to the last-write claim is still relevant
        # to the cell even when the value came from a different producing claim).
        winner_claim_id = winner.get("claim_id") if winner else None
        checks = [
            {"check_id": f.get("check_id"), "severity": f.get("severity"),
             "detail": f.get("detail"), "evidence_url": f.get("evidence_url")}
            for f in findings
            if f.get("field") == field or (winner_claim_id and f.get("claim_id") == winner_claim_id)
        ]
        # Tool calls are matched against the producing claim's source_url / extraction
        # method, so the call that actually fetched the value is the one attributed.
        matched_tc = _matched_tool_calls(tool_calls, source_claim)
        alternatives = [
            {"value": cl.get("answer"), "status": cl.get("status"),
             "source_class": cl.get("source_class"), "extraction_method": cl.get("extraction_method"),
             "why_not_used": code}
            for cl, code in res.alternatives
        ]
        blank_reason = None
        if not shipped:
            has_field_claim = any(c.get("field_name") == field for c in claims)
            blank_reason = _blank_reason(
                conn, entity_id, field,
                winner=winner,
                has_field_claim=has_field_claim,
                has_matched_tool_call=bool(matched_tc),
                run_tool_call_count=len(tool_calls),
            )
        # When a multi-valued cell has several producers (two co-principals), explain
        # each half after the first, so a two-principal cell attributes both sources.
        also_produced_by = [
            {
                "source_url": p.get("source_url"),
                "source_class": p.get("source_class"),
                "extraction_method": p.get("extraction_method"),
                "summary": _method_summary(p),
            }
            for p in res.producers[1:]
        ]
        records.append({
            "schema_version": 1,
            "run_id": run_id,
            "entity_id": entity_id,
            "canonical_name": canonical_name,
            "field": field,
            "value": res.value,
            "status": status,
            "confidence": source_claim.get("confidence") if source_claim else None,
            "shipped": shipped,
            "how": {
                "summary": _method_summary(source_claim),
                "produced_by": source_claim.get("produced_by") if source_claim else None,
                "wave": source_claim.get("wave") if source_claim else None,
                "question_id": source_claim.get("question_id") if source_claim else None,
                "extraction_method": source_claim.get("extraction_method") if source_claim else None,
                "source_class": source_claim.get("source_class") if source_claim else None,
                "source_url": source_claim.get("source_url") if source_claim else None,
                "retrieved_at": source_claim.get("retrieved_at") if source_claim else None,
                "also_produced_by": also_produced_by,
            },
            "verification": {
                "method": source_claim.get("verification_method") if source_claim else None,
                "confirming_url": source_claim.get("confirming_url") if source_claim else None,
                "confirming_class": source_claim.get("confirming_class") if source_claim else None,
                "verified_at": source_claim.get("verified_at") if source_claim else None,
            },
            "checks": checks,
            "alternatives": alternatives,
            "tool_calls": matched_tc,
            "blank_reason": blank_reason,
        })
    return records


def build_run_log(
    conn,
    run_id: str,
    entity_ids: list[str] | list[tuple[str, str]],
) -> dict[str, Any]:
    """The full schema_version-1 document for one run: the run row plus, per lead, that
    lead's field records. ``entity_ids`` may be a list of ids or of ``(entity_id,
    outcome)`` tuples — when only an id is given, ``outcome`` is None. An entity_id with
    no ``entities`` row is skipped (not raised on): the log describes real leads only."""
    leads: list[dict[str, Any]] = []
    for item in entity_ids:
        if isinstance(item, tuple):
            entity_id, outcome = item[0], item[1]
        else:
            entity_id, outcome = item, None
        entity = get_entity(conn, entity_id)
        if entity is None:
            continue
        leads.append({
            "entity_id": entity_id,
            "canonical_name": entity["canonical_name"],
            "outcome": outcome,
            "fields": build_field_records(conn, entity_id, run_id=run_id),
        })
    return {
        "schema_version": 1,
        "run": get_run(conn, run_id),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "leads": leads,
    }
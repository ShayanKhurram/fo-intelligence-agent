"""Layer E — Enrichment (enrichment_validation_dataset_plan.md §4). Ordered by
kill-power, not schema order: wave -1 (derive, free) -> wave 0 (gates, ~2 calls) ->
wave 1 (actionability core, ~6 calls) -> wave 2 (depth, ~8 calls, survivors only). This
module grows one wave at a time; each wave is a plain function over a claim list so the
reserve-pool orchestrator (§4 "Reserve pool control flow") can call into any of them
independently.

All Claims this module produces carry `produced_by="derived"` (wave -1) or
`"enrichment"` (waves 0-2) and a `wave` tag, per the plan's Claim-model extension
(app/state.py). Nothing here ever emits `produced_by="parser"` or `"research"` —
those are Layer 1's alone.
"""
from __future__ import annotations

import asyncio
import json as _json
import re as _re
import sqlite3
import uuid as _uuid
from datetime import date, datetime, timezone
from typing import Any

import phonenumbers
from langchain_core.messages import HumanMessage, SystemMessage

from app.db import (
    connection,
    get_claims,
    get_decisions_by_verdict,
    get_entity,
    get_entity_sources,
    get_enrichment_runs,
    upsert_claims,
    write_audit_rejected_value,
    write_enrichment_run,
    write_field_status,
    write_finding,
    write_rejection,
)
from app.config import SETTINGS
from app.parser import compute_injected_facts
from app.state import Claim, Finding, ValidationInput, utcnow
from app.validation import run_validation
from app.tools.edgar import edgar_full_text_search_raw
from app.tools.freefetch import fetch_page_free_first, fetch_raw_html
from app.tools.gdelt import news_search_raw
from app.tools.hunter import hunter_domain_search_raw
from app.tools.serper import serper_scrape_raw, serper_search_raw
from app.validation import (
    check_domain_mx_exists,
    check_v4_contradictions,
    check_v5_firm_is_fo_hardening,
    check_v5_staleness,
)

# Structured source classes with a known, hand-parsed payload shape (app/schema.sql's
# header comment + app/ingest.py's _map_source). Everything else is a discovery-class
# pass-through whose payload may carry a "people" list of unknown-but-conventional shape
# (name/full_name, title/occupation/role) — see _principal_from_people below.
_STRUCTURED_CLASSES = {"13f_filing", "5500_filing", "conference_sighting", "domain_check", "adv_index"}

# |QoQ 13F value change| at or above this triggers a why_now_trigger claim. Chosen as a
# deliberately conservative threshold — a why_now_trigger is a claim wave 2's
# outreach_hook may be authored from, so a borderline delta shouldn't fire it.
_QOQ_TRIGGER_THRESHOLD_PCT = 25.0


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return rows[-1] if rows else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _by_class(sources: list[dict[str, Any]], source_class: str) -> list[dict[str, Any]]:
    return [s for s in sources if s["source_class"] == source_class]


def _derived_claim(
    *, field_name: str, answer: Any, source: dict[str, Any] | None, extraction_method: str,
    confidence: str = "medium", source_class: str | None = None,
) -> Claim:
    return Claim(
        field_name=field_name,
        answer=answer,
        status="confirmed",
        source_url=(source or {}).get("url"),
        source_class=source_class or (source or {}).get("source_class"),
        extraction_method=extraction_method,
        retrieved_at=_parse_dt((source or {}).get("retrieved_at")),
        confidence=confidence,
        produced_by="derived",
        wave="-1",
    )


def _aum_claims(f13: dict[str, Any]) -> list[Claim]:
    claims: list[Claim] = []
    value_usd = f13["payload"].get("value_usd")
    quarter = f13["payload"].get("quarter")
    if value_usd is not None:
        claims.append(_derived_claim(
            field_name="aum_usd", answer=value_usd, source=f13,
            extraction_method="derived_13f", confidence="high",
        ))
        claims.append(_derived_claim(
            field_name="aum_basis", answer="13f_floor", source=f13,
            extraction_method="derived_13f", confidence="high",
        ))
    if quarter:
        claims.append(_derived_claim(
            field_name="aum_as_of", answer=quarter, source=f13,
            extraction_method="derived_13f", confidence="high",
        ))
    return claims


def _why_now_from_13f_delta(f13: dict[str, Any]) -> Claim | None:
    """concentration_pain (value dropped hard QoQ) / fresh_liquidity (value jumped hard
    QoQ). Depends on prior_value_usd, which app/ingest.py's edgar_13f mapping does not
    currently populate (only value_usd/quarter/position_count are captured from the raw
    discovery feed) — this degrades to "nothing to derive" until ingestion captures a
    prior quarter's value, rather than fabricating a delta from a single data point."""
    payload = f13["payload"]
    value_usd = payload.get("value_usd")
    prior_value = payload.get("prior_value_usd")
    if value_usd is None or prior_value in (None, 0):
        return None
    delta_pct = (value_usd - prior_value) / abs(prior_value) * 100
    if delta_pct <= -_QOQ_TRIGGER_THRESHOLD_PCT:
        trigger = "concentration_pain"
    elif delta_pct >= _QOQ_TRIGGER_THRESHOLD_PCT:
        trigger = "fresh_liquidity"
    else:
        return None
    return _derived_claim(
        field_name="why_now_trigger", answer=trigger, source=f13,
        extraction_method="derived_13f_qoq", confidence="medium",
    )


def _headcount_claim(f5500: dict[str, Any]) -> Claim | None:
    participant_count = f5500["payload"].get("participant_count")
    if participant_count is None:
        return None
    return _derived_claim(
        field_name="headcount", answer=participant_count, source=f5500,
        extraction_method="derived_5500", confidence="medium",
    )


def _access_window_claims(conf_rows: list[dict[str, Any]], today: date) -> list[Claim]:
    """why_now_trigger=access_window — a future-dated conference sighting is a live
    door-opener (the entity will physically be somewhere, soon)."""
    claims: list[Claim] = []
    for conf in conf_rows:
        date_str = conf["payload"].get("date")
        if not date_str:
            continue
        try:
            sighting_date = date.fromisoformat(date_str)
        except ValueError:
            continue
        if sighting_date <= today:
            continue
        claims.append(_derived_claim(
            field_name="why_now_trigger", answer="access_window", source=conf,
            extraction_method="derived_conference", confidence="medium",
        ))
    return claims


def _principal_from_people(sources: list[dict[str, Any]]) -> list[Claim]:
    """principal_name / principal_title from discovery-class pass-through payloads
    carrying a "people" list (fec_employer, ppp_loans, and similar — see
    app/ingest.py's _map_source fallback branch). Payload shape for these classes is
    whatever the raw discovery feed carried, not a normalized schema, so this reads
    defensively (name/full_name, title/occupation/role) and skips anyone with no name
    rather than guessing at field names that aren't there."""
    claims: list[Claim] = []
    for s in sources:
        if s["source_class"] in _STRUCTURED_CLASSES:
            continue
        people = s["payload"].get("people") or []
        for person in people:
            if not isinstance(person, dict):
                continue
            name = person.get("name") or person.get("full_name")
            if not name:
                continue
            claims.append(_derived_claim(
                field_name="principal_name", answer=name, source=s,
                extraction_method=f"derived_{s['source_class']}",
            ))
            title = person.get("title") or person.get("occupation") or person.get("role")
            if title:
                claims.append(_derived_claim(
                    field_name="principal_title", answer=title, source=s,
                    extraction_method=f"derived_{s['source_class']}",
                ))
            break  # first named person only — a full roster isn't wave -1's job
    return claims


def _discovery_class_claims(sources: list[dict[str, Any]]) -> list[Claim]:
    classes = sorted({s["source_class"] for s in sources})
    return [
        _derived_claim(
            field_name=f"discovery_class_{cls}", answer=True, source=None,
            extraction_method="derived_entity_sources", confidence="high", source_class=cls,
        )
        for cls in classes
    ]


def _public_list_overlap_claim(
    canonical_name: str | None, aliases: list[str] | None, public_list: set[str] | None
) -> Claim | None:
    """Set intersection vs. a scraped public list of known family offices (a name-based
    signal, not a source lookup — hence source_class=None). `public_list` is caller-
    supplied because no such list is wired up as a data source in this codebase yet;
    passing None (the default) means this claim is simply not emitted."""
    if not public_list:
        return None
    names = {n.strip().upper() for n in ([canonical_name] if canonical_name else []) + list(aliases or [])}
    overlap = names & {n.strip().upper() for n in public_list}
    if not overlap:
        return None
    return Claim(
        field_name="public_list_overlap",
        answer=sorted(overlap),
        status="confirmed",
        source_class="public_list",
        extraction_method="derived_public_list_match",
        confidence="medium",
        produced_by="derived",
        wave="-1",
    )


def wave_minus_1(
    entity_sources: list[dict[str, Any]],
    canonical_name: str | None = None,
    aliases: list[str] | None = None,
    public_list: set[str] | None = None,
    *,
    today: date | None = None,
    entity_id: str | None = None,
) -> list[Claim]:
    """Pure function, zero API calls (plan §4 wave -1 table). Run on every
    pursue/pursue_low lead before any spend decision — some pursue_low records promote
    to pursue here at zero cost. Emits a claim only when the underlying entity_sources
    data genuinely settles it; never fabricates a value for a field this ingestion
    pipeline hasn't captured (see per-helper docstrings for the two known upstream
    gaps: 13F holdings composition and 5500 sponsor phone are not in the current
    ingest.py payload shapes at all, so investing_mandates/sector_focus/direct_vs_fund/
    recent_investments/principal_phone are structurally unfillable until ingestion
    captures them — correctly emitting nothing beats guessing).

    `entity_id`, when given, stamps every claim with a deterministic claim_id (a hash of
    entity_id + field_name + source_class + extraction_method + answer) instead of the
    Claim model's random default — so calling this twice for the same entity upserts the
    SAME rows in the `claims` table rather than duplicating them (plan §9's idempotency
    test). Omitted by unit tests that don't care about storage identity.
    """
    today = today or datetime.now(timezone.utc).date()
    claims: list[Claim] = []

    f13_rows = _by_class(entity_sources, "13f_filing")
    if f13 := _latest(f13_rows):
        claims.extend(_aum_claims(f13))
        if trigger := _why_now_from_13f_delta(f13):
            claims.append(trigger)

    f5500_rows = _by_class(entity_sources, "5500_filing")
    if f5500 := _latest(f5500_rows):
        if headcount := _headcount_claim(f5500):
            claims.append(headcount)

    conf_rows = _by_class(entity_sources, "conference_sighting")
    claims.extend(_access_window_claims(conf_rows, today))

    claims.extend(_principal_from_people(entity_sources))
    claims.extend(_discovery_class_claims(entity_sources))

    if overlap := _public_list_overlap_claim(canonical_name, aliases, public_list):
        claims.append(overlap)

    if entity_id:
        claims = [
            c.model_copy(update={"claim_id": _uuid.uuid5(
                _uuid.NAMESPACE_URL, f"{entity_id}|{c.field_name}|{c.source_class}|{c.extraction_method}|{c.answer}"
            ).hex})
            for c in claims
        ]

    return claims


# ============================================================================
# Wave 0 — gates (~2 calls). Reuses Layer V's own deterministic functions
# (app/validation.py) called early, on pre-enrichment data — same functions Layer V
# runs again post-enrichment (plan §4: "not duplicating Layer V — same functions,
# different input"). A fatal finding here means wave 1/2 spend would be wasted, since
# Layer V would kill the record on the same grounds later anyway.
# ============================================================================


async def _recheck_registration(canonical_name: str) -> Finding:
    """Best-available live "ADV re-check": this codebase has no wired-up SEC IAPD/ADV
    lookup tool (ADV filings use a CRD identifier, not the CIK EDGAR submissions needs,
    and app/ingest.py's 13F payload doesn't carry a CIK either — see PROJECT_LOG.md's
    deviation note) — so this re-confirms current SEC registration/filing presence via
    EDGAR full-text search on the canonical name instead, which is a real, live,
    keyless call. Always "warn" at worst, never "fatal" on its own: zero new EDGAR hits
    on a re-check doesn't contradict evidence layer 1 already confirmed, it just adds
    nothing — V4/firm-is-FO hardening are what catch an actual contradiction."""
    result = await edgar_full_text_search_raw(canonical_name)
    if result.get("error"):
        return Finding(check_id="V5_adv_recheck", severity="warn", field="registration",
                        detail=f"EDGAR re-check request failed: {result['error']}")
    hits = result.get("results", [])
    if not hits:
        return Finding(check_id="V5_adv_recheck", severity="warn", field="registration",
                        detail=f"no EDGAR filings found for {canonical_name!r} on re-check")
    return Finding(check_id="V5_adv_recheck", severity="info", field="registration",
                    detail=f"{len(hits)} EDGAR filing(s) found on re-check",
                    evidence_url=hits[0].get("url"))


async def wave_0(
    claims: list[Claim], canonical_name: str, injected_facts: dict[str, Any] | None = None
) -> tuple[list[Claim], list[Finding], bool]:
    """Returns (annotated_claims, findings, fatal). `fatal=True` means the caller
    (run_pipeline) should reject immediately rather than spend wave 1/2 budget — Layer V
    would reject it on the same grounds regardless."""
    injected_facts = injected_facts or {}
    findings: list[Finding] = []

    claims, v4_findings = check_v4_contradictions(claims)
    findings.extend(v4_findings)
    findings.extend(check_v5_staleness(claims))
    findings.extend(check_v5_firm_is_fo_hardening(claims))

    domain = injected_facts.get("domain")
    if domain:
        mx_exists = await check_domain_mx_exists(domain)
        if mx_exists is False:
            findings.append(Finding(check_id="V5_domain_mx", severity="warn", field="domain",
                                     detail=f"domain {domain!r} has no MX record", evidence_url=f"https://{domain}"))
        elif mx_exists is None:
            findings.append(Finding(check_id="V5_domain_mx", severity="warn", field="domain",
                                     detail="MX lookup failed (resolver error) — treated as unresolved"))

    findings.append(await _recheck_registration(canonical_name))

    # Only the identity gate (V5_firm_is_fo — is this even a genuine family office, not
    # a plain RIA) stops the pipeline here at wave 0 (2026-07-29 relaxation, see
    # PROJECT_LOG.md). A V4 contradiction is still recorded and the claim is still
    # flipped to "contradicted", but it no longer rejects immediately — the entity gets
    # a full wave 1/2 shot, and Layer V's own re-run of the same checks (with more
    # evidence gathered) makes the final call instead of a cheap, early one.
    fatal = any(f.severity == "fatal" and f.check_id == "V5_firm_is_fo" for f in findings)
    return claims, findings, fatal


# ============================================================================
# Wave 1 — actionability core (~6 calls): named decision-maker + one working contact
# channel + one dated signal (plan §4). Every helper below is one tier of the table in
# the plan's wave-1 section; wave_1() tries tier 1 (already covered by wave -1, or a
# free direct guess), then escalates only if still unresolved.
# ============================================================================

_JSONLD_BLOCK_RE = _re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', _re.IGNORECASE | _re.DOTALL
)
_EMAIL_RE = _re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_COMMON_SITE_PATHS = ("/team", "/about", "/contact", "/about-us", "/leadership")


def _iter_jsonld_nodes(data: Any):
    if isinstance(data, list):
        for item in data:
            yield from _iter_jsonld_nodes(item)
    elif isinstance(data, dict):
        yield data
        for key in ("@graph", "employee", "founder", "member", "employees"):
            val = data.get(key)
            if val:
                yield from _iter_jsonld_nodes(val)


def extract_jsonld_person(html: str) -> tuple[str | None, str | None]:
    """First Person name/jobTitle found in any JSON-LD block on the page (top-level
    Person, or nested under an Organization's @graph/employee/founder/member). Skips
    malformed JSON-LD blocks rather than raising — this is best-effort parsing of
    someone else's markup, never a hard requirement."""
    for block in _JSONLD_BLOCK_RE.findall(html):
        try:
            data = _json.loads(block.strip())
        except ValueError:
            continue
        for node in _iter_jsonld_nodes(data):
            node_type = node.get("@type")
            is_person = node_type == "Person" or (isinstance(node_type, list) and "Person" in node_type)
            if is_person and node.get("name"):
                return node["name"], node.get("jobTitle")
    return None, None


def _extract_name_from_linkedin_title(title: str | None) -> str | None:
    """LinkedIn SERP titles are conventionally "<Name> - <Title> - <Company> | LinkedIn"
    — the name is always the first segment. Rejects anything that doesn't look like a
    short personal name (>4 words is almost certainly not a name)."""
    if not title:
        return None
    for sep in (" - ", " | ", " — "):
        if sep in title:
            candidate = title.split(sep)[0].strip()
            if candidate and 1 <= len(candidate.split()) <= 4:
                return candidate
    return None


async def _find_principal_via_site(domain: str) -> Claim | None:
    """Tier 2 — a direct guess at the entity's own common team/about page paths (free
    fetch, no credit spend on a miss), JSON-LD parsed first per plan §4."""
    for path in _COMMON_SITE_PATHS:
        url = f"https://{domain}{path}"
        html = await fetch_raw_html(url)
        if not html:
            continue
        name, title = extract_jsonld_person(html)
        if name:
            claim = Claim(
                field_name="principal_name", answer=name, status="confirmed",
                source_url=url, source_class="site_scrape", extraction_method="jsonld",
                confidence="high", produced_by="enrichment", wave="1",
            )
            return claim
    return None


async def _find_principal_via_xray(canonical_name: str) -> Claim | None:
    """Tier 3 — Serper x-ray for the entity's own LinkedIn people. A serper_organic
    source can never self-verify (app/validation.py's NON_CONFIRMING_CLASSES) — this is
    a lead for wave 2 / a human to corroborate, not a final answer, and is scored
    "low" confidence accordingly."""
    search = await serper_search_raw(f'site:linkedin.com/in "{canonical_name}"')
    for r in search.get("results", []):
        url = r.get("url") or ""
        if "linkedin.com/in/" not in url:
            continue
        name = _extract_name_from_linkedin_title(r.get("title"))
        if name:
            return Claim(
                field_name="principal_name", answer=name, status="confirmed",
                source_url=url, source_class="serper_organic", extraction_method="serper_xray",
                confidence="low", produced_by="enrichment", wave="1",
            )
    return None


async def _find_role_currency(canonical_name: str, principal_name: str) -> Claim | None:
    """Tier 1 — Serper x-ray snippet for <name> + <entity>, title parsed the same way as
    the LinkedIn-title convention above."""
    search = await serper_search_raw(f'site:linkedin.com/in "{principal_name}" "{canonical_name}"')
    first_token = principal_name.split()[0].lower() if principal_name.split() else ""
    for r in search.get("results", []):
        title = r.get("title") or ""
        parts = [p.strip() for p in title.replace(" | ", " - ").split(" - ")]
        if len(parts) >= 2 and first_token and first_token in parts[0].lower():
            return Claim(
                field_name="principal_title", answer=parts[1], status="confirmed",
                source_url=r.get("url"), source_class="serper_organic", extraction_method="serper_xray",
                confidence="low", produced_by="enrichment", wave="1",
            )
    return None


async def _find_email_on_site(domain: str) -> Claim | None:
    """Tier 1 — free fetch of common contact/team pages, first @domain email found."""
    for path in _COMMON_SITE_PATHS:
        url = f"https://{domain}{path}"
        fetched = await fetch_page_free_first(url, serper_scrape_raw)
        content = fetched.get("content") or ""
        match = _EMAIL_RE.search(content)
        if match and domain.lower() in match.group(0).lower():
            return Claim(
                field_name="principal_email", answer=match.group(0), status="confirmed",
                source_url=url, source_class="site_scrape",
                extraction_method=fetched.get("extraction_method", "site_scrape"),
                confidence="medium", produced_by="enrichment", wave="1",
            )
    return None


async def _find_email_via_hunter(domain: str, principal_name: str | None) -> Claim | None:
    """Tier 3 — Hunter domain search (one credit -> every known address + pattern for
    the domain). Prefers an address matching the known principal's surname; falls back
    to the highest-confidence address on the domain otherwise."""
    result = await hunter_domain_search_raw(domain)
    emails = result.get("emails") or []
    if not emails:
        return None
    last_name = principal_name.split()[-1].lower() if principal_name and principal_name.split() else None
    if last_name:
        for e in emails:
            if e.get("last_name") and e["last_name"].lower() == last_name and e.get("value"):
                return Claim(
                    field_name="principal_email", answer=e["value"], status="confirmed",
                    source_url=f"https://{domain}", source_class="hunter",
                    extraction_method="hunter_domain_search", confidence="medium",
                    produced_by="enrichment", wave="1",
                )
    best = emails[0]
    if best.get("value"):
        return Claim(
            field_name="principal_email", answer=best["value"], status="confirmed",
            source_url=f"https://{domain}", source_class="hunter",
            extraction_method="hunter_domain_search", confidence="low",
            produced_by="enrichment", wave="1",
        )
    return None


async def _find_phone_via_site(domain: str) -> Claim | None:
    """Tier 2 — free fetch of common contact pages, first phone-shaped match. Format-
    valid only, never line-verified (research_layer_plan.md §4.7's Layer-2 note) — so
    this is explicitly status="format_only", not "confirmed"."""
    for path in ("/contact", "/about", "/team"):
        url = f"https://{domain}{path}"
        fetched = await fetch_page_free_first(url, serper_scrape_raw)
        content = fetched.get("content") or ""
        for match in phonenumbers.PhoneNumberMatcher(content, "US"):
            number = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
            return Claim(
                field_name="principal_phone", answer=number, status="format_only",
                source_url=url, source_class="site_scrape",
                extraction_method=fetched.get("extraction_method", "site_scrape"),
                confidence="low", produced_by="enrichment", wave="1",
            )
    return None


async def _find_dated_signal(canonical_name: str) -> Claim | None:
    """Tier 2 GDELT, tier 3 Serper /news — both carry a real dated field (seendate /
    date), which is the whole point of a "dated signal" claim."""
    gdelt = await news_search_raw(f'"{canonical_name}"', lookback_days=180, max_records=5)
    for r in gdelt.get("results", []):
        if r.get("url") and r.get("seendate"):
            return Claim(
                field_name="recent_investments", answer=r.get("title") or r["url"], status="confirmed",
                source_url=r["url"], source_class="gdelt", extraction_method="gdelt_docapi",
                confidence="low", produced_by="enrichment", wave="1",
                retrieved_at=_parse_dt(r.get("seendate")),
            )
    news = await serper_search_raw(f'"{canonical_name}"', topic="news", max_results=5)
    for r in news.get("results", []):
        if r.get("url"):
            return Claim(
                field_name="recent_investments", answer=r.get("title") or r["url"], status="confirmed",
                source_url=r["url"], source_class="news_article", extraction_method="serper_news",
                confidence="low", produced_by="enrichment", wave="1",
            )
    return None


def _settled_fields(claims: list[Claim]) -> set[str]:
    return {
        c.field_name for c in claims
        if c.field_name and c.status not in ("could_not_verify", "removed_failed_validation")
    }


async def wave_1(
    claims: list[Claim], canonical_name: str, domain: str | None = None
) -> tuple[list[Claim], bool]:
    """Returns (new_claims, gated). `gated=True` means no decision-maker OR no contact
    channel was resolved even after every tier — the caller (run_pipeline) should stop;
    V6 completeness will reject it at validation regardless, so wave 2 spend on it is
    pure waste (plan §4's wave-1 gate)."""
    new_claims: list[Claim] = []
    settled = _settled_fields(claims)

    if "principal_name" not in settled and domain:
        if claim := await _find_principal_via_site(domain):
            new_claims.append(claim)
            settled.add("principal_name")
    if "principal_name" not in settled:
        if claim := await _find_principal_via_xray(canonical_name):
            new_claims.append(claim)
            settled.add("principal_name")

    principal_name = next(
        (c.answer for c in [*claims, *new_claims] if c.field_name == "principal_name" and isinstance(c.answer, str)),
        None,
    )

    if principal_name and "principal_title" not in settled:
        if claim := await _find_role_currency(canonical_name, principal_name):
            new_claims.append(claim)
            settled.add("principal_title")

    if "principal_email" not in settled and domain:
        claim = await _find_email_on_site(domain)
        if claim is None:
            claim = await _find_email_via_hunter(domain, principal_name)
        if claim:
            new_claims.append(claim)
            settled.add("principal_email")

    if "principal_phone" not in settled and domain:
        if claim := await _find_phone_via_site(domain):
            new_claims.append(claim)
            settled.add("principal_phone")

    if "recent_investments" not in settled and "why_now_trigger" not in settled:
        if claim := await _find_dated_signal(canonical_name):
            new_claims.append(claim)
            settled.add("recent_investments")

    have_decision_maker = "principal_name" in settled
    have_channel = "principal_email" in settled or "principal_phone" in settled
    gated = not (have_decision_maker and have_channel)
    return new_claims, gated


# ============================================================================
# Wave 2 — depth (~8 calls, survivors only). "No judgment calls — fit was decided at
# layer 1. That makes wave 2 parallelizable and tolerant of a cheaper model" (plan §4).
# Narrative fields go through one LLM extraction pass over gathered documents, citing a
# source_index the code validates against the actual document list (never trusts a
# made-up citation) — same in-code-validation posture as app.researcher's
# compress_to_claims. Everything else here is deterministic (x-ray lookups, news
# search), same as wave 1.
# ============================================================================

_WAVE2_ALLOWED_FIELDS = frozenset({
    "investing_thesis", "background", "check_size_range", "stage_focus",
    "geography_focus", "do_not_pitch", "fit_tags",
})

_WAVE2_SYSTEM_PROMPT = (
    "You are extracting structured facts about a family office from the numbered source "
    "documents below. For each fact you can find, emit ONE entry: "
    '{"field_name": "<one of: investing_thesis, background, check_size_range, '
    'stage_focus, geography_focus, do_not_pitch, fit_tags>", "answer": "<the fact, '
    'concise>", "source_index": <the number of the document that supports it>}. '
    "Only emit a field if a document actually states it — never guess or infer beyond "
    "what is written. Respond with ONLY a JSON array of these objects (an empty array "
    "if nothing is supported). No markdown fences, no extra keys."
)


def _parse_wave2_json(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    data = _json.loads(text)
    if not isinstance(data, list):
        raise ValueError("expected a JSON array")
    return data


async def _gather_wave2_documents(canonical_name: str, domain: str | None) -> list[dict[str, str]]:
    """Numbered source documents for the LLM extraction pass — each {"url","content"}."""
    docs: list[dict[str, str]] = []
    if domain:
        fetched = await fetch_page_free_first(f"https://{domain}", serper_scrape_raw)
        if fetched.get("content"):
            docs.append({"url": f"https://{domain}", "content": fetched["content"][:3000]})
    search = await serper_search_raw(
        f'"{canonical_name}" investment thesis OR "check size" OR "stage focus"', max_results=3
    )
    for r in search.get("results", []):
        if r.get("url") and r.get("content"):
            docs.append({"url": r["url"], "content": r["content"]})
    return docs


async def _extract_wave2_narrative_fields(
    canonical_name: str, domain: str | None, model: Any
) -> tuple[list[Claim], float]:
    docs = await _gather_wave2_documents(canonical_name, domain)
    if not docs:
        return [], 0.0

    numbered = "\n\n".join(f"[{i + 1}] {d['url']}\n{d['content'][:1500]}" for i, d in enumerate(docs))
    response = await model.ainvoke(
        [SystemMessage(content=_WAVE2_SYSTEM_PROMPT), HumanMessage(content=numbered)]
    )
    cost = response.response_metadata.get("cost_usd", 0.0)
    try:
        entries = _parse_wave2_json(str(response.content))
    except (_json.JSONDecodeError, ValueError):
        return [], cost

    claims: list[Claim] = []
    for entry in entries:
        field = entry.get("field_name")
        answer = entry.get("answer")
        idx = entry.get("source_index")
        if field not in _WAVE2_ALLOWED_FIELDS or not answer:
            continue
        if not isinstance(idx, int) or not (1 <= idx <= len(docs)):
            continue  # unverifiable citation -> discard rather than trust a made-up index
        claims.append(Claim(
            field_name=field, answer=answer, status="confirmed",
            source_url=docs[idx - 1]["url"], source_class="web_page",
            extraction_method="llm_wave2_extraction", confidence="low",
            produced_by="enrichment", wave="2",
        ))
    return claims, cost


async def _find_corporate_linkedin(canonical_name: str) -> Claim | None:
    search = await serper_search_raw(f'site:linkedin.com/company "{canonical_name}"')
    for r in search.get("results", []):
        url = r.get("url") or ""
        if "linkedin.com/company/" in url:
            return Claim(
                field_name="corporate_linkedin", answer=url, status="confirmed",
                source_url=url, source_class="serper_organic", extraction_method="serper_xray",
                confidence="medium", produced_by="enrichment", wave="2",
            )
    return None


async def _find_principal_linkedin(principal_name: str, canonical_name: str) -> Claim | None:
    search = await serper_search_raw(f'site:linkedin.com/in "{principal_name}" "{canonical_name}"')
    for r in search.get("results", []):
        url = r.get("url") or ""
        if "linkedin.com/in/" in url:
            return Claim(
                field_name="principal_linkedin", answer=url, status="confirmed",
                source_url=url, source_class="serper_organic", extraction_method="serper_xray",
                confidence="medium", produced_by="enrichment", wave="2",
            )
    return None


_WAVE2_NEWS_QUERIES = {
    "recent_fund_commitments": lambda name: f'"{name}" (commitment OR "LP" OR allocation)',
    "recent_key_hires": lambda name: f'"{name}" (hire OR appoints OR joins)',
    "recent_news": lambda name: f'"{name}"',
}


async def _find_news_signals(canonical_name: str) -> list[Claim]:
    """recent_fund_commitments / recent_key_hires / recent_news — tier 2 GDELT, tier 3
    Serper /news, same dated-evidence pattern as wave 1's dated-signal helper, one
    result (the single highest-kill-power hit) per field."""
    claims: list[Claim] = []
    for field, query_fn in _WAVE2_NEWS_QUERIES.items():
        query = query_fn(canonical_name)
        gdelt = await news_search_raw(query, lookback_days=180, max_records=3)
        results = gdelt.get("results") or []
        source_class, extraction_method = "gdelt", "gdelt_docapi"
        if not results:
            news = await serper_search_raw(query, topic="news", max_results=3)
            results = news.get("results") or []
            source_class, extraction_method = "news_article", "serper_news"
        for r in results[:1]:
            if not r.get("url"):
                continue
            claims.append(Claim(
                field_name=field, answer=r.get("title") or r["url"], status="confirmed",
                source_url=r["url"], source_class=source_class, extraction_method=extraction_method,
                confidence="low", produced_by="enrichment", wave="2",
                retrieved_at=_parse_dt(r.get("seendate")) if source_class == "gdelt" else None,
            ))
    return claims


_OUTREACH_HOOK_SYSTEM = (
    "Write ONE short outreach-hook sentence for a first-contact email, using ONLY the "
    "fact given below — do not add any detail not present in it. Respond with ONLY the "
    "sentence: no quotes, no markdown, no preamble."
)


async def _author_outreach_hook(trigger_claim: Claim, model: Any) -> tuple[Claim | None, float]:
    """"authored by the model from the highest-confidence trigger; if no trigger exists,
    leave blank rather than inventing one" (plan §4) — the "leave blank" half is enforced
    by the caller (wave_2) never calling this without a trigger claim in hand; this
    function itself never fabricates a trigger, only phrases the one it's given."""
    response = await model.ainvoke(
        [SystemMessage(content=_OUTREACH_HOOK_SYSTEM), HumanMessage(content=f"Fact: {trigger_claim.answer}")]
    )
    cost = response.response_metadata.get("cost_usd", 0.0)
    text = str(response.content).strip()
    if not text:
        return None, cost
    return Claim(
        field_name="outreach_hook", answer=text, status="confirmed",
        source_url=trigger_claim.source_url, source_class=trigger_claim.source_class,
        extraction_method="llm_authored", confidence="low",
        produced_by="enrichment", wave="2",
    ), cost


async def wave_2(
    claims: list[Claim], canonical_name: str, model: Any, domain: str | None = None
) -> tuple[list[Claim], float]:
    """Survivors only (called after wave 1 clears its gate). Returns (new_claims,
    cost_usd)."""
    new_claims: list[Claim] = []
    total_cost = 0.0
    settled = _settled_fields(claims)

    narrative_claims, cost = await _extract_wave2_narrative_fields(canonical_name, domain, model)
    total_cost += cost
    new_claims.extend(c for c in narrative_claims if c.field_name not in settled)

    if "corporate_linkedin" not in settled:
        if claim := await _find_corporate_linkedin(canonical_name):
            new_claims.append(claim)

    principal_name = next(
        (c.answer for c in [*claims, *new_claims] if c.field_name == "principal_name" and isinstance(c.answer, str)),
        None,
    )
    if principal_name and "principal_linkedin" not in settled:
        if claim := await _find_principal_linkedin(principal_name, canonical_name):
            new_claims.append(claim)

    new_claims.extend(c for c in await _find_news_signals(canonical_name) if c.field_name not in settled)

    trigger = next((c for c in [*claims, *new_claims] if c.field_name == "why_now_trigger"), None)
    if trigger and "outreach_hook" not in settled:
        hook_claim, cost = await _author_outreach_hook(trigger, model)
        total_cost += cost
        if hook_claim:
            new_claims.append(hook_claim)

    return new_claims, total_cost


# ============================================================================
# Pipeline orchestration — plan §4's "Reserve pool control flow", wired against the
# real DB. process_entity() runs one entity through wave 0 -> 1 -> 2 -> Layer V,
# persisting the claims spine + findings + field_status + audit_rejected_values +
# enrichment_runs bookkeeping as it goes. run_pipeline() is the entry point.
# ============================================================================

_CLAIM_FIELDS = set(Claim.model_fields.keys())


def _claim_from_row(row: dict[str, Any]) -> Claim:
    """get_claims() rows carry a `created_at` column the Claim model doesn't have —
    filter to known fields rather than let Pydantic's default `extra="ignore"` silently
    swallow a real schema mismatch later."""
    return Claim(**{k: v for k, v in row.items() if k in _CLAIM_FIELDS})


async def process_entity(
    conn: sqlite3.Connection, entity_id: str, model: Any, *, force: bool = False
) -> dict[str, Any]:
    """Runs one pursue/pursue_low entity through wave 0 -> 1 -> 2 -> Layer V. Persists
    everything as it goes so a crash mid-batch loses at most one entity's work, not the
    whole run. Returns {"entity_id","outcome","calls_spent","usd_spent"}.

    Idempotent by default: an entity that already has an `enrichment_runs` row (i.e.
    has been through this function once before) is NOT reprocessed — every downstream
    tool call (EDGAR, Serper, Hunter, GDELT, the LLM) is skipped and the prior run's
    outcome is returned as-is ("re-run enrichment on a completed entity -> assert zero
    new API calls", plan §9). Pass `force=True` to deliberately redo the work (e.g. new
    entity_sources arrived upstream)."""
    if not force:
        prior_runs = get_enrichment_runs(conn, entity_id)
        if prior_runs:
            latest = prior_runs[-1]
            return {"entity_id": entity_id, "outcome": latest["outcome"], "calls_spent": 0,
                    "usd_spent": 0.0, "skipped_already_processed": True}

    entity = get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"Unknown entity_id: {entity_id!r} — not found in entities table")
    sources = get_entity_sources(conn, entity_id)
    injected_facts = compute_injected_facts(sources)
    canonical_name = entity["canonical_name"]
    domain = injected_facts.get("domain")

    existing = [_claim_from_row(r) for r in get_claims(conn, entity_id)]
    started_at = utcnow()

    minus1 = wave_minus_1(sources, canonical_name, entity["aliases"], entity_id=entity_id)
    claims = existing + minus1
    waves_completed = ["-1"]

    claims, wave0_findings, fatal0 = await wave_0(claims, canonical_name, injected_facts)
    waves_completed.append("0")

    calls_spent = 0
    usd_spent = 0.0

    if fatal0:
        outcome = "reject"
        fatal_findings = [f for f in wave0_findings if f.severity == "fatal"]
        for f in fatal_findings:
            write_finding(conn, entity_id, f.check_id, f.severity, detail=f.detail,
                          claim_id=f.claim_id, field=f.field, evidence_url=f.evidence_url)
        write_rejection(
            conn, entity_id,
            reason_code=";".join(f"{f.check_id}:{f.field}" for f in fatal_findings) or "wave0_gate",
            gate_results={}, claim_ledger=[c.model_dump(mode="json") for c in claims], stage="wave0",
        )
    else:
        new1, gated = await wave_1(claims, canonical_name, domain=domain)
        claims = claims + new1
        calls_spent += len(new1)
        waves_completed.append("1")

        if not gated:
            new2, cost2 = await wave_2(claims, canonical_name, model, domain=domain)
            claims = claims + new2
            calls_spent += len(new2)
            usd_spent += cost2
            waves_completed.append("2")

        vi = ValidationInput(
            entity_id=entity_id, claim_ledger=claims, waves_completed=waves_completed,
            wave0_findings=wave0_findings,
        )
        dataset_input, audit_entries, v1_cost = await run_validation(vi, model)
        usd_spent += v1_cost
        outcome = dataset_input.outcome
        claims = dataset_input.claim_ledger

        for f in dataset_input.findings:
            write_finding(conn, entity_id, f.check_id, f.severity, detail=f.detail,
                          claim_id=f.claim_id, field=f.field, evidence_url=f.evidence_url)
        for fs in dataset_input.field_statuses:
            write_field_status(conn, entity_id, fs.field, fs.status, method=fs.method,
                               confirming_url=fs.confirming_url, confirming_class=fs.confirming_class)
        for a in audit_entries:
            write_audit_rejected_value(conn, entity_id, a["field_name"], a["rejected_value"],
                                       a["reason_code"], evidence_url=a.get("evidence_url"))

        if outcome == "reject":
            fatal_fields = sorted({f.field for f in dataset_input.findings if f.severity == "fatal" and f.field})
            write_rejection(
                conn, entity_id,
                reason_code=";".join(fatal_fields) or "V6_completeness",
                gate_results={}, claim_ledger=[c.model_dump(mode="json") for c in claims], stage="validation",
            )

    upsert_claims(conn, entity_id, [c.model_dump(mode="json") for c in claims])
    ended_at = utcnow()
    write_enrichment_run(
        conn, entity_id, wave=waves_completed[-1], calls_spent=calls_spent, usd_spent=usd_spent,
        started_at=started_at.isoformat(), ended_at=ended_at.isoformat(), outcome=outcome,
    )
    return {"entity_id": entity_id, "outcome": outcome, "calls_spent": calls_spent, "usd_spent": usd_spent}


def _is_promotable(claims: list[Claim]) -> bool:
    """A pursue_low lead promotes into the pursue queue if wave -1 alone already
    settled both halves of wave 1's own gate (decision-maker + a contact channel) —
    "some pursue_low records promote to pursue here at zero cost" (plan §4)."""
    settled = _settled_fields(claims)
    return "principal_name" in settled and ("principal_email" in settled or "principal_phone" in settled)


def _reserve_sort_key(entity_id: str, minus1_by_entity: dict[str, list[Claim]]):
    """Ranks the reserve pool by discovery_class_count (wave -1's own derived signal —
    the more discovery classes corroborate a lead, the likelier it's real), descending.
    `triage_score` from the plan's pseudocode has no home in this schema — the
    entities/decisions tables never carried one (it belongs to the discovery/triage
    layer upstream of everything this codebase implements) — so this orders on the one
    real, available proxy rather than fabricating a score."""
    claims = minus1_by_entity.get(entity_id, [])
    discovery_class_count = sum(1 for c in claims if c.field_name and c.field_name.startswith("discovery_class_"))
    return -discovery_class_count


def _db_path_from_conn(conn: sqlite3.Connection) -> str:
    """Each concurrent process_entity() call needs its OWN sqlite3 connection — a single
    connection object isn't safe to share across concurrent coroutines, same reason
    every graph node in app/graph.py opens its own short-lived connection rather than
    share one across concurrent lead runs. Derives the file path from the CALLER's
    connection (via PRAGMA database_list) so run_pipeline's own signature doesn't have
    to change — every existing call site keeps working unmodified."""
    row = conn.execute("PRAGMA database_list").fetchone()
    return row[2]


async def _process_entities_concurrently(
    entity_ids: list[str], model: Any, db_path: str, concurrency: int, *, force: bool = False
) -> list[dict[str, Any]]:
    """Runs process_entity() for each entity_id with bounded concurrency (same pattern
    as app.runner.run_batch's batch dispatch) — each call opens its own connection via
    `db_path`. Sequential processing here was a real throughput bottleneck at scale:
    each entity can mean several free-fetches + several LLM calls, so 10+ entities
    processed one at a time serially could take longer than all of Layer 1 combined.

    `force=True` propagates to process_entity's `force`, re-running entities that already
    have an enrichment_runs row (needed when gate logic changed and prior outcomes must be
    recomputed, not replayed from cache)."""
    semaphore = asyncio.Semaphore(concurrency)

    async def _bounded(entity_id: str) -> dict[str, Any]:
        async with semaphore:
            with connection(db_path) as isolated_conn:
                return await process_entity(isolated_conn, entity_id, model, force=force)

    return list(await asyncio.gather(*[_bounded(eid) for eid in entity_ids]))


async def run_pipeline(
    conn: sqlite3.Connection,
    model: Any,
    *,
    target_survivors: int = 50,
    reserve_budget_fraction: float = 0.20,
    total_enrichment_budget_usd: float | None = None,
    concurrency: int = 8,
    force: bool = False,
) -> dict[str, Any]:
    """Plan §4's "Reserve pool control flow", wired against the real DB.

    `force=True` re-runs entities that already have an enrichment_runs row instead of
    replaying their cached outcome — required after a gate-logic change so existing
    pursue/pursue_low leads are re-judged against the new rules rather than the old ones.

    1. wave -1 for every pursue/pursue_low lead (free), persisted immediately.
    2. Leads that promote (pursue_low + wave -1 alone clears the wave-1 gate) join the
       pursue queue at zero extra cost.
    3. Process the full pursue queue through waves 0-2 + Layer V.
    4. If survivors (ship/ship_with_caveats) are short of `target_survivors`, draw from
       the remaining pursue_low reserve — `thin_reason="fixable"` only, ordered by
       discovery_class_count — capped at `reserve_budget_fraction` of the enrichment
       budget. Two draws that still can't fill the target is a discovery-pool problem,
       not an enrichment one (plan §4) — this stops rather than looping forever.

    Returns {"processed": [...], "survivors": int, "reserve_draws": int, "usd_spent": float}.
    """
    total_budget = (
        total_enrichment_budget_usd if total_enrichment_budget_usd is not None else SETTINGS.runner.global_budget_usd
    )
    reserve_cap_usd = total_budget * reserve_budget_fraction

    decisions = get_decisions_by_verdict(conn, ["pursue", "pursue_low"])

    minus1_by_entity: dict[str, list[Claim]] = {}
    for d in decisions:
        entity_id = d["entity_id"]
        entity = get_entity(conn, entity_id)
        sources = get_entity_sources(conn, entity_id)
        claims = wave_minus_1(sources, entity["canonical_name"], entity["aliases"], entity_id=entity_id)
        minus1_by_entity[entity_id] = claims
        upsert_claims(conn, entity_id, [c.model_dump(mode="json") for c in claims])

    # Commit now, before any concurrent processing starts: `conn` is the CALLER's
    # connection (only committed when their own `with connection()` block eventually
    # exits, i.e. after this whole function returns) — if its wave -1 writes above stay
    # uncommitted while _process_entities_concurrently opens NEW connections to the
    # same file, SQLite's single-writer lock deadlocks (the new connections block
    # waiting for `conn` to release the write lock, which never happens until this
    # function returns, which never happens until they succeed). Found live 2026-07-29
    # adding concurrent processing — see PROJECT_LOG.md.
    conn.commit()

    pursue_queue: list[str] = []
    reserve_pool: list[dict[str, Any]] = []
    for d in decisions:
        current = [_claim_from_row(r) for r in get_claims(conn, d["entity_id"])]
        if d["verdict"] == "pursue" or _is_promotable(current):
            pursue_queue.append(d["entity_id"])
        else:
            reserve_pool.append(d)

    db_path = _db_path_from_conn(conn)
    processed: list[dict[str, Any]] = []
    usd_spent = 0.0
    for result in await _process_entities_concurrently(pursue_queue, model, db_path, concurrency, force=force):
        processed.append(result)
        usd_spent += result["usd_spent"]

    def _survivor_count() -> int:
        return sum(1 for r in processed if r["outcome"] in ("ship", "ship_with_caveats"))

    processed_ids = {r["entity_id"] for r in processed}
    remaining_reserve = sorted(
        (d for d in reserve_pool if d["entity_id"] not in processed_ids and d.get("thin_reason") == "fixable"),
        key=lambda d: _reserve_sort_key(d["entity_id"], minus1_by_entity),
    )

    reserve_draws = 0
    reserve_spent = 0.0
    while _survivor_count() < target_survivors and remaining_reserve and reserve_spent < reserve_cap_usd:
        gap = target_survivors - _survivor_count()
        draw_n = max(1, int(gap * 1.5))
        draw, remaining_reserve = remaining_reserve[:draw_n], remaining_reserve[draw_n:]
        if not draw:
            break
        # Processed as one concurrent batch (same as the pursue queue) — the budget cap
        # is checked between batches rather than mid-batch, since a batch's cost isn't
        # known until every call in it finishes. draw_n is already small (~1.5x the
        # remaining gap), so any overshoot within one batch is bounded.
        draw_ids = [d["entity_id"] for d in draw]
        for result in await _process_entities_concurrently(draw_ids, model, db_path, concurrency, force=force):
            processed.append(result)
            usd_spent += result["usd_spent"]
            reserve_spent += result["usd_spent"]
            reserve_draws += 1

    return {
        "processed": processed,
        "survivors": _survivor_count(),
        "reserve_draws": reserve_draws,
        "usd_spent": usd_spent,
    }

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
import logging
import os
import re as _re
import sqlite3
import uuid as _uuid
from datetime import date, datetime, timezone
from typing import Any
from pathlib import Path
from urllib.parse import urlsplit as _urlsplit

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
    start_run,
    finish_run,
)
from app.config import SETTINGS
from app.llm import get_model
from app.parser import compute_injected_facts
from app.state import Claim, Finding, ValidationInput, select_claim_for_question, utcnow, _CLAIM_STATUS_RANK
from app.validation import run_validation
# PLAN.md T27.2: reuse the EXACT T26 provenance ranking the shipped row uses
# (app.dataset._records_rows), so the wave -1 view of "which principal won" can never
# drift from the row's view. `_provenance_rank` is a pure longest-prefix-match helper;
# importing it (rather than re-implementing) is what guarantees the no-drift property.
from app.dataset import _provenance_rank, _atomic_write_json
# T35.2: bind the tool-call provenance context around each entity's enrichment so every
# external tool call made while processing waves 0-2 is attributed to the right entity +
# run. Outside a bound context the recorder is a silent no-op, so process_entity called
# directly from tests (no run) is unaffected.
from app.toollog import tool_log_context
from app.tools.adv import distinctive_name_tokens
from app.tools.edgar import edgar_full_text_search_raw
from app.tools.freefetch import fetch_page_free_first, fetch_raw_html
from app.tools.gdelt import news_search_raw
from app.tools.serper import serper_search_raw
from app.tools.snov import snov_domain_search_raw, snov_emails_by_name_domain_raw
from app.validation import (
    _judge_claim_polarity,
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

logger = logging.getLogger(__name__)

# |QoQ 13F value change| at or above this triggers an important_insight claim. Chosen as a
# deliberately conservative threshold — an important_insight is a claim the row ships, so
# a borderline delta shouldn't fire it.
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


def _adv_aum_claims(adv_row: dict[str, Any]) -> list[Claim]:
    """AUM from an adv_name row's self-reported RAUM (PLAN.md T19.5). Mirrors
    `_aum_claims` but reads `signals.raum_usd` instead of a 13F holdings value. A 13F
    is a holdings floor for a stated quarter; RAUM is self-reported, so the 13F-derived
    aum_usd wins when both exist — wave_minus_1 emits this one ONLY when no 13F row is
    present (see wave_minus_1). Verified live: Class VI $1,498,011,942, MB $204,073,110,
    Arden $107,056,681 all shipped `aum_usd` blank before this.

    `aum_basis` is "adv_raum" (distinct from the 13F's "13f_floor") so the row records
    HOW the figure was derived. `aum_as_of` is the row's `retrieved_at` date when present
    (RAUM has no reporting quarter in the current ingest payload — the retrieval date is
    the best available 'as of')."""
    signals = adv_row["payload"].get("signals") or {}
    raum = signals.get("raum_usd")
    if raum is None:
        return []
    claims: list[Claim] = [
        _derived_claim(
            field_name="aum_usd", answer=raum, source=adv_row,
            extraction_method="adv_raum", confidence="high", source_class="adv_name",
        ),
        _derived_claim(
            field_name="aum_basis", answer="adv_raum", source=adv_row,
            extraction_method="adv_raum", confidence="high", source_class="adv_name",
        ),
    ]
    retrieved_at = _parse_dt(adv_row.get("retrieved_at"))
    if retrieved_at:
        claims.append(_derived_claim(
            field_name="aum_as_of", answer=retrieved_at.date().isoformat(), source=adv_row,
            extraction_method="adv_raum", confidence="high", source_class="adv_name",
        ))
    return claims


# PLAN.md T19.3: Layer 1 writes claims keyed on `question_id` (G2.Q1); the dataset row
# pivots on `field_name` (principal_name). This map bridges the two vocabularies so a
# researched, confirmed fact reaches the row instead of being invisible to Layer D and
# to enrichment's _settled_fields (which is what gated wave 2 off on Class VI).
QUESTION_FIELD_PROJECTIONS: dict[str, str] = {
    "G2.Q1": "principal_name",
    "G2.Q3": "principal_title",
    "G3.Q1": "recent_investments",
    "G3.Q2": "important_insight",
}

# PLAN.md T28.1: the statuses a Layer-1 question claim may carry and STILL project
# into a field-name claim. Layer 1 writes `confirmed`; Layer V then rewrites a
# validated-good claim to `single_source` (confirmed from one source class) or
# `verified` (cross-class corroborated). Both are *validated-good* and strictly
# STRONGER evidence than the raw `confirmed` this set already accepts — so the gate
# must include them, or a `process_entity(..., force=True)` re-run over an
# already-validated ledger silently drops every researched projection and lets the
# discovery feed (a FEC donor on a `fec_employer` lead) win `_select_principal_name`.
# Do NOT collapse this back to a single `== "confirmed"` check — that was T19.3's
# shortcut and it excluded the validated-good statuses by accident (PLAN.md T28).
# Everything else stays excluded: `could_not_verify`, `contradicted`, `superseded`,
# `removed_failed_validation`, `pattern_inferred`, `format_only`.
_PROJECTABLE_STATUSES: frozenset[str] = frozenset({"confirmed", "single_source", "verified"})

# PLAN.md T30.1: the projected fields whose value is legitimately a sentence rather
# than a discrete entity, so a Layer-1 claim's `answer` is an acceptable substitute
# when the compress model omitted `subject_value`. The compress model fills
# `subject_value` reliably for entity answers (G2.Q1 a name, G2.Q2 a URL, G2.Q3 a
# title) and unreliably or never for prose answers (G3.Q1/G3.Q2 are 0-for-6 on the
# live ledger) — for a prose field the `subject_value` and the `answer` are the same
# kind of thing, so the model sees no reason to emit both. Entity-valued fields
# (`principal_name`, `principal_title`, `principal_linkedin`) are deliberately NOT
# in this set: a whole sentence in `principal_name` is exactly the T26/T27/T29
# defect. A present, non-blank `subject_value` always wins over this fallback.
_PROSE_PROJECTION_FIELDS: frozenset[str] = frozenset({"important_insight", "recent_investments"})


def _is_negative_answer(text: str) -> bool:
    """PLAN.md T30.2 — a settled claim can still carry a statement-of-absence answer
    ("No recent exits, hires, or commitments were found for ..."), and projecting
    that would put a *reason not to call* into a field meant to hold a reason to call.
    This guards the T30.1 prose fallback (ONLY the fallback — an explicit
    `subject_value` the model deliberately distilled is always respected).

    LEADING-TOKEN match, never a substring match: `"No"` must not match inside
    `"Norwegian sovereign fund commitment announced"`. `app/validation.py` draws
    this same distinction in `_is_negative_fo_answer`; reimplemented locally because
    validation is not a dependency of the projection path."""
    cleaned = _re.sub(r"[^\w\s]+", " ", text, flags=_re.UNICODE).strip().lower()
    if not cleaned:
        return False
    tokens = cleaned.split()
    # Single leading tokens that signal a statement of absence / inability.
    negative_tokens = {"no", "none", "not", "never", "unable", "insufficient"}
    if tokens[0] in negative_tokens:
        return True
    # Multi-word leading phrases — compare the first N tokens so the phrase is
    # matched as a unit ("there is no", "there are no", "no recent") rather than as
    # a single token, and so "no recent" is caught even though "recent" alone is
    # not a negation.
    negative_phrases = (
        ("there", "is", "no"),
        ("there", "are", "no"),
        ("no", "recent"),
    )
    for phrase in negative_phrases:
        if tuple(tokens[: len(phrase)]) == phrase:
            return True
    return False


def _title_matches_firm(text: str, canonical_name: str) -> bool:
    """PLAN.md T31.1 firm-match clause — does the title text read as the firm's name?
    Compares ``_core_name_phrase(text)`` against ``_core_name_phrase(canonical_name)``
    (both from T20): equal, or either containing the other, means the "title" is the
    company name. Used by ``_is_plausible_title`` (full check) and by
    ``_reconcile_principal_title`` (supersede-never-delete attribution), so a firm
    name in the title column is rejected the same way T27 rejects a person's name —
    with the record left in the ledger as ``superseded``.
    """
    title_phrase = _core_name_phrase(text)
    firm_phrase = _core_name_phrase(canonical_name)
    if not title_phrase or not firm_phrase:
        return False
    return (
        title_phrase == firm_phrase
        or title_phrase in firm_phrase
        or firm_phrase in title_phrase
    )


def _is_malformed_title(text: str | None) -> bool:
    """PLAN.md T31.2 — the projection-path gates that ``_reconcile_principal_title``
    cannot sensibly own. A value the MODEL asserted as a title (a present
    ``subject_value``) is projected and then superseded by reconcile if it is
    mis-attributed (a name or a firm name) — the ledger records what the model
    claimed. But a malformed value (empty/blank, longer than 80 characters, or a
    statement-of-absence) has no audit value as a superseded record, so projection
    drops it outright. ``_is_negative_answer`` is applied here even on the
    subject_value path because "No title could be determined" distilled as a
    subject_value is still not a title.
    """
    if not text or not text.strip():
        return True
    t = text.strip()
    if len(t) > 80:
        return True
    return _is_negative_answer(t)


def _is_plausible_title(
    text: str | None, principal_name: str | None, canonical_name: str | None
) -> bool:
    """PLAN.md T31.1 — is ``text`` a plausible job title?

    Returns False when any of these hold, True otherwise:
    * empty/blank after stripping;
    * longer than 80 characters — a real title is short ("Co-Founder, Managing
      Partner and Chief Investment Officer" is 57; MB's live "Managing Partner at
      MB Family Advisors, LLC" is 43), and anything longer is prose;
    * ``_person_names_match(text, principal_name)`` — a title is not the person's
      name (the existing T27 rule, now applied on the projection fallback path too);
    * it matches the firm (``_title_matches_firm``);
    * ``_is_negative_answer(text)`` — "No title could be determined" is not a title.

    The FULL check (including name/firm attribution) gates only the FALLBACK path of
    projection (a value WE derive from the answer text — we never mint a claim we
    already know is invalid). The MODEL-ASSERTED path (a present subject_value) gates
    only on the malformed subset (``_is_malformed_title``) and leaves name/firm
    attribution to ``_reconcile_principal_title``, which supersedes and keeps the
    audit record (PLAN.md T31 round 2).
    """
    if _is_malformed_title(text):
        return False
    t = text.strip()
    if _person_names_match(t, principal_name):
        return False
    if canonical_name and _title_matches_firm(t, canonical_name):
        return False
    return True


# PLAN.md T29.1: ordered role seniority for the principal pick. Each group is a set
# of substrings matched case-insensitively against the title; the FIRST group that
# matches wins (lower index = more senior). An unrecognised or missing title ranks
# `len(_ROLE_PRIORITY)` (last) — so an unknown role loses to a recognised one but a
# named principal with an unknown title still beats no principal at all (T29.2).
# Word-boundary guards (\b) stop short labels matching inside longer words: "ceo"
# must not match inside "Deceased"/"sociology"-style tokens, and "cio"/"coo"/"cfo"
# likewise (PLAN.md T29.1's substring trap).
_ROLE_PRIORITY: tuple[tuple[str, ...], ...] = (
    ("chief executive", "ceo"),
    ("managing partner",),
    ("managing member",),
    ("founder", "co-founder"),
    ("president",),
    ("chief investment officer", "cio"),
    ("managing director",),
    ("principal",),
    ("partner",),
    ("chief operating officer", "coo"),
    ("chief financial officer", "cfo"),
)

_ROLE_GROUP_RES: tuple[_re.Pattern[str], ...] = tuple(
    _re.compile(
        r"\b(?:" + r"|".join(_re.escape(term) for term in group) + r")\b",
        _re.IGNORECASE,
    )
    for group in _ROLE_PRIORITY
)


def _role_rank(title: str | None) -> int:
    """Rank a title by role seniority (PLAN.md T29.1). Returns the index of the first
    matching `_ROLE_PRIORITY` group, or `len(_ROLE_PRIORITY)` when the title is
    missing, blank, or matches no recognised role. Case-insensitive and
    word-boundary guarded so short labels ("ceo", "cio", ...) never match inside a
    longer word."""
    if not title or not title.strip():
        return len(_ROLE_PRIORITY)
    for idx, rx in enumerate(_ROLE_GROUP_RES):
        if rx.search(title):
            return idx
    return len(_ROLE_PRIORITY)


def _project_question_claims(
    claims: list[Claim], canonical_name: str | None = None
) -> list[Claim]:
    """Project Layer-1 question claims into field-name claims the row pivots on
    (PLAN.md T19.3). `claims` is the combined list the caller already holds — both the
    Layer-1 research claims (which carry `question_id` + `subject_value`) and any
    structured derivations produced so far (13F/5500/conference, which carry
    `field_name`). Only the question_id-bearing claims match the projection map; the
    structured ones are consulted only to decide what is already settled.

    For each Layer-1 claim whose question_id is in QUESTION_FIELD_PROJECTIONS (or G2.Q2
    -> principal_linkedin when subject_value is a linkedin.com/in/ URL), whose status is in
    `_PROJECTABLE_STATUSES` (`confirmed` / `single_source` / `verified`), and whose
    subject_value is a non-empty string: emit a _derived_claim
    with that field_name, answer=subject_value, extraction_method=f"projected_{qid}",
    confidence=`medium`, PRESERVING the original claim's source_url and source_class — the
    researcher's citation must survive the projection so the row stays auditable back to
    the page the fact came from. Skip a field already settled by a higher-confidence
    13F/5500/conference structured derivation. Never project a claim whose status is
    could_not_verify / contradicted / superseded (the `status not in
    _PROJECTABLE_STATUSES` guard covers all three and the other excluded statuses)."""
    # PLAN.md T31.2: the principal name used by the FALLBACK path's full
    # `_is_plausible_title` check (the name-match clause). The MODEL-ASSERTED path
    # does NOT use this — it leaves name/firm attribution to
    # `_reconcile_principal_title`, which supersedes and keeps the audit record.
    principal_name = next(
        (c.subject_value.strip() for c in claims
         if c.question_id == "G2.Q1"
         and isinstance(c.subject_value, str) and c.subject_value.strip()),
        None,
    )

    settled_by_structured = {
        c.field_name for c in claims
        if c.produced_by == "derived"
        and c.field_name
        and c.status not in ("could_not_verify", "removed_failed_validation", "superseded")
        and (c.extraction_method or "").startswith(("derived_13f", "derived_5500", "derived_conference"))
    }
    # PLAN.md T23.3: a lane may now emit several claims for one question (one per
    # supporting page). Project ONLY the winner `select_claim_for_question` picks —
    # the same rule the HARD gates use — so the row and the gate can never disagree
    # about which claim won, and we never project N claims onto one field_name.
    layer1_by_q: dict[str, list[Claim]] = {}
    for c in claims:
        qid = c.question_id
        if not qid:
            continue
        layer1_by_q.setdefault(qid, []).append(c)

    def _projectable(c: Claim) -> bool:
        """A Layer-1 claim that may be projected: projectable status AND a non-empty
        string subject_value to lift into the field_name claim."""
        if c.status not in _PROJECTABLE_STATUSES:
            return False
        sv = c.subject_value
        return isinstance(sv, str) and bool(sv.strip())

    def _t23_key(c: Claim) -> tuple[int, int, float, str]:
        # The EXACT tie-break `select_claim_for_question` uses (PLAN.md T23.2):
        # status rank, then most-recent retrieved_at, then claim_id. Re-implemented
        # here so the G2.Q1 seniority pick can layer role-rank AHEAD of this key
        # without changing the underlying tie-break.
        rank = _CLAIM_STATUS_RANK.get(c.status, 99)
        if c.retrieved_at is not None:
            ra: tuple[int, float] = (0, -c.retrieved_at.timestamp())
        else:
            ra = (1, 0.0)
        return (rank, ra[0], ra[1], c.claim_id)

    def _emit(c: Claim, qid: str, field_name: str, answer: str | None = None) -> Claim:
        # Preserve the original claim's timestamp alongside its url/source_class — the
        # date is part of the provenance the projection must carry (PLAN.md T19.3), and
        # `check_v5_staleness` skips any claim whose retrieved_at is None, so dropping it
        # would silently make a two-year-old researched principal look freshly found.
        # `_parse_dt` accepts an ISO string and returns None for None, so a Layer-1 claim
        # with no timestamp keeps behaving exactly as it did before this fix.
        # T30.1: `answer` defaults to `c.subject_value`; the prose-fallback path passes
        # the claim's `answer` instead (when the compress model omitted subject_value
        # for a prose field). The source provenance is preserved either way.
        if answer is None:
            answer = c.subject_value
        return _derived_claim(
            field_name=field_name, answer=answer,
            source={
                "url": c.source_url,
                "retrieved_at": c.retrieved_at.isoformat() if c.retrieved_at else None,
            },
            extraction_method=f"projected_{qid}", confidence="medium",
            source_class=c.source_class,
        )

    out: list[Claim] = []
    # PLAN.md T29.2: project G2.Q1 (principal_name) and G2.Q3 (principal_title) as a
    # PAIR keyed on shared `source_url`, selecting the principal whose paired title is
    # the most senior by `_role_rank`. This makes the pick DETERMINISTIC given a ledger
    # (the regression guard for "a different principal every run") and guarantees the
    # name and title describe the SAME person from the SAME page — closing the
    # coherence gap that produced "Chris Younger, Director of Wealth Advisory" (someone
    # else's title) on a prior run. Only the winning pair projects; remaining ties use
    # the existing T23.2 order (status, then retrieved_at, then claim_id).
    # A G2.Q1 with no paired title still projects (ranked last) — a named principal
    # with an unknown title beats no principal (PLAN.md T29.2).
    skip_pair_qids: set[str] = set()
    g2q1_proj = [c for c in layer1_by_q.get("G2.Q1", []) if _projectable(c)]
    if g2q1_proj:
        skip_pair_qids = {"G2.Q1", "G2.Q3"}
        # Projectable G2.Q3 titles grouped by their source_url, so a G2.Q1 can find the
        # title found on the SAME page. A G2.Q3 with no source_url cannot be paired and
        # is intentionally excluded — the whole point of T29.2 is name+title from the
        # same source.
        # T31.2: a G2.Q3 whose subject_value the model omitted is still pairable
        # by source_url — its `answer` is the title text on the fallback path. Only
        # the status gate (projectable status) and a source_url are required here;
        # the plausibility gate is applied at emission below.
        q3_by_url: dict[str, list[Claim]] = {}
        for c in layer1_by_q.get("G2.Q3", []):
            if c.status not in _PROJECTABLE_STATUSES or not c.source_url:
                continue
            q3_by_url.setdefault(c.source_url, []).append(c)

        def _q3_title_text(c: Claim | None) -> str | None:
            # The title text a G2.Q3 contributes: the model-distilled subject_value
            # when present, else the answer (the T31.2 fallback). Used both for the
            # seniority ranking and for the emission text.
            if c is None:
                return None
            sv = c.subject_value
            if isinstance(sv, str) and sv.strip():
                return sv.strip()
            ans = c.answer
            return str(ans).strip() if ans is not None else None

        # Rank each projectable G2.Q1 by its paired title's seniority (lower is better),
        # breaking ties with the T23.2 key on the G2.Q1 claim itself.
        ranked: list[tuple[int, tuple[int, int, float, str], Claim, Claim | None]] = []
        for q1 in g2q1_proj:
            paired_q3: Claim | None = None
            if q1.source_url and q1.source_url in q3_by_url:
                paired_q3 = select_claim_for_question(q3_by_url[q1.source_url])
            ranked.append((_role_rank(_q3_title_text(paired_q3)), _t23_key(q1), q1, paired_q3))
        ranked.sort(key=lambda t: (t[0], t[1]))
        _, _, best_q1, best_q3 = ranked[0]
        if "principal_name" not in settled_by_structured:
            out.append(_emit(best_q1, "G2.Q1", "principal_name"))
        if best_q3 is not None and "principal_title" not in settled_by_structured:
            title = _q3_title_text(best_q3)
            if title:
                has_q3_sv = isinstance(best_q3.subject_value, str) and bool(
                    best_q3.subject_value.strip()
                )
                if has_q3_sv:
                    # T31.2 MODEL-ASSERTED path: project the subject_value, gating
                    # only on malformed (empty/long/negative). Reconcile owns
                    # name/firm attribution and supersedes with the record kept.
                    if not _is_malformed_title(title):
                        out.append(_emit(best_q3, "G2.Q3", "principal_title", answer=title))
                else:
                    # T31.2 FALLBACK path: we derive the title from `answer` — mint
                    # it ONLY if fully plausible (name/firm/length/negation), because
                    # we do not manufacture a claim we already know is invalid.
                    if _is_plausible_title(title, best_q1.subject_value, canonical_name):
                        out.append(_emit(best_q3, "G2.Q3", "principal_title", answer=title))

    # All other questions (and G2.Q1/G2.Q3 when no projectable G2.Q1 exists, preserving
    # the pre-T29 behaviour for a lone G2.Q3): project the per-question winner the
    # gates themselves use (`select_claim_for_question`).
    for qid, group in layer1_by_q.items():
        if qid in skip_pair_qids:
            continue
        c = select_claim_for_question(group)
        if c is None:
            continue
        sv = c.subject_value
        has_sv = isinstance(sv, str) and bool(sv.strip())
        field_name = QUESTION_FIELD_PROJECTIONS.get(qid)

        if field_name == "principal_title":
            # PLAN.md T31.2 — principal_title has its own gating on BOTH paths.
            if c.status not in _PROJECTABLE_STATUSES:
                continue
            if has_sv:
                # MODEL-ASSERTED path: project the subject_value, gating only on
                # malformed (empty/long/negative). `_reconcile_principal_title`
                # supersedes a name/firm-as-title and keeps the audit record.
                text = sv.strip()
                if _is_malformed_title(text):
                    continue
            else:
                # FALLBACK path: we derive the title from `answer` — mint it ONLY if
                # fully plausible (name/firm/length/negation). We do not manufacture
                # a claim we already know is invalid.
                text = str(c.answer).strip() if c.answer is not None else ""
                if not text or not _is_plausible_title(text, principal_name, canonical_name):
                    continue
            if field_name in settled_by_structured:
                continue
            out.append(_emit(c, qid, field_name, answer=text))
            continue

        if has_sv:
            # Explicit subject_value path — the model distilled a bare value. This is
            # the only path that ever populates an entity field (principal_*).
            if c.status not in _PROJECTABLE_STATUSES:
                continue
            if field_name is None:
                # G2.Q2 -> principal_linkedin ONLY when subject_value is a
                # linkedin.com/in/ URL — a generic profile URL or a company LinkedIn
                # page is not a person's LinkedIn field (PLAN.md T19.3).
                if qid == "G2.Q2" and "linkedin.com/in/" in sv.lower():
                    field_name = "principal_linkedin"
                else:
                    continue
            answer = sv
        else:
            # PLAN.md T30.1: prose fallback. When subject_value is missing/blank and
            # the target field is a prose-valued field (important_insight /
            # recent_investments), fall back to the claim's `answer` — the compress
            # model routinely omits subject_value for prose answers because the bare
            # value and the answer are the same kind of thing (G3.Q2 is 0-for-6 on
            # the live ledger). Entity fields (principal_name`, `principal_linkedin`)
            # NEVER fall back — a whole sentence in principal_name is the T26/T27/T29
            # defect. The T30.2 negation guard applies ONLY to this fallback, never
            # to an explicit subject_value.
            if field_name not in _PROSE_PROJECTION_FIELDS:
                continue
            if c.status not in _PROJECTABLE_STATUSES:
                continue
            prose = str(c.answer).strip() if c.answer is not None else ""
            if not prose or _is_negative_answer(prose):
                continue
            answer = prose
        if field_name in settled_by_structured:
            continue
        out.append(_emit(c, qid, field_name, answer=answer))
    return out


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
        field_name="important_insight", answer=trigger, source=f13,
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
    """important_insight=access_window — a future-dated conference sighting is a live
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
            field_name="important_insight", answer="access_window", source=conf,
            extraction_method="derived_conference", confidence="medium",
        ))
    return claims


def _principal_from_people(
    sources: list[dict[str, Any]]
) -> tuple[list[Claim], dict[str, str]]:
    """principal_name / principal_title from discovery-class pass-through payloads
    carrying a "people" list (fec_employer, ppp_loans, and similar — see
    app/ingest.py's _map_source fallback branch). Payload shape for these classes is
    whatever the raw discovery feed carried, not a normalized schema, so this reads
    defensively (name/full_name, title/occupation/role) and skips anyone with no name
    rather than guessing at field names that aren't there.

    Returns ``(claims, title_owner_by_claim_id)``. The second element is a LOCAL
    structure (no schema change) mapping each emitted ``principal_title`` claim's
    ``claim_id`` to the person-name it came from, so a later step
    (``_reconcile_principal_title``) can tell whose title each claim is — the whole
    point of PLAN.md T27.2. A title emitted here has no inherent link to its
    principal_name sibling otherwise."""
    claims: list[Claim] = []
    title_owner_by_claim_id: dict[str, str] = {}
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
                title_claim = _derived_claim(
                    field_name="principal_title", answer=title, source=s,
                    extraction_method=f"derived_{s['source_class']}",
                )
                claims.append(title_claim)
                title_owner_by_claim_id[title_claim.claim_id] = name
            break  # first named person only — a full roster isn't wave -1's job
    return claims, title_owner_by_claim_id


def _person_names_match(a: str | None, b: str | None) -> bool:
    """PLAN.md T27.1 — does name `a` refer to the same person as name `b`?

    Normalise each name to a set of alphabetic tokens of length >= 2 (casefold,
    punctuation stripped: ``"TULLMAN, CAYLEY ELYSE"`` -> ``{tullman, cayley, elyse}``,
    ``"Glen Tullman"`` -> ``{glen, tullman}``). Match when either token set is a
    **subset** of the other:
    - ``"TULLMAN, GLEN"`` matches ``"Glen Tullman"`` (equal sets);
    - ``"TULLMAN, CAYLEY ELYSE"`` does NOT match ``"Glen Tullman"`` (different given
      names — neither is a subset, so a shared surname alone never matches);
    - ``"Santiago Ulloa"`` does not match ``"ORTEGA, ROCIO"``.
    A single-token name matches an identical token or a superset containing it.
    Empty/None on either side -> False.
    """
    if not a or not b:
        return False

    def _tokens(name: str) -> set[str]:
        return {tok.lower() for tok in _re.findall(r"[A-Za-z]+", name) if len(tok) >= 2}

    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return ta.issubset(tb) or tb.issubset(ta)


def _reconcile_principal_title(
    claims: list[Claim],
    title_owner_by_claim_id: dict[str, str],
    canonical_name: str | None = None,
) -> list[Claim]:
    """PLAN.md T27.2 — attribute discovery-feed titles to the person they belong to.

    The user's rule: if the researched principal name IS in the discovery input, take
    that person's title from the input; if it is NOT, leave ``principal_title`` unsettled
    so wave 1's ``_find_role_currency`` searches the name together with the firm. Today
    wave -1's ``_principal_from_people`` emits a ``principal_name`` and a
    ``principal_title`` per source row with no link between them, and wave 1's guard
    (``if principal_name and "principal_title" not in settled``) then never runs the
    search tier because an arbitrary donor's title has already settled the field —
    dead code on the whole ``fec_employer`` path. This reconciler restores the link.

    Called at the END of ``wave_minus_1`` (after ``_project_question_claims``, so the
    researched principal is known). Steps, in order:
    1. Pick the winning ``principal_name`` by the T26 provenance ranking
       (``projected_`` > ``derived_`` > other); ties keep first-seen.
    2. **Type guard** — mark ``superseded`` any ``principal_title`` whose value
       ``_person_names_match``es the winning ``principal_name``. A title is not a
       name; this is what stops T26 promoting ``"Glen Tullman"`` into the title column.
    3. Keep a ``derived_`` title ONLY when its owner (looked up in
       ``title_owner_by_claim_id``) ``_person_names_match``es the winning
       ``principal_name``. Mark every other ``derived_`` title ``superseded`` — do
       NOT delete: the ledger keeps the evidence and ``_settled_fields`` already
       ignores that status.
    4. Keep ``projected_``/other titles that survived step 2 untouched.

    If nothing survives, ``principal_title`` is left unsettled so wave 1's
    ``_find_role_currency`` runs — the user's second rule.
    """
    # 1. Winning principal_name by the T26 provenance ranking. `min` returns the FIRST
    #    minimum element, so ties keep first-seen.
    name_claims = [
        c for c in claims
        if c.field_name == "principal_name" and isinstance(c.answer, str) and c.answer.strip()
    ]
    winning_name: str | None = None
    if name_claims:
        winner = min(name_claims, key=lambda c: _provenance_rank(c.extraction_method))
        if isinstance(winner.answer, str) and winner.answer.strip():
            winning_name = winner.answer
    # T31 round 2: when there is no principal_name to attribute against, we still
    # run the loop so the firm-match clause can supersede a firm-name-as-title
    # (e.g. WE FAMILY's "WE Family Offices" with no G2.Q1). Only bail when there
    # is neither a winning name nor a canonical name to check against.
    if not winning_name and not canonical_name:
        return claims

    out: list[Claim] = []
    for c in claims:
        if c.field_name != "principal_title":
            out.append(c)
            continue
        value = c.answer if isinstance(c.answer, str) else None
        # 2a. Type guard — a title that IS the principal's name is not a title.
        if winning_name and value and _person_names_match(value, winning_name):
            out.append(c.model_copy(update={"status": "superseded"}))
            continue
        # 2b. T31 firm guard — a title that IS the firm's name is not a title.
        # Same treatment as the name guard: supersede, never delete, so the ledger
        # records what the model asserted (PLAN.md T31 round 2).
        if canonical_name and value and _title_matches_firm(value, canonical_name):
            out.append(c.model_copy(update={"status": "superseded"}))
            continue
        method = c.extraction_method or ""
        if method.startswith("derived_"):
            # 3. Keep a derived title ONLY when its owner matches the winning
            #    principal. Every other derived title is superseded (never deleted).
            #    Without a winning principal there is nobody to attribute against,
            #    so keep derived titles untouched (pre-T31 behaviour).
            if winning_name:
                owner = title_owner_by_claim_id.get(c.claim_id)
                if owner and _person_names_match(owner, winning_name):
                    out.append(c)
                else:
                    out.append(c.model_copy(update={"status": "superseded"}))
            else:
                out.append(c)
        else:
            # 4. projected_/other titles that survived the type/firm guards are kept.
            out.append(c)
    return out


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
    layer1_claims: list[Claim] | None = None,
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

    `layer1_claims` (PLAN.md T19.3): the Layer-1 research claims for this entity, when the
    caller already holds them (process_entity / run_pipeline read them from the DB).
    Question claims confirmed with a `subject_value` are projected into field-name
    claims the row pivots on (G2.Q1 -> principal_name, etc.), preserving the original
    source_url/source_class. Omitted by callers/tests that don't need the projection.
    """
    today = today or datetime.now(timezone.utc).date()
    claims: list[Claim] = []

    f13_rows = _by_class(entity_sources, "13f_filing")
    if f13 := _latest(f13_rows):
        claims.extend(_aum_claims(f13))
        if trigger := _why_now_from_13f_delta(f13):
            claims.append(trigger)
    else:
        # PLAN.md T19.5: trust the source's own AUM. A 13F is a holdings floor for a
        # stated quarter; RAUM is self-reported — so the 13F wins when both exist, and
        # the ADV RAUM is emitted only when no 13F row is present.
        adv_rows = _by_class(entity_sources, "adv_name")
        if adv := _latest(adv_rows):
            claims.extend(_adv_aum_claims(adv))

    f5500_rows = _by_class(entity_sources, "5500_filing")
    if f5500 := _latest(f5500_rows):
        if headcount := _headcount_claim(f5500):
            claims.append(headcount)

    conf_rows = _by_class(entity_sources, "conference_sighting")
    claims.extend(_access_window_claims(conf_rows, today))

    people_claims, title_owner_by_claim_id = _principal_from_people(entity_sources)
    claims.extend(people_claims)
    claims.extend(_discovery_class_claims(entity_sources))

    if overlap := _public_list_overlap_claim(canonical_name, aliases, public_list):
        claims.append(overlap)

    # PLAN.md T19.3: project confirmed Layer-1 question claims into field-name claims
    # AFTER the structured derivations, so a field already settled by a 13F/5500/
    # conference derivation is not overwritten by a lower-confidence projection.
    if layer1_claims:
        claims.extend(_project_question_claims([*claims, *layer1_claims], canonical_name))

    # PLAN.md T27.2: attribute discovery-feed titles to the person they belong to,
    # AFTER the projection so the researched (projected_) principal is known. Runs
    # before the entity_id re-stamping below so the title_owner_by_claim_id mapping
    # (keyed on the uuid4 claim_ids _principal_from_people emitted) still resolves.
    # T31 round 2: ``canonical_name`` threads the firm-match guard so a firm name
    # in the title column is superseded the same way a person's name is.
    claims = _reconcile_principal_title(claims, title_owner_by_claim_id, canonical_name)

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
    claims: list[Claim], canonical_name: str, injected_facts: dict[str, Any] | None = None,
    model: Any = None,
) -> tuple[list[Claim], list[Finding], bool]:
    """Returns (annotated_claims, findings, fatal). `fatal=True` means the caller
    (run_pipeline) should reject immediately rather than spend wave 1/2 budget — Layer V
    would reject it on the same grounds regardless.

    `model` (optional, T33) is the cheapest-tier model used to judge G1.Q3/G1.Q5 answer
    polarity before the firm-is-FO gate. `None` (the test default) skips the judge and the
    gate falls back to its deterministic string floor — so the existing 2-arg calls of
    ``wave_0(claims, name)`` behave exactly as before. Production passes
    ``get_model(SETTINGS.models.validation_tier)`` (PLAN.md T33.1)."""
    injected_facts = injected_facts or {}
    findings: list[Finding] = []

    claims, v4_findings = check_v4_contradictions(claims)
    findings.extend(v4_findings)
    findings.extend(check_v5_staleness(claims))
    polarity = await _judge_claim_polarity(claims, model)
    findings.extend(check_v5_firm_is_fo_hardening(claims, polarity))

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
# Domain resolution — the one input every wave-1 contact tier is gated on.
#
# `injected_facts["domain"]` is derived from a `domain_check` entity_source
# (app/parser.py:66-68) and NOTHING in this codebase writes one: app/ingest.py's
# _map_source only ever emits 13f_filing / fec_employer / 5500_filing / ppp_loans, so the
# domain was None for all 2,644 ingested entities. Consequence, measured on the 2026-08-12
# 10-lead pilot: wave 1's four domain-gated tiers had never executed once, `principal_email`
# and `principal_phone` had never been produced in the project's history, and because
# `have_channel` was therefore permanently False, wave_1 always gated and **wave 2 had never
# run for any entity** — taking its 13 depth fields down with it. See PLAN.md T16.
#
# So: resolve a domain from a Serper organic search when ingestion didn't supply one. The
# firm's own site is result #1 for these long-tail RIA/family-office names (verified live
# for ICG Advisors / IMPACTfolio / IMZ Advisory), but a SERP is untrusted input — an
# unrelated firm's domain attributed to this entity would poison every contact claim
# derived from it, the same false-positive class app/tools/adv.py::_name_match exists for.
# Hence both guards below: skip known aggregators, and require a real name match.
# ============================================================================

# Hosts that rank for a firm name without being the firm. Directory/profile aggregators
# (fintrx, aum13f, radientanalytics, bizapedia…) are the dangerous ones: they rank highly
# for exactly these long-tail adviser names and their domains would never trip the
# name-match guard on their own.
_AGGREGATOR_HOSTS = frozenset({
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "yelp.com", "wikipedia.org", "crunchbase.com",
    "bloomberg.com", "reuters.com", "forbes.com", "zoominfo.com", "dnb.com",
    "glassdoor.com", "indeed.com", "bbb.org", "sec.gov", "google.com",
    "fintrx.com", "radientanalytics.com", "aum13f.com", "usnews.com",
    "opencorporates.com", "manta.com", "corporationwiki.com", "buzzfile.com",
    "mapquest.com", "bizapedia.com", "apollo.io", "rocketreach.co", "signalhire.com",
    "brokercheck.finra.org", "finra.org", "wealthminder.com", "investor.gov",
})

_NON_ALNUM_RE = _re.compile(r"[^a-z0-9]")


def _host_from_url(url: str) -> str | None:
    """Lowercased registrable host with scheme, port, credentials and a leading `www.`
    stripped. None for anything that isn't an http(s) URL with a host."""
    try:
        parts = _urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


def _is_aggregator(host: str) -> bool:
    return any(host == a or host.endswith(f".{a}") for a in _AGGREGATOR_HOSTS)


def _domain_from_sources(entity_sources: list[dict[str, Any]]) -> str | None:
    """The entity's own web domain straight from the discovery record, no Serper call
    (PLAN.md T19.4). An adv_name row's `signals.website` is the firm's self-reported
    domain — it arrives upper-cased in real data
    (e.g. `HTTPS://WWW.CLASSVIPARTNERS.COM`), so this normalises to a bare lowercase host
    (strip scheme, `www.`, path). Rejected and falls through to resolve_domain when the
    field holds an aggregator (verified live: ARDEN GLOBAL FAMILY OFFICES carries
    `HTTPS://WWW.LINKEDIN.COM/COMPANY/ARDENWOOD-ADVISORS` in that field) or is None
    (MB FAMILY ADVISORS). This is not re-verification of the fact; it is a check that the
    field contains a firm domain at all — the source class IS the provenance."""
    adv_rows = _by_class(entity_sources, "adv_name")
    adv = _latest(adv_rows)
    if adv is None:
        return None
    signals = adv["payload"].get("signals") or {}
    website = signals.get("website")
    if not isinstance(website, str) or not website.strip():
        return None
    host = _host_from_url(website) if "://" in website else _host_from_url(f"https://{website}")
    if not host or _is_aggregator(host):
        return None
    return host


def _domain_matches_entity(host: str, canonical_name: str) -> bool:
    """Does this host plausibly BELONG to this entity?

    Compares the domain's leading label against the entity's distinctive name tokens
    (`adv.py::distinctive_name_tokens` — "Family"/"Office"/"LLC" and friends excluded,
    since they match hundreds of unrelated advisers). A bare substring test is too loose
    for the short acronym tokens these names are full of ("IFS" would match
    "clifshire.com"), so a token has to either anchor the label or be long enough that an
    incidental hit is implausible:

        ICG Advisors, LLC   -> {"icg"}          icgadvisors.com          startswith ✓
        IMPACTfolio, LLC    -> {"impactfolio"}  impactfolio.co           equals     ✓
        IMZ Advisory Inc    -> {"imz"}          imzfinancialservices.com startswith ✓
    """
    tokens = distinctive_name_tokens(canonical_name)
    if not tokens:
        return False
    label = _NON_ALNUM_RE.sub("", host.split(".")[0])
    if not label:
        return False
    for token in tokens:
        token = _NON_ALNUM_RE.sub("", token)
        if not token:
            continue
        if label == token or label.startswith(token):
            return True
        if len(token) >= 5 and token in label:
            return True
    return False


async def resolve_domain(canonical_name: str) -> str | None:
    """The entity's own web domain from one Serper organic search, or None.

    None is a normal outcome, not an error: a Serper failure, an all-aggregator SERP, and
    "this firm genuinely has no website" all land here, and every caller must treat it the
    same way it already treats a missing `domain_check` source — by skipping the tiers that
    need one rather than guessing at a domain.
    """
    search = await serper_search_raw(f'"{canonical_name}"', max_results=5)
    for result in search.get("results", []):
        host = _host_from_url(result.get("url") or "")
        if not host or _is_aggregator(host):
            continue
        if _domain_matches_entity(host, canonical_name):
            return host
    return None


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


# Generic firm inboxes are not a person's address. A contact page that offers only
# `info@` / `contact@` / `admin@` is a channel for the firm, not the decision-maker, so
# attributing it to `principal_email` asserts a named principal's address on a row that
# may not even name a principal (the Class VI `info@classvipartners.com` row). These are
# relabelled to `firm_email` — same contact, honestly labelled. The local part is matched
# case-insensitively, before any `+suffix` (so `Info+sales@firm.com` is still a role address).
_ROLE_LOCAL_PARTS = frozenset({
    "info", "contact", "contacts", "hello", "admin", "office", "team", "support",
    "help", "enquiries", "inquiries", "press", "media", "careers", "jobs", "sales",
    "noreply", "no-reply", "donotreply", "webmaster", "mail", "general",
})


def _is_role_address(email: str) -> bool:
    """True when the local part of `email` is a generic firm-inbox role word.
    Case-insensitive; any `+suffix` is stripped before the comparison."""
    if not email or "@" not in email:
        return False
    local = email.split("@", 1)[0].split("+", 1)[0].lower()
    return local in _ROLE_LOCAL_PARTS


def _email_matches_domain(email: str, domain: str) -> bool:
    """True when `email`'s host is the queried `domain` or a subdomain of it.

    Snov.io's name-targeted and domain-wide endpoints are NOT domain-scoped: a single
    lookup can return addresses at other companies (observed live 2026-08-13 — a
    `Matt Blackburn @ classvipartners.com` query returned `Matt@capitalvalue.net`
    alongside the correct `Matt@classvipartners.com`). Accepting such an address would
    present a different person at a different company as this principal's contact,
    which is the exact failure this project exists to prevent. `_find_email_on_site`
    already performs this check inline; only the Snov paths omitted it.

    Both sides are casefolded and a leading `www.` is stripped, so
    `matt@mail.classvipartners.com` matches `classvipartners.com` (subdomain) and
    `Matt@capitalvalue.net` does not match `classvipartners.com` (different host).
    An off-domain address is not evidence about this firm at any confidence, so the
    caller drops it entirely rather than emitting, downgrading, or relabelling it."""
    if not email or "@" not in email or not domain:
        return False
    host = email.rsplit("@", 1)[-1].strip().lower()
    dom = domain.strip().lower()
    if host.startswith("www."):
        host = host[4:]
    if dom.startswith("www."):
        dom = dom[4:]
    return host == dom or host.endswith("." + dom)


def _email_field_name(email: str, principal_name: str | None) -> str:
    """Decide which column an discovered address belongs to.

    * A role address (`info@`, `contact@`, ...) is always `firm_email` — it is the firm's
      inbox, not a named principal's, regardless of whether a principal is known.
    * A personal-looking address is `principal_email` only when a principal is actually
      named on the row; with no principal, even a personal-looking address can't be
      attributed to a decision-maker and is recorded as `firm_email` instead.
    """
    if _is_role_address(email):
        return "firm_email"
    return "principal_email" if principal_name else "firm_email"


async def _find_email_on_site(domain: str, principal_name: str | None = None) -> Claim | None:
    """Tier 1 — free fetch of common contact/team pages, first @domain email found.

    A generic firm inbox (`info@`, `contact@`, ...) is emitted as `firm_email`, not
    `principal_email`, and when no principal is named on the row any address found is
    `firm_email` — a contact channel with nobody attached is not a personal address."""
    for path in _COMMON_SITE_PATHS:
        url = f"https://{domain}{path}"
        fetched = await fetch_page_free_first(url)
        content = fetched.get("content") or ""
        match = _EMAIL_RE.search(content)
        if match and domain.lower() in match.group(0).lower():
            email = match.group(0)
            return Claim(
                field_name=_email_field_name(email, principal_name), answer=email, status="confirmed",
                source_url=url, source_class="site_scrape",
                extraction_method=fetched.get("extraction_method", "site_scrape"),
                confidence="medium", produced_by="enrichment", wave="1",
            )
    return None


def _split_name(principal_name: str | None) -> tuple[str, str]:
    """First/last split for a name-targeted email lookup. Snov.io's
    emails-by-domain-by-name wants both parts; a single-token name goes in as the last
    name, which is the half that actually drives the match."""
    parts = (principal_name or "").split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    return parts[0], parts[-1]


async def _find_email_via_snov(domain: str, principal_name: str | None) -> Claim | None:
    """Tier 3 — Snov.io email discovery (replaced Hunter.io).

    Two shapes, tried in order:
      1. name-targeted (`emails-by-domain-by-name`) when a principal is already known —
         the surname match happens server-side, so a hit is directly attributable to the
         decision-maker rather than to "someone at this domain".
      2. domain-wide (`domain-search/domain-emails`) otherwise, or when (1) misses. This is
         the closest analogue to Hunter's old domain search, and like it the result is only
         "low" confidence: an address on the right domain is not evidence about the right
         person.

    `confidence` is carried from Snov.io's own 0-100 score where present; anything under 70
    stays "low" so a weak pattern-guess can't be presented as a solid contact.
    `emails-by-domain-by-name` does not actually return a numeric `confidence` — it returns
    `smtp_status`, and `smtp_status == "valid"` is the strongest deliverability signal the
    endpoint offers (verified live 2026-08-12: `marc@msdcapital.com`, SMTP-valid and
    name-matched, was being graded `"low"`). Treat `smtp_status == "valid"` as strong
    (`"medium"`) in addition to the existing numeric rule.
    """
    first, last = _split_name(principal_name)
    # T32.1: track whether ANY Snov call returned an `error` key, and whether either
    # tier returned ANY row at all. An outage (HTTP 402 credits exhausted) and a firm
    # with genuinely no published address are otherwise byte-identical: both fall
    # through the `for row in ...get("results") or []` loops to `return None`. Surfacing
    # the failure as a `could_not_verify` claim (below) lets the row say "we could not
    # look" instead of silently reporting an absence — the same distinction
    # app/researcher.py's `_tag_evidence_gaps` already draws with the `tool_unavailable`
    # vs `no_evidence_found` vocabulary. `saw_any_row` keeps the pre-T32 `None` return
    # for the distinct case where Snov DID answer (rows present) but every row was
    # off-domain and filtered out by `_email_matches_domain` — that is "Snov returned
    # the wrong company", not "no evidence", and is the behaviour tests/test_email_domain_guard.py
    # guards (an off-domain row yields no claim).
    errors: list[str] = []
    saw_any_row = False

    if last:
        targeted = await snov_emails_by_name_domain_raw(first, last, domain)
        if targeted.get("error"):
            errors.append(str(targeted["error"]))
        targeted_rows = targeted.get("results") or []
        if targeted_rows:
            saw_any_row = True
        for row in targeted_rows:
            email = row.get("email")
            if not email:
                continue
            # T22.2: a Snov row at a different company is not evidence about this firm
            # at any confidence — drop it and keep scanning for an on-domain row rather
            # than emitting/downgrading/relabeling it. (Live 2026-08-13: a Matt Blackburn
            # @ classvipartners.com query returned Matt@capitalvalue.net first, then the
            # correct Matt@classvipartners.com — both rows are preserved by
            # _flatten_envelopes, so skipping the off-domain one unearths the valid hit.)
            # Runs before _email_field_name so an off-domain role address can't slip in as
            # firm_email.
            if not _email_matches_domain(email, domain):
                continue
            score = row.get("confidence")
            smtp_valid = str(row.get("smtp_status") or "").lower() == "valid"
            strong = (isinstance(score, (int, float)) and score >= 70) or smtp_valid
            return Claim(
                field_name=_email_field_name(email, principal_name), answer=email, status="confirmed",
                source_url=f"https://{domain}", source_class="snov",
                extraction_method="snov_emails_by_name_domain",
                confidence="medium" if strong else "low",
                produced_by="enrichment", wave="1",
            )

    domain_wide = await snov_domain_search_raw(domain)
    if domain_wide.get("error"):
        errors.append(str(domain_wide["error"]))
    rows = domain_wide.get("results") or []
    if rows:
        saw_any_row = True
    # A domain-wide hit is preferred only if it matches the known surname; otherwise take
    # the first address but score it "low" — same policy the Hunter version used. A generic
    # firm inbox (`info@`, `contact@`, ...) is `firm_email` regardless of surname match, and
    # with no principal named any address found is `firm_email` — it is the firm's channel,
    # not an attributable personal address.
    # T22.2: the domain guard runs first (before the surname check and before
    # _email_field_name) on every row — an off-domain row is skipped entirely, never
    # emitted as principal_email or firm_email.
    if last:
        for row in rows:
            email = row.get("email") or row.get("value")
            if email and last.lower() in str(email).lower() and _email_matches_domain(email, domain):
                return Claim(
                    field_name=_email_field_name(email, principal_name), answer=email, status="confirmed",
                    source_url=f"https://{domain}", source_class="snov",
                    extraction_method="snov_domain_search", confidence="medium",
                    produced_by="enrichment", wave="1",
                )
    for row in rows:
        email = row.get("email") or row.get("value")
        if email and _email_matches_domain(email, domain):
            return Claim(
                field_name=_email_field_name(email, principal_name), answer=email, status="confirmed",
                source_url=f"https://{domain}", source_class="snov",
                extraction_method="snov_domain_search", confidence="low",
                produced_by="enrichment", wave="1",
            )
    # T32.1: no usable address found. Distinguish "the tool failed" from "the tool
    # answered and there was nothing". `answer=None` (NOT the error text) because
    # `_records_rows` renders a `could_not_verify` claim's `answer` into the cell — the
    # error message would otherwise print in the `principal_email` column. The error
    # string survives in `verification_method` (free-text, unset by any producer on this
    # path) so the ledger records why the look failed without reaching the sheet.
    # `saw_any_row` keeps the pre-T32 `None` return when Snov DID answer but every row was
    # off-domain (filtered out by `_email_matches_domain`) — that is not "no evidence",
    # and tests/test_email_domain_guard.py guards it (an off-domain row yields no claim).
    if errors:
        return Claim(
            field_name="principal_email", answer=None, status="could_not_verify",
            source_url=f"https://{domain}", source_class="tool_unavailable",
            extraction_method="snov_error", confidence="low",
            produced_by="enrichment", wave="1",
            verification_method="; ".join(errors),
        )
    if saw_any_row:
        return None
    return Claim(
        field_name="principal_email", answer=None, status="could_not_verify",
        source_url=f"https://{domain}", source_class="no_evidence_found",
        extraction_method="snov_no_match", confidence="low",
        produced_by="enrichment", wave="1",
    )


async def _find_phone_via_site(domain: str) -> Claim | None:
    """Tier 2 — free fetch of common contact pages, first phone-shaped match. Format-
    valid only, never line-verified (research_layer_plan.md §4.7's Layer-2 note) — so
    this is explicitly status="format_only", not "confirmed"."""
    for path in ("/contact", "/about", "/team"):
        url = f"https://{domain}{path}"
        fetched = await fetch_page_free_first(url)
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


_LEGAL_SUFFIX_RE = _re.compile(
    r"\s*,?\s*\b(?:L\.L\.C\.|LLC|INC\.?|LP|LLP|LTD|CORP|CO)\s*$",
    _re.IGNORECASE,
)


def _normalise_entity_text(s: str) -> str:
    """Casefold, replace punctuation with space, collapse whitespace, strip — for the
    contiguous-substring entity-mention gate (T20.1)."""
    s = (s or "").casefold()
    s = _re.sub(r"[^\w\s]", " ", s)
    s = _re.sub(r"\s+", " ", s).strip()
    return s


def _core_name_phrase(name: str) -> str:
    """The canonical name (or alias) with a trailing legal suffix removed, then
    normalised — the contiguous phrase `_mentions_entity` requires to appear in the
    article text. Suffix list per PLAN.md T20: LLC, L.L.C., INC, INC., LP, LLP, LTD,
    CORP, CO (and an optional trailing comma before it). The leading `\b` keeps a
    suffix like CO from biting the end of a word such as 'DISCO'."""
    return _normalise_entity_text(_LEGAL_SUFFIX_RE.sub("", name or ""))


def _result_mention_text(r: dict[str, Any]) -> str:
    """Title plus any snippet/description/content the result carries — the full text the
    entity-mention gate runs against (T20.1)."""
    parts = [r.get("title") or ""]
    for key in ("snippet", "description", "content"):
        v = r.get(key)
        if isinstance(v, str):
            parts.append(v)
    return " ".join(parts)


def _mentions_entity(text: str, canonical_name: str, aliases: list[str] | None) -> bool:
    """T20.1 entity-mention gate. Normalise both sides (casefold, strip punctuation,
    collapse whitespace) and require the article text to contain the entity's **core
    name phrase** — the canonical name with a trailing legal suffix removed — as a
    contiguous substring. Any alias, normalised the same way, also satisfies the gate.

    Deliberate recall trade-off (PLAN.md T20.1): requiring the contiguous core phrase
    drops a real article that calls the firm only 'Class VI Partners'. Single
    distinctive tokens are NOT a safe fallback — the distinctive tokens of 'CLASS VI
    FAMILY OFFICE' are 'class'/'vi', which match almost anything. A missed article
    costs a blank cell; a wrong one ships a falsehood with a citation, which is worse.
    """
    norm_text = _normalise_entity_text(text)
    if not norm_text:
        return False
    phrases = [_core_name_phrase(canonical_name)]
    for alias in aliases or []:
        phrases.append(_core_name_phrase(alias))
    return any(p and p in norm_text for p in phrases)


async def _find_dated_signal(
    canonical_name: str, aliases: list[str] | None = None
) -> Claim | None:
    """Tier 2 GDELT, tier 3 Serper /news — both carry a real dated field (seendate /
    date), which is the whole point of a 'dated signal' claim. A news article is
    evidence a story exists, not a statement the firm made an investment, so T20.2
    emits it on `recent_news` (never `recent_investments`). T20.1 skips any result that
    doesn't actually mention the entity in its title/snippet/description."""
    gdelt = await news_search_raw(f'"{canonical_name}"', lookback_days=180, max_records=5)
    for r in gdelt.get("results", []):
        if not _mentions_entity(_result_mention_text(r), canonical_name, aliases):
            continue
        if r.get("url") and r.get("seendate"):
            return Claim(
                field_name="recent_news", answer=r.get("title") or r["url"], status="confirmed",
                source_url=r["url"], source_class="gdelt", extraction_method="gdelt_docapi",
                confidence="low", produced_by="enrichment", wave="1",
                retrieved_at=_parse_dt(r.get("seendate")),
            )
    news = await serper_search_raw(f'"{canonical_name}"', topic="news", max_results=5)
    for r in news.get("results", []):
        if not _mentions_entity(_result_mention_text(r), canonical_name, aliases):
            continue
        if r.get("url"):
            return Claim(
                field_name="recent_news", answer=r.get("title") or r["url"], status="confirmed",
                source_url=r["url"], source_class="news_article", extraction_method="serper_news",
                confidence="low", produced_by="enrichment", wave="1",
            )
    return None


def _settled_fields(claims: list[Claim]) -> set[str]:
    return {
        c.field_name for c in claims
        if c.field_name
        and c.status not in ("could_not_verify", "removed_failed_validation", "superseded")
    }


def _select_principal_name(claims: list[Claim]) -> str | None:
    """The principal_name wave 1 should search/attribute against.

    PLAN.md T27.3: this used to be `next(...)` — first principal_name claim in list
    order. On a `fec_employer` lead that order is the discovery-feed donor (a
    `derived_fec_employer` claim appended before the `projected_G2.Q1` projection),
    so wave 1 searched the DONOR's name together with the firm instead of the
    researched principal — the fall-through fired but with the wrong name, defeating
    the user's rule ("use the extracted name and search for it with the corporate
    name"). Pick the T26 provenance winner instead (`projected_` > `derived_` > other;
    ties keep first-seen), the SAME ranking `_reconcile_principal_title` and the
    shipped row use, so the name wave 1 acts on can never disagree with the name the
    row ships. `min` returns the first minimum element, so first-seen wins ties.
    """
    candidates = [
        c for c in claims
        if c.field_name == "principal_name"
        and isinstance(c.answer, str)
        and c.answer.strip()
        and c.status not in ("could_not_verify", "removed_failed_validation", "superseded")
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: _provenance_rank(c.extraction_method)).answer


async def wave_1(
    claims: list[Claim], canonical_name: str, domain: str | None = None,
    aliases: list[str] | None = None,
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

    principal_name = _select_principal_name([*claims, *new_claims])

    if principal_name and "principal_title" not in settled:
        if claim := await _find_role_currency(canonical_name, principal_name):
            new_claims.append(claim)
            settled.add("principal_title")

    if domain and "principal_email" not in settled:
        # T21.5: with a principal named, the Snov name-targeted lookup is attributable to
        # the decision-maker and the site scrape is not — run Snov first so a site-scraped
        # `info@` can't short-circuit the good tier. With no principal named, any address
        # found (site or Snov domain-wide) is `firm_email`, never `principal_email`.
        if principal_name:
            claim = await _find_email_via_snov(domain, principal_name)
            # T32: Snov may now return a `could_not_verify` claim (`tool_unavailable` /
            # `no_evidence_found`) instead of None. Fall back to the site tier whenever
            # Snov did not produce a usable (confirmed) address — a real address or
            # `firm_email` the site finds is a genuine channel, not an absence, and
            # surfacing a `tool_unavailable` claim while the site could have found the
            # address would itself be "an outage looking like an absence". Only when the
            # site tier ALSO finds nothing does the Snov `could_not_verify` claim
            # survive, so a true outage is surfaced rather than silently looking like
            # the firm published nothing (PLAN.md T32).
            if claim is None or claim.status == "could_not_verify":
                site_claim = await _find_email_on_site(domain, principal_name)
                if site_claim is not None:
                    claim = site_claim
        else:
            claim = await _find_email_on_site(domain, principal_name)
            if claim is None:
                claim = await _find_email_via_snov(domain, principal_name)
        if claim:
            new_claims.append(claim)
            # T32.2: an unresolved claim (`could_not_verify` / `removed_failed_validation` /
            # `superseded`) must NOT settle its field — the same statuses `_settled_fields`
            # already excludes. Without this guard a `tool_unavailable` `principal_email`
            # claim would enter `settled` and flip `have_channel` (T21.5) to True on a lead
            # with no contact channel at all, wrongly ungating wave 2 and spending money
            # on a dead lead.
            if claim.status not in ("could_not_verify", "removed_failed_validation", "superseded"):
                settled.add(claim.field_name)

    if "principal_phone" not in settled and domain:
        if claim := await _find_phone_via_site(domain):
            new_claims.append(claim)
            settled.add("principal_phone")

    if "recent_news" not in settled and "important_insight" not in settled:
        if claim := await _find_dated_signal(canonical_name, aliases):
            new_claims.append(claim)
            settled.add("recent_news")

    have_decision_maker = "principal_name" in settled
    # T21.5: a firm inbox plus a phone is still a usable outreach channel — gating wave 2
    # off unless a *personal* email is present would re-break T19's cascade (verified live:
    # that gate is what kept 13 depth fields blank).
    have_channel = (
        "principal_email" in settled or "principal_phone" in settled or "firm_email" in settled
    )
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
        fetched = await fetch_page_free_first(f"https://{domain}")
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
    "recent_news": lambda name: f'"{name}"',
}


async def _find_news_signals(
    canonical_name: str, aliases: list[str] | None = None
) -> list[Claim]:
    """recent_news — tier 2 GDELT, tier 3 Serper /news, same dated-evidence pattern as
    wave 1's dated-signal helper, one result (the single highest-kill-power hit). A
    news article is evidence a story exists, not a statement the firm made an
    investment/hire/commitment, so T20.2 collapsed this helper to `recent_news` only —
    the `recent_fund_commitments` and `recent_key_hires` paths that used to write a
    field chosen by the query string (never by the article content) are deleted. T20.1
    skips any result that doesn't actually mention the entity in its
    title/snippet/description."""
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
            if not _mentions_entity(_result_mention_text(r), canonical_name, aliases):
                continue
            claims.append(Claim(
                field_name=field, answer=r.get("title") or r["url"], status="confirmed",
                source_url=r["url"], source_class=source_class, extraction_method=extraction_method,
                confidence="low", produced_by="enrichment", wave="2",
                retrieved_at=_parse_dt(r.get("seendate")) if source_class == "gdelt" else None,
            ))
    return claims


async def wave_2(
    claims: list[Claim], canonical_name: str, model: Any, domain: str | None = None,
    aliases: list[str] | None = None,
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

    new_claims.extend(
        c for c in await _find_news_signals(canonical_name, aliases) if c.field_name not in settled
    )

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

    if force:
        # wave_minus_1 is a pure function of entity_sources: same input, same output. On a
        # plain re-run entity_sources hasn't changed, so it just regenerates identical
        # claims and V4's compatibility check treats them as confirmations, not conflicts.
        # But if entity_sources HAS changed since the prior run (a backfilled/corrected
        # value — exactly what happened after the 2026-08-12 13F-quarter fix), the fresh
        # derivation now genuinely disagrees with the OLD derived claim still sitting in
        # `existing`, and V4 correctly-but-uselessly reports the pipeline's own prior
        # output as contradicting itself. Mark this entity's existing derived claims
        # superseded before re-deriving, so the stale value is never compared against the
        # fresh one as if both were live facts. The value and its history are kept (not
        # blanked, not deleted) — only its authority is retracted.
        existing = [
            c.model_copy(update={"status": "superseded"}) if c.produced_by == "derived" else c
            for c in existing
        ]

    started_at = utcnow()

    minus1 = wave_minus_1(sources, canonical_name, entity["aliases"], entity_id=entity_id,
                          layer1_claims=existing)
    claims = existing + minus1
    waves_completed = ["-1"]

    claims, wave0_findings, fatal0 = await wave_0(
        claims, canonical_name, injected_facts, model=get_model(SETTINGS.models.validation_tier)
    )
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
        # Resolve a domain if ingestion didn't supply one (no `domain_check` source — which
        # is every entity in the current pool). Deliberately AFTER the wave-0 gate: an
        # entity wave 0 rejects must not cost a Serper call. See PLAN.md T16.
        # PLAN.md T19.4: first take the domain straight from the discovery record when the
        # adv_name row carries a firm website — costs no Serper call (do NOT increment
        # calls_spent). Falls through to resolve_domain when the field is None or an
        # aggregator (e.g. a LinkedIn company page).
        if not domain:
            domain = _domain_from_sources(sources)
        if not domain:
            domain = await resolve_domain(canonical_name)
            calls_spent += 1

        new1, gated = await wave_1(claims, canonical_name, domain=domain, aliases=entity["aliases"])
        claims = claims + new1
        calls_spent += len(new1)
        waves_completed.append("1")

        if not gated:
            new2, cost2 = await wave_2(claims, canonical_name, model, domain=domain, aliases=entity["aliases"])
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
    entity_ids: list[str], model: Any, db_path: str, concurrency: int, *, force: bool = False,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Runs process_entity() for each entity_id with bounded concurrency (same pattern
    as app.runner.run_batch's batch dispatch) — each call opens its own connection via
    `db_path`. Sequential processing here was a real throughput bottleneck at scale:
    each entity can mean several free-fetches + several LLM calls, so 10+ entities
    processed one at a time serially could take longer than all of Layer 1 combined.

    `force=True` propagates to process_entity's `force`, re-running entities that already
    have an enrichment_runs row (needed when gate logic changed and prior outcomes must be
    recomputed, not replayed from cache).

    `run_id` (T35.2) binds a tool_log_context around each entity's processing so every
    external tool call enrichment makes is attributed to the right entity + run. Bound
    here — the narrowest place that covers all of waves 0-2 + Layer V without changing
    process_entity's signature (which tests call directly). The wave is left None: an
    unattributed wave is a small loss, a restructure of process_entity is a big risk."""
    semaphore = asyncio.Semaphore(concurrency)

    async def _bounded(entity_id: str) -> dict[str, Any]:
        async with semaphore:
            with connection(db_path) as isolated_conn:
                # Bind for the whole per-entity body (waves 0-2 + Layer V). Outside a
                # bound context (run_id is None — direct process_entity calls in tests)
                # tool_log_context still works but the recorder is a no-op, so no test
                # that calls these tools unmodified is affected.
                with tool_log_context(entity_id, run_id=run_id, db_path=db_path):
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

    # T35.1: open an `enrichment` run for this pipeline execution so every tool call
    # made while processing the pursue queue + reserve draws can be attributed to it
    # (run_id is threaded into _process_entities_concurrently -> tool_log_context). The
    # run row is written on the caller's `conn` and committed with the wave -1 writes
    # below, so it's visible to the isolated connections the concurrent workers open.
    run_id = start_run(conn, "enrichment", entity_count=len(decisions))
    try:
        minus1_by_entity: dict[str, list[Claim]] = {}
        for d in decisions:
            entity_id = d["entity_id"]
            entity = get_entity(conn, entity_id)
            sources = get_entity_sources(conn, entity_id)
            claims = wave_minus_1(sources, entity["canonical_name"], entity["aliases"], entity_id=entity_id,
                                  layer1_claims=[_claim_from_row(r) for r in get_claims(conn, entity_id)])
            minus1_by_entity[entity_id] = claims
            upsert_claims(conn, entity_id, [c.model_dump(mode="json") for c in claims])

        # Commit now, before any concurrent processing starts: `conn` is the CALLER's
        # connection (only committed when their own `with connection()` block eventually
        # exits, i.e. after this whole function returns) — if its wave -1 writes above stay
        # uncommitted while _process_entities_concurrently opens NEW connections to the
        # same file, SQLite's single-writer lock deadlocks (the new connections block
        # waiting for `conn` to release the write lock, which never happens until this
        # function returns, which never happens until they succeed). Found live 2026-07-29
        # adding concurrent processing — see PROJECT_LOG.md. The run row opened above is
        # committed here too, so the concurrent workers' tool_calls (which reference it by
        # run_id) point at a row the caller can already see.
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
        for result in await _process_entities_concurrently(pursue_queue, model, db_path, concurrency, force=force, run_id=run_id):
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
            for result in await _process_entities_concurrently(draw_ids, model, db_path, concurrency, force=force, run_id=run_id):
                processed.append(result)
                usd_spent += result["usd_spent"]
                reserve_spent += result["usd_spent"]
                reserve_draws += 1

        notes = {
            "processed": len(processed),
            "survivors": _survivor_count(),
            "reserve_draws": reserve_draws,
            "usd_spent": usd_spent,
        }
        finish_run(conn, run_id, status="done", notes=notes)
        # T35.5: persist + emit the field provenance log for every entity this run
        # processed, so the log appears on EVERY run, not only when a workbook is
        # emitted. Lazy import avoids a cycle (app.provenance_log imports app.dataset).
        # An emission failure must never fail the run — the leads are the deliverable.
        try:
            from app.provenance_log import build_run_log
            from app.db import write_field_provenance
            entity_outcomes = [(p["entity_id"], p.get("outcome")) for p in processed]
            doc = build_run_log(conn, run_id, entity_outcomes)
            rows = []
            for lead in doc["leads"]:
                for rec in lead["fields"]:
                    rows.append({
                        "run_id": run_id,
                        "entity_id": lead["entity_id"],
                        "field": rec["field"],
                        "value": rec["value"],
                        "status": rec["status"],
                        "shipped": rec["shipped"],
                        "source_class": (rec["how"] or {}).get("source_class"),
                        "extraction_method": (rec["how"] or {}).get("extraction_method"),
                        "record": _json.dumps(rec, ensure_ascii=False, default=str),
                    })
            write_field_provenance(conn, rows)
            out_dir = Path(db_path).parent / "runs" / run_id
            _atomic_write_json(out_dir / "field_provenance.json", doc)
        except Exception:  # noqa: BLE001 — never fail the run over the log
            logger.error("field provenance emission failed for run_id=%s", run_id, exc_info=True)
        return {
            "processed": processed,
            "survivors": _survivor_count(),
            "reserve_draws": reserve_draws,
            "usd_spent": usd_spent,
            "run_id": run_id,
        }
    except Exception:
        # Any unhandled exception must still close the run as failed, never leave it
        # running. The wave -1 writes already committed stay; the run row records that
        # this execution did not complete.
        finish_run(conn, run_id, status="failed")
        raise

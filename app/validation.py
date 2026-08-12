"""Layer V — Validation (enrichment_validation_dataset_plan.md §5). Validation
ANNOTATES claims — writes verification_method/confirming_url/confirming_class/
verified_at and flips `status` on EXISTING claims. It never creates a new value claim;
a validator that produces values is a second enrichment layer with no oversight (the
plan's own words).

No standalone "validation plan" with a V1-V6 spec exists anywhere in this repository —
enrichment_validation_dataset_plan.md references one ("unchanged in substance from the
validation plan") but it was never written down. The checks below were designed from
what that document says each one must catch: V1 is quoted almost verbatim
("source-supports-claim... roughly 150 calls... runs last... ring-fence ~200 Serper
credits"); V4/V5/V6 and the cross-class rule are inferred from their one-line
descriptions (contradictions / staleness+domain+ADV+firm-is-FO / completeness) since
that's all the plan states about them. See PROJECT_LOG.md's deviation note.

This module holds the deterministic checks (V4/V5/V6, cross-class, release rule) reused
by BOTH wave 0 (enrichment's cheap pre-gate, on pre-enrichment data) and the full Layer V
pass (post-enrichment, plus V1). Same functions, different input — not duplication (plan
§4 wave 0's own framing).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import dns.resolver
import phonenumbers
from langchain_core.messages import HumanMessage, SystemMessage

from app.state import Claim, DatasetInput, FieldStatus, Finding, ValidationInput, utcnow
from app.tools.freefetch import fetch_page_free_first

# Search-index results are never themselves an authority — a hit must be fetched (which
# turns it into site_scrape/web_page) before it can confirm anything else. "note
# serper_organic is a search index, not an authority: a Serper result confirms nothing
# on its own — the page it points to does, once fetched" (plan §5).
NON_CONFIRMING_CLASSES: frozenset[str] = frozenset({"serper_organic", "gdelt"})

# Every class either layer can produce a claim from — CONFIRMING_CLASSES must have an
# entry for the enrichment sources too (plan §5), including layer 1's own classes so the
# map is complete rather than implicitly defaulting unknown-but-real classes to unsafe.
CONFIRMING_CLASSES: dict[str, bool] = {
    # Layer 1 / injected-fact classes
    "adv_index": True, "13f_filing": True, "5500_filing": True, "conference_sighting": True,
    "domain_check": True, "edgar_filing": True, "fec_employer": True, "ppp_loans": True,
    "web_page": True, "news_article": True, "linkedin_profile": True,
    # Enrichment classes (plan §5's explicit list)
    "site_scrape": True, "derived_13f": True, "derived_5500": True,
    # Snov.io replaced Hunter.io as the email/profile provider. Kept CONFIRMING for the
    # same reason "hunter" was: it is a data vendor asserting a fact about a specific
    # person at a specific domain, not a search index that merely points at a page. The
    # "hunter" key stays mapped so historical claims already in the ledger (produced before
    # the swap) keep their corroboration weight rather than silently becoming
    # non-confirming on the next validation re-run.
    "snov": True, "hunter": True,
    "serper_organic": False,  # search index only — see NON_CONFIRMING_CLASSES
    "gdelt": False,  # article index — the article itself (news_article) confirms, not the index hit
}

_STALENESS_DAYS = 180


def is_confirming_class(source_class: str | None) -> bool:
    """Fail-closed: an unregistered class is treated as non-confirming, same policy as
    this codebase's on_unknown=reject default elsewhere (app/questions.py)."""
    if not source_class:
        return False
    return CONFIRMING_CLASSES.get(source_class, False)


def _normalize_answer(answer: object) -> str:
    return str(answer).strip().lower()


# Leading-token / phrase markers of a NEGATIVE or hedged answer to G1.Q3 ("does affirmative
# family-office evidence exist?"). Affirmative answers pass; these are the ones that mean
# "no FO evidence" even when the claim's status is settled (single_source). See
# check_v5_firm_is_fo_hardening — this is what keeps settled-but-negative G1.Q3 entities
# (Achmea, ASR — institutional asset managers) out of the dataset.
_FO_NEGATIVE_PREFIXES = ("no", "there is no", "there's no", "insufficient", "not enough", "not a", "none")
_FO_NEGATIVE_PHRASES = ("no affirmative evidence", "not a family office", "no evidence that")


def _is_negative_fo_answer(answer: object) -> bool:
    ans = _normalize_answer(answer).strip('"').lstrip()
    return ans.startswith(_FO_NEGATIVE_PREFIXES) or any(p in ans for p in _FO_NEGATIVE_PHRASES)


def _answers_compatible(a: object, b: object) -> bool:
    # Numeric answers (AUM, headcount, ...) must match exactly — substring containment
    # is meaningless for numbers ("50000000" is a substring of "150000000" but they are
    # not the same figure) and a false "compatible" here would wrongly verify or fail to
    # contradict two genuinely different values.
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return a == b
    na, nb = _normalize_answer(a), _normalize_answer(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def _group_by_field(claims: list[Claim]) -> dict[str, list[Claim]]:
    groups: dict[str, list[Claim]] = {}
    for c in claims:
        key = c.field_name or c.question_id
        if key is None:
            continue
        groups.setdefault(key, []).append(c)
    return groups


def apply_cross_class_rule(claims: list[Claim], now: datetime | None = None) -> list[Claim]:
    """For every field/question group, a "confirmed" claim with a compatible-answer
    corroborator from a DIFFERENT, confirming source_class is promoted to "verified"
    (with verification_method/confirming_url/confirming_class/verified_at stamped from
    the corroborator). Otherwise it's downgraded to "single_source" — confirmed, but
    never independently corroborated. Applies across the WHOLE ledger, including
    enrichment-produced claims (plan §5)."""
    now = now or utcnow()
    out = [c.model_copy(deep=True) for c in claims]
    groups = _group_by_field(out)

    for group in groups.values():
        for claim in group:
            if claim.status != "confirmed":
                continue
            confirmer = next(
                (
                    other for other in group
                    if other is not claim
                    and other.status in ("confirmed", "verified", "single_source")
                    and other.source_class != claim.source_class
                    and is_confirming_class(other.source_class)
                    and _answers_compatible(claim.answer, other.answer)
                ),
                None,
            )
            if confirmer is not None:
                claim.status = "verified"
                claim.verification_method = "cross_class"
                claim.confirming_url = confirmer.source_url
                claim.confirming_class = confirmer.source_class
                claim.verified_at = now
            else:
                claim.status = "single_source"
    return out


# Fields that legitimately hold MORE THAN ONE value, so two different answers are two facts
# rather than a conflict. V4's pairwise incompatibility check is skipped for these.
#
# `principal_name` is the motivating case: a family office routinely has two co-managing
# members, or a founder plus a president, and V4's `_answers_compatible` (a substring
# heuristic built for scalars) reads two different people's names as a contradiction and
# flips BOTH to `contradicted` — which then blocks G2.Q1's HARD gate and drops the record.
# Real example from the ledger: "Co-Managing Members Mary C. McNutt and Michelle J. Blass".
#
# `principal_title` rides along because it is meaningless to keep multiple names while
# collapsing their titles to one. CAVEAT, stated rather than hidden: name<->title PAIRING is
# not modelled — the ledger holds a set of names and a set of titles, not (name, title)
# couples. Consumers must not assume the Nth title belongs to the Nth name.
_MULTI_VALUED_FIELDS: frozenset[str] = frozenset({"principal_name", "principal_title"})


def check_v4_contradictions(claims: list[Claim]) -> tuple[list[Claim], list[Finding]]:
    """V4 — same field, incompatible answers from >=2 settled claims -> both flip to
    "contradicted". Re-run post-enrichment because enrichment can introduce a claim
    that contradicts what layer 1 already settled (e.g. a derived AUM figure that
    disagrees with a research claim).

    Scoped to `field_name`-keyed claims only (derived/enrichment, typically a scalar or
    a canonical label) — deliberately EXCLUDES pure `question_id`-keyed prose claims.
    Found live (2026-07-28, see PROJECT_LOG.md): two claims answering the same
    open-ended question in different wording — "13F value $174,032,930 (2026Q3)" vs
    "Yes, 6th Street Advisors files Form 13F showing holdings, indicating deployment of
    capital" — are NOT actually contradictory, they're two independent confirmations of
    the same fact, but `_answers_compatible`'s substring heuristic (the right tool for
    a scalar/label field) has no way to recognize that for free text. Layer 1's own
    researcher already does real contradiction detection on prose during compression
    (plan §4.5: "Contradictions between sources -> status=contradicted... the Verdict
    node decides what a contradiction means") — re-running a crude text check over its
    output here would only introduce false positives layer 1 never had, not catch
    anything new."""
    out = [c.model_copy(deep=True) for c in claims]
    findings: list[Finding] = []
    groups = {
        field_name: group
        for field_name, group in _group_by_field(out).items()
        if any(c.field_name == field_name for c in group)  # exclude question_id-only groups
        and field_name not in _MULTI_VALUED_FIELDS
    }

    for key, group in groups.items():
        # "superseded" excluded alongside could_not_verify: a claim wave -1 has already
        # regenerated (or retracted) this run must never be compared against its own
        # replacement as if both were live facts — see process_entity's force=True
        # superseding step (app/enrichment.py).
        settled = [c for c in group if c.status not in ("could_not_verify", "superseded")]
        for i, a in enumerate(settled):
            for b in settled[i + 1:]:
                if a.status == "contradicted" and b.status == "contradicted":
                    continue
                if not _answers_compatible(a.answer, b.answer):
                    a.status = "contradicted"
                    b.status = "contradicted"
                    findings.append(Finding(
                        check_id="V4_contradiction", severity="fatal", field=key,
                        detail=f"{a.source_class}:{a.answer!r} vs {b.source_class}:{b.answer!r}",
                        claim_id=a.claim_id,
                    ))
    return out, findings


def check_v5_staleness(claims: list[Claim], now: datetime | None = None) -> list[Finding]:
    """V5a — a settled claim older than the staleness threshold gets a warn finding.
    Doesn't itself change status (an old-but-uncontradicted fact isn't wrong, just worth
    flagging) — V6/the release rule are what act on it if the field is high-value and
    nothing fresher exists."""
    now = now or utcnow()
    findings: list[Finding] = []
    for c in claims:
        if c.retrieved_at is None or c.status not in ("confirmed", "verified", "single_source"):
            continue
        retrieved_at = c.retrieved_at if c.retrieved_at.tzinfo else c.retrieved_at.replace(tzinfo=timezone.utc)
        age_days = (now - retrieved_at).days
        if age_days > _STALENESS_DAYS:
            findings.append(Finding(
                check_id="V5_staleness", severity="warn", field=c.field_name or c.question_id,
                detail=f"claim is {age_days}d old (> {_STALENESS_DAYS}d threshold)",
                claim_id=c.claim_id, evidence_url=c.source_url,
            ))
    return findings


async def check_domain_mx_exists(domain: str) -> bool | None:
    """Real DNS MX lookup — free, keyless, one query per entity. Returns True/False, or
    None if the lookup itself failed (DNS server unreachable, timeout) — None must be
    treated as could_not_verify, never coerced to False (a transient resolver failure
    is not proof the domain can't receive mail)."""

    def _resolve() -> bool | None:
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=5.0)
            return len(answers) > 0
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return False
        except Exception:
            return None

    return await asyncio.to_thread(_resolve)


def check_phone_format_valid(phone: str, region: str = "US") -> bool:
    """Format-valid only — NOT line-verified (research_layer_plan.md §4.7's Layer-2
    note: "format-valid is not the same claim as line-verified, and labelling it as
    though it were is the kind of thing the audit is looking for"). A phone claim
    confirmed only by this check must carry status="format_only", never "verified" —
    callers are responsible for that distinction, this function only answers the format
    question."""
    try:
        parsed = phonenumbers.parse(phone, region)
        return phonenumbers.is_valid_number(parsed)
    except phonenumbers.NumberParseException:
        return False


def check_v5_firm_is_fo_hardening(claims: list[Claim]) -> list[Finding]:
    """V5b — re-assert G1.Q3 (affirmative FO evidence) and G1.Q5 (RIA-in-costume) are
    still settled post-enrichment. If enrichment introduced evidence that contradicts
    either, V4 will already have flagged it as "contradicted" — this specifically
    catches the case where G1.Q3 was never resolved by layer 1 (could_not_verify) and
    still isn't after enrichment, which V6 completeness should also catch but this makes
    the reason explicit and attributable to identity, not just "incomplete"."""
    by_q = {c.question_id: c for c in claims if c.question_id}
    findings: list[Finding] = []
    g1q3 = by_q.get("G1.Q3")
    # G1.Q3 must be BOTH settled (not could_not_verify/contradicted) AND affirmative — a
    # "single_source" claim whose answer is "No, there is no affirmative evidence that the
    # entity operates as a family office" is a settled NEGATIVE, and must still fatal here.
    # Checking status alone let genuine non-FOs through: Achmea Investment Management and
    # ASR Vermogensbeheer (both Dutch institutional asset managers) shipped 2026-07-29 on a
    # settled-but-negative G1.Q3, exactly the category this gate exists to exclude. We test
    # for a NEGATIVE answer rather than requiring "yes", because affirmative answers phrase
    # themselves several ways ("Yes, there is affirmative evidence…", "Affirmative evidence
    # shows the firm offers family-office services…") and must all pass — only clear
    # negatives / hedges ("No…", "There is no affirmative evidence…", "Insufficient
    # evidence…") get killed.
    g1q3_negative = g1q3 is not None and _is_negative_fo_answer(g1q3.answer)
    if g1q3 is None or g1q3.status in ("could_not_verify", "contradicted") or g1q3_negative:
        findings.append(Finding(
            check_id="V5_firm_is_fo", severity="fatal", field="G1.Q3",
            detail="no affirmative family-office evidence (unsettled or a settled-negative G1.Q3)",
            claim_id=g1q3.claim_id if g1q3 else None,
        ))
    g1q5 = by_q.get("G1.Q5")
    if g1q5 is not None and g1q5.status == "confirmed" and _normalize_answer(g1q5.answer).startswith("yes"):
        # G1.Q5's real answers are full sentences, not literal "yes"/"no" tokens — e.g.
        # "No, the entity is not merely a plain RIA in costume; it presents itself as a
        # family office." An exact-match check against the literal string "no" (the
        # original implementation) NEVER matches a real sentence, so every negative
        # answer was wrongly treated as an affirmative RIA-in-costume confirmation —
        # a critical false-positive found live 2026-07-29 (see PROJECT_LOG.md): it
        # silently killed "ACIMA PRIVATE WEALTH, LLC" at wave 0, a lead with G1.Q3
        # confirmed (genuine FO evidence), G1.Q4 confirmed MFO, and G1.Q5 explicitly
        # negative — exactly the kind of real survivor this pipeline exists to find.
        # Every G1 answer observed in practice opens with "Yes," or "No," (matching
        # every other G1 question's phrasing), so checking the leading token is a much
        # more reliable signal than exact equality.
        findings.append(Finding(
            check_id="V5_firm_is_fo", severity="fatal", field="G1.Q5",
            detail="entity confirmed as RIA-in-costume, not a genuine family office",
            claim_id=g1q5.claim_id,
        ))
    return findings


# Fields plan §4's wave 1 gate treats as the actionability core — V6 completeness checks
# these three categories are each covered by at least one settled (non could_not_verify,
# non removed_failed_validation) claim.
_DECISION_MAKER_FIELDS = ("principal_name", "G2.Q1")
_CONTACT_CHANNEL_FIELDS = ("principal_email", "principal_phone", "G2.Q2")
_DATED_SIGNAL_FIELDS = ("recent_investments", "why_now_trigger", "G3.Q1", "G3.Q2")


def _any_settled(claims: list[Claim], field_names: tuple[str, ...]) -> bool:
    keys = set(field_names)
    return any(
        (c.field_name in keys or c.question_id in keys)
        and c.status not in ("could_not_verify", "removed_failed_validation", "contradicted", "superseded")
        for c in claims
    )


def check_v6_completeness(claims: list[Claim]) -> list[Finding]:
    """V6 — completeness is now advisory only: decision-maker, contact channel, AND dated
    signal are ALL warnings, none fatal (third relaxation, 2026-07-29 — see PROJECT_LOG.md).
    Completeness no longer rejects anything on its own; an entity that clears the
    firm-is-a-genuine-family-office identity gate (V5_firm_is_fo / G1.Q3+G1.Q5) ships even
    with no confirmed principal, no contact, and no dated why-now trigger — those fields are
    left honestly blank (never fabricated) and surfaced as caveats. Rationale: the user
    explicitly wants the pursue_low tier of genuine family offices in the dataset even when
    they're only firm-level leads without a fresh trigger yet; a known family office with
    blanks is real data outreach can still use, whereas the earlier dated-signal-is-fatal
    rule was throwing those leads away entirely.

    The one gate this deliberately does NOT relax is V5's firm-is-FO hardening (the
    G1.Q3 "is this actually a family office" + G1.Q5 "plain RIA-in-costume" identity check).
    That decides whether the entity BELONGS in a family-office dataset at all — a different
    question from how complete its record is — and lowering it would ship confirmed banks /
    plain RIAs as if they were family-office leads, which is the same integrity line as
    fabricating rows. Group-1 non-FOs (Amarillo National Bank, AEGON, Argent Capital, …)
    stay rejected at wave 0 on that gate regardless of anything here."""
    findings: list[Finding] = []
    if not _any_settled(claims, _DECISION_MAKER_FIELDS):
        findings.append(Finding(check_id="V6_completeness", severity="warn", field="principal_name",
                                 detail="no named decision-maker settled — shipped with this field blank"))
    if not _any_settled(claims, _CONTACT_CHANNEL_FIELDS):
        findings.append(Finding(check_id="V6_completeness", severity="warn", field="principal_email",
                                 detail="no working contact channel settled — shipped with this field blank"))
    if not _any_settled(claims, _DATED_SIGNAL_FIELDS):
        findings.append(Finding(check_id="V6_completeness", severity="warn", field="recent_investments",
                                 detail="no dated why-now signal settled — shipped without a fresh trigger"))
    return findings


# High-value fields the release rule can blank on verification failure (plan §5's
# "Release rule enforcement" + §6.3's "never silently empty" cell rule). Contact fields
# only — the release rule is about not shipping an undeliverable contact, not about
# suppressing every unverifiable soft field.
# DELIBERATELY EMPTY as of 2026-08-12 — the release rule no longer blanks anything.
#
# It used to blank the four contact fields when Layer V couldn't verify them. In practice
# that meant blanking almost every contact the pipeline found, for a structural reason:
# a firm's public website essentially never publishes a decision-maker's direct email or
# phone. So V1 would fetch `https://<domain>`, ask "does this page contain
# alan@accredited.com?", correctly answer no, and the release rule would destroy a perfectly
# good Snov.io-sourced contact.
#
# The deeper flaw is provenance, not strictness: `app.enrichment._find_email_via_snov` sets
# `source_url` to the firm's homepage even though the email came from Snov.io's API, so V1
# was checking a page that was never the claim's source. Same category error as the
# `aum_basis` V1 exemption.
#
# Contacts now SHIP with their status and source_class attached (Layer D carries
# `principal_email_source_class`), to be validated downstream by a real deliverability
# check rather than by asking whether a marketing page happens to mention them.
# The mechanism below is kept intact — re-populate this set to re-arm it for any field.
_RELEASABLE_FIELDS: frozenset[str] = frozenset()


def apply_release_rule(
    claims: list[Claim], failed_field_names: set[str], reason_code: str = "verification_failed"
) -> tuple[list[Claim], list[dict[str, object]]]:
    """Any claim in `failed_field_names` (fields Layer V determined don't hold up — e.g.
    an email that bounced an MX check) gets status="removed_failed_validation" and its
    value blanked. Returns (updated_claims, audit_entries) — the caller
    (run_validation) writes audit_entries to `audit_rejected_values` and Layer D must
    never see a killed value in a shippable field (plan §5/§6.3)."""
    out: list[Claim] = []
    audit_entries: list[dict[str, object]] = []
    for c in claims:
        key = c.field_name or c.question_id
        if key in failed_field_names and (c.field_name in _RELEASABLE_FIELDS or key in _RELEASABLE_FIELDS):
            audit_entries.append({
                "field_name": key,
                "rejected_value": c.answer,
                "reason_code": reason_code,
                "evidence_url": c.source_url,
            })
            c = c.model_copy(update={"status": "removed_failed_validation", "answer": None})
        out.append(c)
    return out, audit_entries


# --- V1: source-supports-claim (plan §5, the check quoted almost verbatim) ---
# "roughly 150 calls across the file... runs last in the pipeline... ring-fence ~200
# Serper credits for it... the check that catches claims whose source URL doesn't
# support them... is the single most valuable check in the system." Judged by a cheap
# LLM pass over already-fetched page content — fetching is the caller's job
# (run_validation, free-path-first via app.tools.freefetch) so this function stays pure
# and testable given content, matching every other LLM-pass in this codebase
# (app.verdict._llm_soft_gate_pass).

# Fields whose "answer" is an internal schema label WE invented, never text any real
# source page would literally contain — checking whether a page "supports" the string
# "13f_floor" is a category error, not a genuine verification failure. Found live
# (2026-07-28, see PROJECT_LOG.md): a strict V1 judge correctly noted the SEC filing
# page never says "13f_floor" and flagged it fatal, which is technically true and
# completely useless — the claim `aum_basis="13f_floor"` describes HOW aum_usd was
# computed, not a fact extractable from prose. `aum_usd`/`aum_as_of` stay in scope —
# a dollar figure and a filing quarter genuinely are facts a filing page states.
_V1_EXEMPT_FIELDS: frozenset[str] = frozenset({
    "aum_basis",
    # Contact fields: same category error, different shape. These come from Snov.io's
    # database (or a site-wide scrape), but the claim's source_url is the firm's homepage —
    # a page that essentially never publishes a decision-maker's direct email or phone.
    # Asking V1 "does this page support alan@accredited.com?" is therefore guaranteed to
    # answer no regardless of whether the address is real, so the check carries no
    # information and only produced false contradictions. Deliverability is the right test
    # for these and it isn't a page-content question (2026-08-12).
    "principal_email", "principal_phone",
    "secondary_contact_email", "secondary_contact_phone",
})

# Source classes V1 must not spend a call on. A search-index hit is already incapable of
# confirming anything (NON_CONFIRMING_CLASSES), and its URL is typically a LinkedIn profile
# that returns a login wall to any fetcher — so V1 reliably "fails" it for reasons that say
# nothing about the claim. 42 of V1's 144 fatal findings were exactly this: an unreadable
# page scored as a contradiction (2026-08-12).
_V1_SKIP_SOURCE_CLASSES: frozenset[str] = frozenset({"serper_organic"})

_V1_SYSTEM_PROMPT = (
    "You are checking whether a web page's content supports a specific factual claim "
    "about an organization. Judge the SUBSTANCE of the claim, not exact wording: if the "
    "page states the same fact using different phrasing, units, or labels, that counts "
    "as support. Only mark a claim unsupported if the page is actually silent on it or "
    "states something different. Respond with ONLY a JSON object: "
    '{"supported": true|false, "reason": "<one sentence>"}. No markdown fences, no extra keys.'
)


def _parse_v1_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    data = json.loads(text)
    if not isinstance(data.get("supported"), bool):
        raise ValueError(f"invalid 'supported' value: {data.get('supported')!r}")
    if not isinstance(data.get("reason"), str):
        raise ValueError("'reason' must be a string")
    return data


async def check_v1_source_supports_claim(claim: Claim, page_content: str, model: Any) -> tuple[Finding, float]:
    """Returns (Finding, cost_usd). `page_content` is whatever was fetched from
    claim.source_url (empty string if the fetch itself failed — treated as fatal, since
    an unreachable/dead source can't support anything). A judge-parse failure degrades to
    a "warn" finding (unresolved, not failed) rather than fabricating a verdict — same
    fail-safe posture as app.verdict._llm_soft_gate_pass's parse-failure default."""
    field = claim.field_name or claim.question_id
    if not page_content.strip():
        # WARN, not FATAL: an unreadable source leaves the claim UNPROVEN, not disproven.
        # "I couldn't read the page" is not evidence the claim is false — most of these are
        # LinkedIn login walls and Cloudflare challenges, which say nothing either way.
        # Scoring them fatal flipped the claim to "contradicted" and accounted for 42 of
        # V1's 144 fatal findings (2026-08-12). Same fail-safe posture as the judge-parse
        # failure below, and the same distinction app/researcher.py draws between
        # `tool_unavailable` and `no_evidence_found`.
        return Finding(
            check_id="V1_source_supports", severity="warn", field=field,
            detail="source page fetch returned no extractable content — claim unproven, not disproven",
            claim_id=claim.claim_id, evidence_url=claim.source_url,
        ), 0.0

    human = HumanMessage(
        content=f"Claim: {field} = {claim.answer!r}\n\nPage content (truncated):\n{page_content[:4000]}"
    )
    response = await model.ainvoke([SystemMessage(content=_V1_SYSTEM_PROMPT), human])
    cost = response.response_metadata.get("cost_usd", 0.0)
    try:
        data = _parse_v1_json(str(response.content))
    except (json.JSONDecodeError, ValueError):
        return Finding(
            check_id="V1_source_supports", severity="warn", field=field,
            detail="judge model returned unparseable output — left unresolved, not failed",
            claim_id=claim.claim_id, evidence_url=claim.source_url,
        ), cost

    severity = "info" if data["supported"] else "fatal"
    return Finding(
        check_id="V1_source_supports", severity=severity, field=field, detail=data["reason"],
        claim_id=claim.claim_id, evidence_url=claim.source_url,
    ), cost


# ============================================================================
# Full Layer V orchestration (plan §5's two integration adjustments: skip-if-unchanged,
# and release-rule enforcement being the seam to Layer D).
# ============================================================================


async def _default_v1_fetch(url: str) -> dict[str, Any]:
    """Free-fetch-first (plan §5's ring-fence: "Free-path fetches... don't draw from the
    ring-fence — prefer them here too"). `credits_spent` is 0 on the cheap
    httpx+trafilatura path, 1 on the escalation tier.

    The escalation tier is now a Crawl4AI browser render rather than a paid Serper scrape,
    so that 1 is no longer literally a purchased API credit — it is a unit of *wall time*
    (a Chromium render, seconds not milliseconds). `v1_credit_budget` is still the right
    bound to charge it against: V1 runs ~150x per validation pass, and letting every one of
    those escalate to a browser render unbounded would dominate the run's duration."""
    result = await fetch_page_free_first(url)
    credits_spent = 0 if result.get("extraction_method") == "httpx_trafilatura" else 1
    return {"content": result.get("content", ""), "credits_spent": credits_spent}


def _needs_rerun(validation_input: ValidationInput) -> bool:
    """Skip-if-unchanged (plan §5): if enrichment never advanced past wave 0 (no wave 1/2
    claims were added), V4/V5 would see the exact same claim set they already ran
    against at wave 0 — carry that result forward instead of paying for it twice."""
    return any(w in ("1", "2") for w in validation_input.waves_completed)


def _latest_by_field(claims: list[Claim]) -> dict[str, Claim]:
    latest: dict[str, Claim] = {}
    for c in claims:
        key = c.field_name or c.question_id
        if key is not None:
            latest[key] = c  # last write wins, same convention as app.verdict._claims_by_question
    return latest


def _build_field_statuses(claims: list[Claim], now: datetime) -> list[FieldStatus]:
    return [
        FieldStatus(
            field=key, status=c.status, method=c.verification_method,
            confirming_url=c.confirming_url, confirming_class=c.confirming_class, last_checked=now,
        )
        for key, c in _latest_by_field(claims).items()
    ]


def decide_type_final(claims) -> str:
    """SFO | MFO | type_unconfirmed from the G1.Q4 claim's final validated status.

    Accepts EITHER a list of pydantic Claim objects (as Layer V holds them) OR a list
    of plain dicts (raw sqlite rows, as Layer D's dataset.py holds them) — it reads
    only `question_id`, `status`, `answer`, and normalizes access with a tiny local
    helper rather than converting whole objects. The SFO/MFO rule is a correctness
    rule and must not be duplicated; this is the one implementation, imported by both
    layers. `'superseded'` is a disqualifying status (added to the codebase in 6666dab
    but missed here at the time) — a superseded G1.Q4 has been overwritten and must not
    decide the type.
    """
    def _get(c, key):
        return c.get(key) if isinstance(c, dict) else getattr(c, key, None)

    g1q4 = next((c for c in claims if _get(c, "question_id") == "G1.Q4"), None)
    if g1q4 is None or _get(g1q4, "status") in (
        "could_not_verify", "removed_failed_validation", "contradicted", "superseded",
    ):
        return "type_unconfirmed"
    answer = str(_get(g1q4, "answer")).upper()
    if "MFO" in answer:
        return "MFO"
    if "SFO" in answer:
        return "SFO"
    return "type_unconfirmed"


# Private-name alias so anything that imported `_decide_type_final` keeps working.
_decide_type_final = decide_type_final


async def run_validation(
    validation_input: ValidationInput,
    model: Any,
    *,
    now: datetime | None = None,
    v1_credit_budget: int = 200,
    fetch_fn=None,
) -> tuple[DatasetInput, list[dict[str, Any]], float]:
    """Full Layer V pass (plan §5). Deterministic checks first (cheap, always re-run
    since enrichment-appended claims change cross-class corroboration every time);
    V4/V5 skip-if-unchanged; V1 last, ring-fenced to `v1_credit_budget` paid-scrape
    credits (free-path fetches don't count against it). Returns (DatasetInput,
    audit_rejected_value entries for the caller to persist, total V1 LLM cost) — the
    audit entries and cost aren't part of the DatasetInput seam contract itself but the
    orchestrator (app.enrichment.run_pipeline) needs them to write `audit_rejected_values`
    and track spend."""
    now = now or utcnow()
    fetch_fn = fetch_fn or _default_v1_fetch
    claims = list(validation_input.claim_ledger)
    findings: list[Finding] = []

    # Cross-class rule always re-runs: enrichment appends claims wave 0 never saw, so
    # skipping it would miss every enrichment-produced corroboration.
    claims = apply_cross_class_rule(claims, now=now)

    if _needs_rerun(validation_input):
        claims, v4_findings = check_v4_contradictions(claims)
        findings.extend(v4_findings)
        # check_v4_contradictions deliberately skips re-flagging a pair that's already
        # both "contradicted" (avoids duplicate findings on re-run) — but since wave 0
        # relaxation (2026-07-29) means wave-0-fatal no longer stops the pipeline before
        # this recompute runs, that skip would otherwise silently drop the ONLY record of
        # a wave-0-found contradiction. Carry forward any wave-0 V4 finding this recompute
        # didn't re-emit.
        seen = {f.claim_id for f in v4_findings}
        findings.extend(
            f for f in validation_input.wave0_findings
            if f.check_id == "V4_contradiction" and f.claim_id not in seen
        )
        findings.extend(check_v5_staleness(claims, now=now))
        findings.extend(check_v5_firm_is_fo_hardening(claims))
    else:
        findings.extend(validation_input.wave0_findings)

    # V1 — before the release rule, not after: a claim V1 determines its source doesn't
    # actually support is exactly the kind of failure the release rule exists to catch
    # on a high-value field (an earlier version of this function ran the release rule
    # first and V1 never got a chance to feed it — fixed after writing the seam
    # integration tests exposed it, see PROJECT_LOG.md). Ring-fenced to
    # `v1_credit_budget` paid-scrape credits; free-path fetches don't count against it.
    v1_cost = 0.0
    credits_spent = 0
    for i, claim in enumerate(claims):
        if credits_spent >= v1_credit_budget:
            break
        if claim.status in ("contradicted", "removed_failed_validation", "could_not_verify", "superseded"):
            continue
        if not claim.source_url:
            continue
        if (claim.field_name or claim.question_id) in _V1_EXEMPT_FIELDS:
            continue
        if claim.source_class in _V1_SKIP_SOURCE_CLASSES:
            # A search-index hit can't confirm anything anyway, and its URL is usually a
            # login-walled LinkedIn profile — V1 would burn a fetch and an LLM call to
            # produce a meaningless "unsupported". See _V1_SKIP_SOURCE_CLASSES.
            continue
        fetched = await fetch_fn(claim.source_url)
        credits_spent += fetched.get("credits_spent", 1)
        finding, cost = await check_v1_source_supports_claim(claim, fetched.get("content", ""), model)
        v1_cost += cost
        findings.append(finding)
        if finding.severity == "fatal":
            claims[i] = claim.model_copy(update={"status": "contradicted"})

    # Release rule: any high-value field with a fatal finding against it (V4/V5
    # contradiction/hardening, OR a V1 source-doesn't-support-it verdict) gets killed
    # and blanked before Layer D ever sees it.
    failed_fields = {f.field for f in findings if f.severity == "fatal" and f.field}
    claims, audit_entries = apply_release_rule(claims, failed_field_names=failed_fields)

    # Completeness is judged LAST, on the final post-V1/release-rule claim set — a V1
    # kill on the sole contact channel must be able to cascade into a real reject, not
    # just an isolated finding nobody acts on.
    completeness_findings = check_v6_completeness(claims)
    findings.extend(completeness_findings)

    if any(f.severity == "fatal" for f in completeness_findings):
        outcome = "reject"
    elif any(f.severity == "fatal" for f in findings):
        outcome = "ship_with_caveats"
    elif any(f.severity == "warn" for f in findings):
        outcome = "ship_with_caveats"
    else:
        outcome = "ship"

    dataset_input = DatasetInput(
        entity_id=validation_input.entity_id,
        outcome=outcome,
        claim_ledger=claims,
        field_statuses=_build_field_statuses(claims, now),
        findings=findings,
        caveats=[f.detail for f in findings if f.severity in ("warn", "fatal") and outcome != "reject"],
        chain=[],
        type_final=decide_type_final(claims),
    )
    return dataset_input, audit_entries, v1_cost

"""Layer V core deterministic checks (enrichment_validation_dataset_plan.md §5).
No V1-V6 spec exists elsewhere in this repo — see app/validation.py's module docstring
for how these were designed. These tests pin the behavior actually implemented."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import dns.resolver
from langchain_core.messages import AIMessage

import app.validation as validation_module
from app.state import Claim, Finding, ValidationInput
from app.validation import (
    apply_cross_class_rule,
    apply_release_rule,
    check_domain_mx_exists,
    check_phone_format_valid,
    check_v1_source_supports_claim,
    check_v4_contradictions,
    check_v5_firm_is_fo_hardening,
    check_v5_staleness,
    check_v6_completeness,
    is_confirming_class,
    run_validation,
)


def _claim(**kw) -> Claim:
    defaults = dict(status="confirmed", confidence="medium")
    defaults.update(kw)
    return Claim(**defaults)


# --- cross-class rule ---

def test_cross_class_verifies_when_two_different_confirming_classes_agree():
    claims = [
        _claim(field_name="aum_usd", answer=150_000_000, source_class="13f_filing", source_url="http://sec"),
        _claim(field_name="aum_usd", answer=150_000_000, source_class="site_scrape", source_url="http://firm.com"),
    ]
    out = apply_cross_class_rule(claims)
    assert all(c.status == "verified" for c in out)
    assert out[0].confirming_class == "site_scrape"
    assert out[0].verification_method == "cross_class"


def test_cross_class_downgrades_to_single_source_with_no_corroborator():
    claims = [_claim(field_name="aum_usd", answer=150_000_000, source_class="13f_filing")]
    out = apply_cross_class_rule(claims)
    assert out[0].status == "single_source"


def test_serper_organic_never_confirms_anything():
    """"a Serper result confirms nothing on its own — the page it points to does, once
    fetched" — plan §5."""
    claims = [
        _claim(field_name="principal_name", answer="Jane Doe", source_class="research"),
        _claim(field_name="principal_name", answer="Jane Doe", source_class="serper_organic"),
    ]
    out = apply_cross_class_rule(claims)
    named = next(c for c in out if c.source_class == "research")
    assert named.status == "single_source"
    assert is_confirming_class("serper_organic") is False


def test_cross_class_ignores_same_source_class_pair():
    claims = [
        _claim(field_name="principal_name", answer="Jane Doe", source_class="web_page", source_url="http://a"),
        _claim(field_name="principal_name", answer="Jane Doe", source_class="web_page", source_url="http://b"),
    ]
    out = apply_cross_class_rule(claims)
    assert all(c.status == "single_source" for c in out)  # same class -> not cross-class corroboration


def test_cross_class_only_promotes_confirmed_status():
    claims = [
        _claim(field_name="f", answer="x", source_class="a", status="could_not_verify"),
        _claim(field_name="f", answer="x", source_class="b"),
    ]
    out = apply_cross_class_rule(claims)
    untouched = next(c for c in out if c.source_class == "a")
    assert untouched.status == "could_not_verify"


# --- V4 contradictions ---

def test_v4_flags_incompatible_answers_as_contradicted():
    claims = [
        _claim(field_name="aum_usd", answer=150_000_000, source_class="13f_filing"),
        _claim(field_name="aum_usd", answer=50_000_000, source_class="site_scrape"),
    ]
    out, findings = check_v4_contradictions(claims)
    assert all(c.status == "contradicted" for c in out)
    assert len(findings) == 1
    assert findings[0].check_id == "V4_contradiction"
    assert findings[0].severity == "fatal"


def test_v4_leaves_could_not_verify_alone():
    claims = [
        _claim(field_name="f", answer="x", status="could_not_verify"),
        _claim(field_name="f", answer="y", status="could_not_verify"),
    ]
    out, findings = check_v4_contradictions(claims)
    assert findings == []


def test_v4_no_contradiction_on_compatible_answers():
    claims = [
        _claim(field_name="principal_name", answer="Jane Doe"),
        _claim(field_name="principal_name", answer="jane doe"),
    ]
    _, findings = check_v4_contradictions(claims)
    assert findings == []


# --- V5 staleness ---

def test_v5_staleness_flags_old_settled_claims():
    old = datetime.now(timezone.utc) - timedelta(days=400)
    claims = [_claim(field_name="f", answer="x", retrieved_at=old, status="verified")]
    findings = check_v5_staleness(claims)
    assert len(findings) == 1
    assert findings[0].check_id == "V5_staleness"
    assert findings[0].severity == "warn"


def test_v5_staleness_ignores_recent_claims():
    recent = datetime.now(timezone.utc) - timedelta(days=5)
    claims = [_claim(field_name="f", answer="x", retrieved_at=recent, status="verified")]
    assert check_v5_staleness(claims) == []


# --- V5 firm-is-FO hardening ---

def test_v5_firm_is_fo_fatal_when_g1q3_unresolved():
    findings = check_v5_firm_is_fo_hardening([])
    assert any(f.field == "G1.Q3" and f.severity == "fatal" for f in findings)


def test_v5_firm_is_fo_clean_when_g1q3_confirmed_and_g1q5_no():
    claims = [
        _claim(question_id="G1.Q3", answer="yes, operates as a family office"),
        _claim(question_id="G1.Q5", answer="no"),
    ]
    findings = check_v5_firm_is_fo_hardening(claims)
    assert findings == []


def test_v5_firm_is_fo_fatal_when_ria_in_costume_confirmed():
    claims = [
        _claim(question_id="G1.Q3", answer="yes"),
        _claim(question_id="G1.Q5", answer="yes, plain RIA"),
    ]
    findings = check_v5_firm_is_fo_hardening(claims)
    assert any(f.field == "G1.Q5" for f in findings)


def test_v5_firm_is_fo_clean_when_g1q5_is_a_full_negative_sentence():
    """Regression for a critical false-positive found live 2026-07-29 (see
    PROJECT_LOG.md): real G1.Q5 answers are full sentences, not the literal token
    "no" — an exact-match check against "no" never matches a real sentence, so every
    negative answer was wrongly flagged as a confirmed RIA-in-costume. This killed
    "ACIMA PRIVATE WEALTH, LLC" (G1.Q3 confirmed genuine FO evidence, G1.Q4 confirmed
    MFO) at wave 0 before enrichment ever got a chance."""
    claims = [
        _claim(question_id="G1.Q3", answer="Yes, there is affirmative evidence that the entity operates as a family office."),
        _claim(question_id="G1.Q5", answer="No, the entity is not merely a plain RIA in costume; it presents itself as a family office."),
    ]
    findings = check_v5_firm_is_fo_hardening(claims)
    assert findings == []


def test_v5_firm_is_fo_fatal_when_g1q5_is_a_full_affirmative_sentence():
    claims = [
        _claim(question_id="G1.Q3", answer="Yes, there is affirmative evidence that the entity operates as a family office."),
        _claim(question_id="G1.Q5", answer="Yes, the entity appears to be a plain SEC-registered investment adviser (RIA) rather than a family office."),
    ]
    findings = check_v5_firm_is_fo_hardening(claims)
    assert any(f.field == "G1.Q5" for f in findings)


def test_v5_firm_is_fo_fatal_when_g1q3_is_settled_but_negative():
    """Regression for an integrity gap found live 2026-07-29 (see PROJECT_LOG.md): the gate
    checked only G1.Q3's STATUS (settled vs could_not_verify), not its answer polarity. A
    "single_source" (settled) claim whose answer is "No, there is no affirmative evidence
    that the entity operates as a family office" passed — shipping Achmea Investment
    Management and ASR Vermogensbeheer (institutional asset managers) into the dataset. A
    settled-negative G1.Q3 must still fatal."""
    claims = [
        _claim(question_id="G1.Q3", answer="No, there is no affirmative evidence that the entity operates as a family office.", status="single_source"),
        _claim(question_id="G1.Q5", answer="No, not a plain RIA in costume.", status="single_source"),
    ]
    findings = check_v5_firm_is_fo_hardening(claims)
    assert any(f.field == "G1.Q3" and f.severity == "fatal" for f in findings)


def test_v5_firm_is_fo_clean_when_g1q3_affirmative_without_leading_yes():
    """Affirmative G1.Q3 answers phrase themselves several ways — not only "Yes, …" but also
    "Affirmative evidence shows …" (1015 Capital Partners' real answer). Those must pass; the
    negative check must not require a literal leading "yes"."""
    claims = [
        _claim(question_id="G1.Q3", answer="Affirmative evidence shows the firm offers family office services.", status="single_source"),
        _claim(question_id="G1.Q5", answer="No, not a plain RIA in costume.", status="single_source"),
    ]
    findings = check_v5_firm_is_fo_hardening(claims)
    assert findings == []


# --- V6 completeness ---

def test_v6_completeness_all_warn_nothing_fatal():
    """Completeness is advisory only now — decision-maker, contact channel, AND dated
    signal are ALL warnings, none fatal (2026-07-29, third relaxation — see PROJECT_LOG.md:
    the user wants genuine pursue_low family offices in the dataset even when they're only
    firm-level leads with no fresh trigger yet; completeness no longer rejects on its own,
    only the V5 firm-is-FO identity gate does)."""
    findings = check_v6_completeness([])
    assert len(findings) == 3
    assert {f.field for f in findings} == {"principal_name", "principal_email", "recent_investments"}
    by_field = {f.field: f.severity for f in findings}
    assert by_field["principal_name"] == "warn"
    assert by_field["recent_investments"] == "warn"
    assert by_field["principal_email"] == "warn"
    assert all(f.severity == "warn" for f in findings)


def test_v6_completeness_clean_when_all_three_present():
    claims = [
        _claim(field_name="principal_name", answer="Jane Doe"),
        _claim(field_name="principal_email", answer="jane@acme.com"),
        _claim(field_name="why_now_trigger", answer="fresh_liquidity"),
    ]
    assert check_v6_completeness(claims) == []


def test_v6_completeness_ignores_removed_failed_validation_claims():
    claims = [
        _claim(field_name="principal_name", answer="Jane Doe"),
        _claim(field_name="principal_email", answer=None, status="removed_failed_validation"),
        _claim(field_name="why_now_trigger", answer="fresh_liquidity"),
    ]
    findings = check_v6_completeness(claims)
    assert any(f.field == "principal_email" for f in findings)


# --- release rule ---

def test_release_rule_blanks_value_and_writes_audit_entry():
    claims = [_claim(field_name="principal_email", answer="dead@nowhere.invalid")]
    out, audit = apply_release_rule(claims, failed_field_names={"principal_email"})
    assert out[0].status == "removed_failed_validation"
    assert out[0].answer is None
    assert len(audit) == 1
    assert audit[0]["field_name"] == "principal_email"
    assert audit[0]["rejected_value"] == "dead@nowhere.invalid"


def test_release_rule_only_touches_releasable_fields():
    claims = [_claim(field_name="investing_thesis", answer="growth equity")]
    out, audit = apply_release_rule(claims, failed_field_names={"investing_thesis"})
    assert out[0].status == "confirmed"  # not a releasable field -> untouched
    assert audit == []


# --- domain/MX (dns.resolver mocked — this suite stays fully offline, no live network
# call, matching every other tool test in this repo) + phone format (pure parsing) ---

async def test_domain_mx_exists_true_when_resolver_returns_records(monkeypatch):
    monkeypatch.setattr(dns.resolver, "resolve", lambda domain, rtype, lifetime=5.0: [object()])
    result = await check_domain_mx_exists("acmecap.com")
    assert result is True


async def test_domain_mx_false_on_nxdomain(monkeypatch):
    def _raise(domain, rtype, lifetime=5.0):
        raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(dns.resolver, "resolve", _raise)
    result = await check_domain_mx_exists("this-domain-should-not-exist-xyz123.invalid")
    assert result is False


async def test_domain_mx_none_on_resolver_failure(monkeypatch):
    def _raise(domain, rtype, lifetime=5.0):
        raise RuntimeError("resolver unreachable")

    monkeypatch.setattr(dns.resolver, "resolve", _raise)
    result = await check_domain_mx_exists("acmecap.com")
    assert result is None  # a resolver outage is NOT proof the domain lacks mail


def test_phone_format_valid_accepts_real_shape():
    assert check_phone_format_valid("+1 415 555 2671", region="US") is True


def test_phone_format_valid_rejects_garbage():
    assert check_phone_format_valid("not a phone number", region="US") is False


# --- V1 source-supports-claim ---

async def test_v1_empty_page_content_is_fatal_no_llm_call(fake_model):
    claim = _claim(field_name="principal_name", answer="Jane Doe", source_url="http://x")
    finding, cost = await check_v1_source_supports_claim(claim, "", fake_model)
    assert finding.severity == "fatal"
    assert cost == 0.0
    assert fake_model.calls == []


async def test_v1_supported_claim_yields_info_finding(fake_model):
    fake_model.queue(AIMessage(content='{"supported": true, "reason": "page states this directly"}'))
    claim = _claim(field_name="principal_name", answer="Jane Doe", source_url="http://x")
    finding, _ = await check_v1_source_supports_claim(claim, "Jane Doe is the Managing Principal.", fake_model)
    assert finding.severity == "info"
    assert finding.check_id == "V1_source_supports"


async def test_v1_unsupported_claim_yields_fatal_finding(fake_model):
    fake_model.queue(AIMessage(content='{"supported": false, "reason": "page never mentions this"}'))
    claim = _claim(field_name="principal_name", answer="Jane Doe", source_url="http://x")
    finding, _ = await check_v1_source_supports_claim(claim, "This page is about something else entirely.", fake_model)
    assert finding.severity == "fatal"


async def test_v1_unparseable_judge_output_is_warn_not_fatal(fake_model):
    fake_model.queue(AIMessage(content="not json at all"))
    claim = _claim(field_name="principal_name", answer="Jane Doe", source_url="http://x")
    finding, _ = await check_v1_source_supports_claim(claim, "some page content here", fake_model)
    assert finding.severity == "warn"


# --- run_validation orchestration ---

async def _supportive_fetch(url):
    return {"content": "this page supports the claim", "credits_spent": 0}


async def test_run_validation_ships_clean_when_everything_supported(fake_model):
    for _ in range(4):
        fake_model.queue(AIMessage(content='{"supported": true, "reason": "page confirms it"}'))
    claims = [
        _claim(field_name="principal_name", answer="Jane Doe", source_url="http://a"),
        _claim(field_name="principal_email", answer="jane@acme.com", source_url="http://b"),
        _claim(field_name="why_now_trigger", answer="fresh_liquidity", source_url="http://c"),
        _claim(question_id="G1.Q3", answer="yes, operates as a family office", source_url="http://d"),
    ]
    vi = ValidationInput(entity_id="e1", claim_ledger=claims, waves_completed=["-1", "0", "1"], wave0_findings=[])
    dataset_input, audit, cost = await run_validation(vi, fake_model, fetch_fn=_supportive_fetch)
    assert dataset_input.outcome == "ship"
    assert audit == []
    assert cost >= 0  # FakeChatModel doesn't populate cost_usd; real providers do


async def test_run_validation_ships_with_caveats_when_release_rule_kills_only_contact_channel(fake_model):
    """A missing/killed contact channel alone must NOT reject the whole record (2026-07-29
    relaxation, see PROJECT_LOG.md) — it ships with that field honestly blank
    (status=removed_failed_validation, never fabricated) since a real decision-maker +
    dated signal are still present."""
    # 2 queued V1 responses: only principal_name and why_now_trigger survive to V1 —
    # both contradictory email claims get killed by the release rule before V1 runs.
    for _ in range(2):
        fake_model.queue(AIMessage(content='{"supported": true, "reason": "ok"}'))
    claims = [
        Claim(field_name="principal_name", answer="Jane Doe", status="confirmed", confidence="high", source_url="http://a"),
        Claim(field_name="principal_email", answer="jane@acme.com", status="confirmed", confidence="medium",
              source_class="site_scrape", source_url="http://b"),
        Claim(field_name="principal_email", answer="totally-different@other.com", status="confirmed", confidence="low",
              source_class="hunter", source_url="http://c"),
        Claim(field_name="why_now_trigger", answer="fresh_liquidity", status="confirmed", confidence="medium", source_url="http://d"),
    ]
    vi = ValidationInput(entity_id="e1", claim_ledger=claims, waves_completed=["-1", "0", "1"], wave0_findings=[])
    dataset_input, audit, cost = await run_validation(vi, fake_model, fetch_fn=_supportive_fetch)

    assert dataset_input.outcome == "ship_with_caveats"
    email_statuses = {c.status for c in dataset_input.claim_ledger if c.field_name == "principal_email"}
    assert email_statuses == {"removed_failed_validation"}
    assert len(audit) == 2
    assert all(a["reason_code"] == "verification_failed" for a in audit)


async def test_run_validation_skip_if_unchanged_carries_wave0_findings_forward(fake_model, monkeypatch):
    """waves_completed only reaching wave 0 (no enrichment claims added) -> V4/V5 must
    NOT re-run; wave0_findings is carried forward verbatim instead."""
    carried = Finding(check_id="V5_firm_is_fo", severity="fatal", field="G1.Q3", detail="carried forward")
    claims = [_claim(question_id="G1.Q3", answer="yes", source_url="http://a")]
    vi = ValidationInput(entity_id="e1", claim_ledger=claims, waves_completed=["-1", "0"], wave0_findings=[carried])

    called = {"v4": False}
    original = validation_module.check_v4_contradictions

    def _spy(claims):
        called["v4"] = True
        return original(claims)

    monkeypatch.setattr(validation_module, "check_v4_contradictions", _spy)
    dataset_input, _, _ = await run_validation(vi, fake_model, fetch_fn=_supportive_fetch)

    assert called["v4"] is False
    assert any(f.detail == "carried forward" for f in dataset_input.findings)


async def test_run_validation_v1_ring_fence_stops_spending_paid_credits(fake_model):
    for _ in range(2):
        fake_model.queue(AIMessage(content='{"supported": true, "reason": "ok"}'))
    claims = [
        _claim(field_name="principal_name", answer="Jane Doe", source_url="http://a"),
        _claim(field_name="principal_email", answer="jane@acme.com", source_url="http://b"),
        _claim(field_name="why_now_trigger", answer="fresh_liquidity", source_url="http://c"),
    ]
    vi = ValidationInput(entity_id="e1", claim_ledger=claims, waves_completed=["-1", "0", "1"], wave0_findings=[])

    async def paid_fetch(url):
        return {"content": "supports it", "credits_spent": 1}

    dataset_input, _, _ = await run_validation(vi, fake_model, fetch_fn=paid_fetch, v1_credit_budget=2)
    v1_findings = [f for f in dataset_input.findings if f.check_id == "V1_source_supports"]
    assert len(v1_findings) == 2  # third claim never checked -> budget exhausted after 2 paid credits


async def test_run_validation_type_final_from_g1q4():
    claims = [_claim(question_id="G1.Q4", answer="SFO (ADV client_count=1)")]
    vi = ValidationInput(entity_id="e1", claim_ledger=claims, waves_completed=["-1", "0"], wave0_findings=[])
    dataset_input, _, _ = await run_validation(vi, None, fetch_fn=_supportive_fetch)
    assert dataset_input.type_final == "SFO"


# --- regression coverage for 2 real bugs found live-testing (2026-07-28, see PROJECT_LOG.md) ---

def test_v4_does_not_flag_differently_worded_prose_confirming_the_same_fact():
    """Real live case: "13F value $174,032,930 (2026Q3)" vs "Yes, 6th Street Advisors
    files Form 13F showing holdings, indicating deployment of capital" — both TRUE,
    both about G3.Q1, worded completely differently. Must NOT be flagged as a
    contradiction (that's a job for layer 1's own researcher, which judges semantic
    equivalence with an LLM, not a substring check)."""
    claims = [
        _claim(question_id="G3.Q1", answer="13F value $174,032,930 (2026Q3)", source_class="13f_filing"),
        _claim(question_id="G3.Q1",
                answer="Yes, 6th Street Advisors files Form 13F showing holdings, indicating deployment of capital",
                source_class="edgar_filing"),
    ]
    _, findings = check_v4_contradictions(claims)
    assert findings == []


def test_v4_still_flags_genuinely_incompatible_field_name_claims():
    """The fix must not blunt V4 entirely — a real numeric mismatch on a field_name-keyed
    (structured) claim still needs to fire."""
    claims = [
        _claim(field_name="aum_usd", answer=150_000_000, source_class="13f_filing"),
        _claim(field_name="aum_usd", answer=5_000_000, source_class="site_scrape"),
    ]
    _, findings = check_v4_contradictions(claims)
    assert len(findings) == 1


async def test_v1_skips_aum_basis_category_error_field(fake_model):
    """Real live case: V1 correctly (but uselessly) flagged aum_basis="13f_floor" as
    unsupported because no SEC filing page literally contains the string "13f_floor" —
    that's our own internal label for HOW aum_usd was computed, not a fact any page
    could ever state. Must be skipped, not spend a call getting a guaranteed-useless
    fatal finding."""
    claims = [
        _claim(field_name="aum_basis", answer="13f_floor", source_class="13f_filing", source_url="http://sec.gov/13f"),
    ]
    vi = ValidationInput(entity_id="e1", claim_ledger=claims, waves_completed=["-1", "0"], wave0_findings=[])
    dataset_input, _, _ = await run_validation(vi, fake_model, fetch_fn=_supportive_fetch)
    assert fake_model.calls == []
    v1_findings = [f for f in dataset_input.findings if f.check_id == "V1_source_supports"]
    assert v1_findings == []
    assert dataset_input.claim_ledger[0].status != "contradicted"


async def test_v1_still_checks_aum_usd_and_aum_as_of(fake_model):
    """aum_usd/aum_as_of stay in V1 scope — a dollar figure and a filing quarter ARE
    facts a source page genuinely states, unlike aum_basis's internal label."""
    fake_model.queue(AIMessage(content='{"supported": true, "reason": "matches filing"}'))
    claims = [_claim(field_name="aum_usd", answer=150_000_000, source_class="13f_filing", source_url="http://sec.gov/13f")]
    vi = ValidationInput(entity_id="e1", claim_ledger=claims, waves_completed=["-1", "0"], wave0_findings=[])
    dataset_input, _, _ = await run_validation(vi, fake_model, fetch_fn=_supportive_fetch)
    assert len(fake_model.calls) == 1

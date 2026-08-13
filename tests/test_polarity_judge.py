"""T33 — the G1.Q3/G1.Q5 identity gate judges answer POLARITY with the model, not a
leading-character string prefix.

The motivating failure (2026-08-14, see PLAN.md T33 / PROJECT_LOG.md): NOBLE FAMILY
WEALTH — a $527M ADV-registered MFO whose confirmed G1.Q3 reads "Noble Family Wealth LLC
operates as a family office, offering wealth planning, investment management, and family
governance services" — was rejected at wave 0 by ``V5_firm_is_fo:G1.Q3`` because
``_is_negative_fo_answer`` did ``ans.startswith("no")`` and the firm's NAME begins with
"No". The model judged correctly and a two-character prefix check overruled it.

T33 fixes this three ways (all covered here):
- T33.1: ``_judge_answer_polarity`` — a cheapest-tier model call decides
  affirmative/negative/unclear from the answer's substance, not its opening characters.
- T33.2: ``check_v5_firm_is_fo_hardening(claims, polarity)`` uses the verdict; on
  ``"unclear"``/missing it falls back to the deterministic string floor so a model outage
  can never silently open a HARD gate.
- T33.3: the floor (``_is_negative_fo_answer``) now matches the leading TOKEN, so "Noble"
  / "Northern" / "Nominally" are no longer negations while "No," / "None" / "Not a" /
  "There is no" / "Insufficient" still are.

The corpus uses the REAL ledger answers (Noble, ASR Vermogensbeheer, Hilltop National
Bank, and the ACIMA PRIVATE WEALTH G1.Q5 false-positive from 2026-07-29). Most tests pass
a ``polarity`` dict directly so they stay deterministic; one drives ``_judge_answer_polarity``
with a stubbed model returning garbage to prove the ``"unclear"`` fallback reaches the
string floor and still fatals a genuine non-FO.
"""
from __future__ import annotations

from langchain_core.messages import AIMessage

from app.llm import FakeChatModel
from app.questions import QUESTIONS_BY_ID
from app.state import Claim
from app.validation import (
    _judge_answer_polarity,
    _judge_claim_polarity,
    _is_negative_fo_answer,
    check_v5_firm_is_fo_hardening,
)


# --- the real ledger answers (verbatim from data/foia.db) ---

NOBLE_G1Q3 = ("Noble Family Wealth LLC operates as a family office, offering wealth "
              "planning, investment management, and family governance services")
# disc_f0f0e9f95b5d3ab0 — ASR Vermogensbeheer N.V.
ASR_G1Q3 = ("There is no affirmative evidence that ASR Vermogensbeheer N.V. operates as a "
            "family office; it is described as the asset\u2011management arm of a.s.r.")
# disc_fd2fff652a21ad7c — Hilltop National Bank
HILLTOP_G1Q3 = "There is no affirmative evidence that Hilltop National Bank operates as a family office."
# ACIMA PRIVATE WEALTH, LLC — the 2026-07-29 false-positive (G1.Q5 negative must NOT fatal)
ACIMA_G1Q5 = ("No, the entity is not merely a plain RIA in costume; it presents itself as a "
              "family office.")
ACIMA_G1Q3 = ("Yes, there is affirmative evidence that the entity operates as a family office.")

_G1Q3_TEXT = QUESTIONS_BY_ID["G1.Q3"].text
_G1Q5_TEXT = QUESTIONS_BY_ID["G1.Q5"].text


def _claim(**kw) -> Claim:
    defaults = dict(status="confirmed", confidence="medium")
    defaults.update(kw)
    return Claim(**defaults)


# --- T33.1 acceptance: _judge_answer_polarity verdicts on the real answers ---
# Driven through the judge with a stubbed FakeChatModel returning the expected JSON, to
# prove the wiring (system prompt -> parse -> verdict) end to end, not just the gate logic.

async def test_judge_noble_answer_is_affirmative(fake_model):
    fake_model.queue(AIMessage(content='{"verdict": "affirmative"}'))
    assert await _judge_answer_polarity(_G1Q3_TEXT, NOBLE_G1Q3, fake_model) == "affirmative"


async def test_judge_asr_answer_is_negative(fake_model):
    fake_model.queue(AIMessage(content='{"verdict": "negative"}'))
    assert await _judge_answer_polarity(_G1Q3_TEXT, ASR_G1Q3, fake_model) == "negative"


async def test_judge_acima_g1q5_negative_is_negative(fake_model):
    fake_model.queue(AIMessage(content='{"verdict": "negative"}'))
    assert await _judge_answer_polarity(_G1Q5_TEXT, ACIMA_G1Q5, fake_model) == "negative"


# --- T33.2 acceptance: the gate uses the polarity verdict ---

def test_noble_no_longer_fatals_with_affirmative_polarity():
    """The regression at the heart of T33: a confirmed G1.Q3 whose answer opens with the
    firm's name "Noble …" must NOT fatal once the model says affirmative."""
    claims = [_claim(question_id="G1.Q3", answer=NOBLE_G1Q3, status="confirmed")]
    findings = check_v5_firm_is_fo_hardening(claims, {"G1.Q3": "affirmative"})
    assert findings == []


def test_asr_still_fatals_with_negative_polarity():
    claims = [_claim(question_id="G1.Q3", answer=ASR_G1Q3, status="single_source")]
    findings = check_v5_firm_is_fo_hardening(claims, {"G1.Q3": "negative"})
    assert any(f.field == "G1.Q3" and f.severity == "fatal" for f in findings)


def test_hilltop_still_fatals_with_negative_polarity():
    claims = [_claim(question_id="G1.Q3", answer=HILLTOP_G1Q3, status="single_source")]
    findings = check_v5_firm_is_fo_hardening(claims, {"G1.Q3": "negative"})
    assert any(f.field == "G1.Q3" and f.severity == "fatal" for f in findings)


def test_acima_g1q5_negative_does_not_fatal():
    """The ACIMA PRIVATE WEALTH false positive (2026-07-29) must not regress: a confirmed
    G1.Q5 that DENIES being a plain RIA in costume is NOT a disqualifying RIA-in-costume."""
    claims = [
        _claim(question_id="G1.Q3", answer=ACIMA_G1Q3, status="confirmed"),
        _claim(question_id="G1.Q5", answer=ACIMA_G1Q5, status="confirmed"),
    ]
    findings = check_v5_firm_is_fo_hardening(claims, {"G1.Q5": "negative"})
    assert findings == []


def test_acima_g1q5_affirmative_does_fatal_inverted_semantics():
    """G1.Q5 is inverted: an AFFIRMATIVE answer (yes, it IS a plain RIA in costume) is the
    disqualifying one. A confirmed G1.Q5 + affirmative polarity must fatal on G1.Q5."""
    claims = [
        _claim(question_id="G1.Q3", answer=ACIMA_G1Q3, status="confirmed"),
        _claim(question_id="G1.Q5",
               answer="Yes, the entity is a plain SEC-registered RIA in costume, not a family office.",
               status="confirmed"),
    ]
    findings = check_v5_firm_is_fo_hardening(claims, {"G1.Q5": "affirmative"})
    assert any(f.field == "G1.Q5" and f.severity == "fatal" for f in findings)


# --- T33.2 floor: "unclear"/missing falls back to the deterministic string check ---

def test_unclear_polarity_falls_back_to_string_floor_and_fatals_asr():
    """A model verdict of "unclear" must NOT silently open the gate — the string floor
    still fatals a settled-negative G1.Q3."""
    claims = [_claim(question_id="G1.Q3", answer=ASR_G1Q3, status="single_source")]
    findings = check_v5_firm_is_fo_hardening(claims, {"G1.Q3": "unclear"})
    assert any(f.field == "G1.Q3" and f.severity == "fatal" for f in findings)


def test_no_polarity_falls_back_to_string_floor_and_fatals_asr():
    claims = [_claim(question_id="G1.Q3", answer=ASR_G1Q3, status="single_source")]
    findings = check_v5_firm_is_fo_hardening(claims)  # polarity omitted -> floor
    assert any(f.field == "G1.Q3" and f.severity == "fatal" for f in findings)


def test_floor_now_passes_noble_after_t33_3_leading_token_fix():
    """T33.3 acceptance: even without a model, the leading-token floor must no longer
    reject Noble (whose name begins with "No")."""
    claims = [_claim(question_id="G1.Q3", answer=NOBLE_G1Q3, status="confirmed")]
    findings = check_v5_firm_is_fo_hardening(claims)  # no polarity -> floor
    assert findings == []


def test_floor_passes_acima_g1q5_negative():
    """Floor (no polarity): a G1.Q5 opening "No, …" is not the leading token "yes", so it
    is not a disqualifying RIA-in-costume — the ACIMA regression holds on the floor too."""
    claims = [
        _claim(question_id="G1.Q3", answer=ACIMA_G1Q3, status="confirmed"),
        _claim(question_id="G1.Q5", answer=ACIMA_G1Q5, status="confirmed"),
    ]
    findings = check_v5_firm_is_fo_hardening(claims)
    assert findings == []


# --- T33.2 fail-safe: a garbage model reply degrades to "unclear" -> floor -> fatal ---

async def test_garbage_model_reply_falls_back_to_string_check_and_still_fatals_asr(fake_model):
    """The required garbage test (T33.4 case 5): a model returning unparseable output must
    not raise and must not open the gate — `_judge_answer_polarity` returns "unclear", the
    gate falls to the string floor, and a genuine non-FO (ASR) still fatals."""
    fake_model.queue(AIMessage(content="this is not json at all %%"))
    verdict = await _judge_answer_polarity(_G1Q3_TEXT, ASR_G1Q3, fake_model)
    assert verdict == "unclear"
    claims = [_claim(question_id="G1.Q3", answer=ASR_G1Q3, status="single_source")]
    findings = check_v5_firm_is_fo_hardening(claims, {"G1.Q3": verdict})
    assert any(f.field == "G1.Q3" and f.severity == "fatal" for f in findings)


async def test_judge_answer_polarity_never_raises_on_model_error():
    """A model-invocation failure degrades to "unclear", never raises — so a model outage
    can never crash the gate or silently open it."""

    class _Boom:
        async def ainvoke(self, messages, tools=None):
            raise RuntimeError("network down")

    assert await _judge_answer_polarity(_G1Q3_TEXT, NOBLE_G1Q3, _Boom()) == "unclear"


async def test_judge_claim_polarity_skips_when_model_is_none():
    """`model is None` (the wave_0 test default) returns {} — no calls, gate uses the floor."""
    claims = [_claim(question_id="G1.Q3", answer=NOBLE_G1Q3, status="confirmed")]
    assert await _judge_claim_polarity(claims, None) == {}


async def test_judge_claim_polarity_only_judges_when_verdict_matters(fake_model):
    """G1.Q3 is judged only when settled (not could_not_verify/contradicted); G1.Q5 only
    when status == "confirmed" (its sole fatal condition). Wasted calls are skipped."""
    fake_model.queue(AIMessage(content='{"verdict": "negative"}'))   # for G1.Q3
    claims = [
        _claim(question_id="G1.Q3", answer=ASR_G1Q3, status="single_source"),
        _claim(question_id="G1.Q5", answer="no", status="single_source"),  # not confirmed -> skip
    ]
    out = await _judge_claim_polarity(claims, fake_model)
    assert out == {"G1.Q3": "negative"}  # exactly one call, G1.Q5 skipped
    assert len(fake_model.calls) == 1


async def test_judge_claim_polarity_skips_g1q3_when_status_already_fatals(fake_model):
    """A could_not_verify G1.Q3 fatals on status alone, so the verdict cannot change the
    outcome — no call is spent."""
    claims = [_claim(question_id="G1.Q3", answer="anything", status="could_not_verify")]
    assert await _judge_claim_polarity(claims, fake_model) == {}
    assert fake_model.calls == []


# --- T33.3 acceptance: _is_negative_fo_answer matches the leading TOKEN ---

def test_is_negative_fo_answer_false_for_name_starting_with_no():
    assert _is_negative_fo_answer("Noble Family Wealth LLC operates as a family office") is False
    assert _is_negative_fo_answer("Northern Trust Family Office provides wealth services.") is False
    assert _is_negative_fo_answer("Nominally a family office, the firm serves one family.") is False


def test_is_negative_fo_answer_true_for_genuine_negations():
    assert _is_negative_fo_answer("No, there is no affirmative evidence that it operates as a family office.") is True
    assert _is_negative_fo_answer("None of the evidence indicates the entity is a family office.") is True
    assert _is_negative_fo_answer("Not a family office") is True
    assert _is_negative_fo_answer("There is no affirmative evidence that ASR Vermogensbeheer operates as a family office.") is True
    assert _is_negative_fo_answer("Insufficient evidence to conclude it is a family office.") is True


def test_is_negative_fo_answer_handles_punctuation_and_quotes():
    # The real ledger answers are stored with surrounding quotes and a non-breaking hyphen.
    assert _is_negative_fo_answer('"' + ASR_G1Q3 + '"') is True
    assert _is_negative_fo_answer('"' + NOBLE_G1Q3 + '"') is False


def test_is_negative_fo_answer_false_for_affirmative_without_leading_yes():
    assert _is_negative_fo_answer("Affirmative evidence shows the firm offers family office services.") is False


def test_is_negative_fo_answer_empty():
    assert _is_negative_fo_answer("") is False
    assert _is_negative_fo_answer("   ") is False
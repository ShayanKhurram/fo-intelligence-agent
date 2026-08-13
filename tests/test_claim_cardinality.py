"""PLAN.md T23.4 — regression guards for the one-claim/one-fact/one-source fix.

The live defect (CLASS VI FAMILY OFFICE, LLC, two consecutive runs): a lane that found
two real signals on two different pages concatenated them under a single `source_url`,
so V1 correctly fatals the composite claim to `contradicted` — the page only states half
of what the answer asserts. The fix is structural, in three parts:

  T23.1 — the compress prompt must tell the model to emit ONE claim PER supporting fact,
          each carrying its own `source_url`, and never join facts from different pages.
  T23.2 — when several claims exist for one question, a deterministic winner is chosen
          (`select_claim_for_question`), replacing the silent last-write-wins that made
          gates depend on list order.
  T23.3 — the projection picks the SAME winner the gates use, so the row and the gate
          never disagree about which claim won.

These tests fail against pre-T23 HEAD and pass after T23.1-T23.3.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

from app.enrichment import _project_question_claims
from app.questions import QUESTIONS_BY_LANE
from app.researcher import _COMPRESS_SYSTEM_TEMPLATE, _parse_claims_json
from app.state import Claim, select_claim_for_question

_G3_Q2_IDS = {q.question_id for q in QUESTIONS_BY_LANE["activity_signals"]}


def test_compress_template_forbids_joining_facts_from_different_pages():
    """T23.4(a): the rule against composite answers lives in the prompt constant itself,
    so it cannot be silently dropped without this test going red. The shipped defect
    concatenated "2023 hires ... and 2024 Convergence acquisition" under one URL — the
    wording this asserts is what prevents that shape."""
    t = _COMPRESS_SYSTEM_TEMPLATE.lower()
    # The one-claim/one-fact/one-source rule must be stated.
    assert "one claim" in t and "one fact" in t
    # Explicit prohibition on joining facts from different pages.
    assert "join facts from different pages" in t
    # And the positive instruction: several facts -> several claims, each with own url.
    assert "several claims" in t
    assert "its own" in t and "source_url" in t


def test_two_single_fact_claims_survive_parsing_and_project_to_one_field():
    """T23.4(b): two G3.Q2 claims with distinct `source_url`s — the shape T23.1 tells the
    model to emit instead of one composite claim — both survive `_parse_claims_json`, and
    the projection emits EXACTLY ONE `important_insight` field claim, carrying the selected
    claim's `source_url` (not both, not neither)."""
    payload = json.dumps([
        {
            "question_id": "G3.Q2",
            "answer": "Class VI Family Office acquired Convergence Controls in 2024",
            "subject_value": "2024 acquisition of Convergence Controls",
            "status": "confirmed",
            "source_url": "https://classvifamilyoffice.com/news/convergence-acquisition",
            "source_class": "web_page",
            "confidence": "high",
        },
        {
            "question_id": "G3.Q2",
            "answer": "Class VI Family Office hired Ben Krapfl and Ginny Robinson in 2024",
            "subject_value": "2024 hires of Ben Krapfl and Ginny Robinson",
            "status": "confirmed",
            "source_url": "https://www.linkedin.com/jobs/view/associate-wealth-advisor",
            "source_class": "linkedin",
            "confidence": "high",
        },
    ])
    claims = _parse_claims_json(payload, _G3_Q2_IDS)
    # Both claims are retained — parsing does not deduplicate by question_id.
    assert len(claims) == 2
    assert {c.source_url for c in claims} == {
        "https://classvifamilyoffice.com/news/convergence-acquisition",
        "https://www.linkedin.com/jobs/view/associate-wealth-advisor",
    }

    projected = _project_question_claims(claims)
    # Exactly ONE projected field claim for the one question, despite two input claims.
    assert len(projected) == 1
    assert projected[0].field_name == "important_insight"
    # It carries ONE of the two source URLs — the selected claim's — not both, not none.
    assert projected[0].source_url in {c.source_url for c in claims}


def test_selection_is_deterministic_under_shuffle():
    """T23.4(c): the winner for a question must not depend on input list order. Shuffle
    the claim list across 20 permutations and assert the SAME claim is selected every
    time (by claim_id, which is stable per Claim instance)."""
    base = datetime(2026, 8, 13, tzinfo=timezone.utc)
    claims = [
        Claim(
            question_id="G3.Q2",
            answer="older signal",
            subject_value="older",
            status="confirmed",
            source_url="https://example.com/old",
            confidence="high",
            retrieved_at=base - timedelta(days=5),
            claim_id="old-claim-id",
        ),
        Claim(
            question_id="G3.Q2",
            answer="newer signal",
            subject_value="newer",
            status="confirmed",
            source_url="https://example.com/new",
            confidence="high",
            retrieved_at=base,
            claim_id="new-claim-id",
        ),
        Claim(
            question_id="G3.Q2",
            answer="oldest signal",
            subject_value="oldest",
            status="confirmed",
            source_url="https://example.com/oldest",
            confidence="medium",
            retrieved_at=base - timedelta(days=30),
            claim_id="mid-claim-id",
        ),
    ]
    # Same-status tie-break is latest retrieved_at, so the newer claim wins.
    expected = "new-claim-id"

    selected_ids = set()
    for seed in range(20):
        order = list(claims)
        random.Random(seed).shuffle(order)
        winner = select_claim_for_question(order)
        assert winner is not None
        selected_ids.add(winner.claim_id)
    assert selected_ids == {expected}


def test_confirmed_beats_contradicted_for_same_question():
    """T24.2 (inverts the old T23.4(d) fail-safe): under the one-claim/one-fact/one-source
    model (T23.1), `contradicted` means *this particular fact* is not supported by *its
    own* cited page — which says nothing about a sibling claim answering the same
    question from a different, well-supported source. A `confirmed` sibling therefore
    outranks a `contradicted` claim for the same question (PLAN.md T24).

    Order must not matter — `confirmed` wins either way."""
    confirmed = Claim(
        question_id="G3.Q2",
        answer="a confirmed signal",
        subject_value="confirmed-value",
        status="confirmed",
        source_url="https://example.com/confirmed",
        confidence="high",
        retrieved_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
        claim_id="confirmed-id",
    )
    contradicted = Claim(
        question_id="G3.Q2",
        answer="a contradicted signal",
        subject_value="contradicted-value",
        status="contradicted",
        source_url="https://example.com/contradicted",
        confidence="low",
        retrieved_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        claim_id="contradicted-id",
    )
    assert select_claim_for_question([confirmed, contradicted]).claim_id == "confirmed-id"
    assert select_claim_for_question([contradicted, confirmed]).claim_id == "confirmed-id"


def test_lone_contradicted_claim_is_still_returned():
    """T24.2 companion: "prefer a good sibling" must never be mistaken for "discard
    contradicted claims". A question whose only claim is `contradicted` still returns
    that claim — selection picks a winner, it never filters."""
    contradicted = Claim(
        question_id="G3.Q2",
        answer="a contradicted signal",
        subject_value="contradicted-value",
        status="contradicted",
        source_url="https://example.com/contradicted",
        confidence="low",
        retrieved_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        claim_id="contradicted-id",
    )
    assert select_claim_for_question([contradicted]).claim_id == "contradicted-id"
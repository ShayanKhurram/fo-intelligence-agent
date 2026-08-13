"""PLAN.md T30 — regression corpus for the prose-fallback projection.

The live ledger has produced a blank `why_now_trigger` on every row of every run
this session even though four of six leads carry a SETTLED G3.Q2 — the researcher
found a dated signal and cited it. The cause is that the compress model fills
`subject_value` reliably for entity answers (a name, a URL, a title) and unreliably
or never for prose answers; G3.Q2 is 0-for-6 with `subject_value=None`, so the
pre-T30 projection (`_projectable` requires a non-blank `subject_value`) skipped
every one of them.

T30.1 adds a prose fallback: when `subject_value` is missing/blank AND the target
field is a prose-valued field (`why_now_trigger` / `recent_investments`), project
the claim's `answer` instead. Entity fields (`principal_name`, `principal_title`,
`principal_linkedin`) NEVER fall back — a whole sentence in `principal_name` is the
T26/T27/T29 defect. T30.2 guards the fallback against statement-of-absence answers
("No recent exits, hires, or commitments were found") with a LEADING-TOKEN
negation match (so "Norwegian" is not caught by "No"). The negation guard applies
ONLY to the fallback, never to an explicit `subject_value`.
"""
from __future__ import annotations

from app.enrichment import _is_negative_answer, _project_question_claims
from app.state import Claim


_CLASS_VI_JOB_URL = "https://www.linkedin.com/jobs/view/associate-wealth-advisor-classvi"
_RESEARCHER_SOURCE_CLASS = "web_page"


def _g3q2_classvi_job_posting(subject_value: str | None = None) -> Claim:
    """Class VI's real G3.Q2 shape: single_source, subject_value=None, a dated
    job-posting answer, real source_url. Run 4 found a LinkedIn job posting one day
    old — as good a why-now trigger as this pipeline is likely to produce — and it
    never reached a row."""
    return Claim(
        question_id="G3.Q2",
        answer=(
            "LinkedIn job posting for an Associate Wealth Advisor at Class VI Family "
            "Office, posted 1 day ago"
        ),
        subject_value=subject_value,
        status="single_source",
        source_url=_CLASS_VI_JOB_URL,
        source_class=_RESEARCHER_SOURCE_CLASS,
        confidence="high",
        produced_by="research",
    )


def _projected_field(claims: list[Claim], field_name: str) -> Claim | None:
    out = _project_question_claims(claims)
    for c in out:
        if c.field_name == field_name:
            return c
    return None


# --- T30.1: prose fallback -------------------------------------------------


def test_g3q2_prose_projects_why_now_trigger_with_source_preserved():
    """(a) A settled G3.Q2 with subject_value=None and a dated job-posting answer
    projects a why_now_trigger claim carrying that prose, with the original
    source_url/source_class preserved (the citation must survive the projection so
    the row stays auditable back to the page the fact came from)."""
    layer1 = _g3q2_classvi_job_posting(subject_value=None)
    projected = _projected_field([layer1], "why_now_trigger")
    assert projected is not None, "settled G3.Q2 with prose answer must project"
    assert (
        projected.answer
        == "LinkedIn job posting for an Associate Wealth Advisor at Class VI Family "
        "Office, posted 1 day ago"
    )
    assert projected.source_url == _CLASS_VI_JOB_URL
    assert projected.source_class == _RESEARCHER_SOURCE_CLASS
    assert projected.extraction_method == "projected_G3.Q2"


def test_g3q1_prose_projects_recent_investments():
    """(b) The same prose-fallback shape on G3.Q1 projects recent_investments."""
    layer1 = Claim(
        question_id="G3.Q1",
        answer=(
            "Class VI closed a $120M growth fund in Q3 2026, anchored by a sovereign "
            "wealth fund commitment"
        ),
        subject_value=None,
        status="single_source",
        source_url="https://classvifamilyoffice.com/news/fund-close",
        source_class=_RESEARCHER_SOURCE_CLASS,
        confidence="high",
        produced_by="research",
    )
    projected = _projected_field([layer1], "recent_investments")
    assert projected is not None
    assert projected.answer.startswith("Class VI closed a $120M growth fund")
    assert projected.source_url == "https://classvifamilyoffice.com/news/fund-close"
    assert projected.extraction_method == "projected_G3.Q1"


def test_prose_g2q1_does_not_project_principal_name():
    """(c) A prose G2.Q1 with no subject_value projects NO principal_name — entity
    fields never fall back, so a whole sentence never lands in principal_name (the
    T26/T27/T29 defect)."""
    layer1 = Claim(
        question_id="G2.Q1",
        answer="The firm is led by a managing director whose name was not published.",
        subject_value=None,
        status="single_source",
        source_url="https://example.com/about",
        source_class=_RESEARCHER_SOURCE_CLASS,
        confidence="high",
        produced_by="research",
    )
    assert _projected_field([layer1], "principal_name") is None


def test_explicit_subject_value_wins_over_prose_fallback():
    """(d) When both subject_value and a prose answer are present, subject_value
    wins — the model deliberately distilled a value and that is respected."""
    layer1 = _g3q2_classvi_job_posting(subject_value="Associate Wealth Advisor role posted")
    projected = _projected_field([layer1], "why_now_trigger")
    assert projected is not None
    assert projected.answer == "Associate Wealth Advisor role posted"
    assert projected.source_url == _CLASS_VI_JOB_URL


# --- T30.2: negation guard -------------------------------------------------


def test_negative_answer_does_not_project():
    """(e) A statement-of-absence answer projects nothing — a "no recent activity"
    answer is a reason NOT to call, not a why-now trigger."""
    layer1 = Claim(
        question_id="G3.Q2",
        answer="No recent exits, hires, or commitments were found for Class VI.",
        subject_value=None,
        status="single_source",
        source_url="https://example.com/research",
        source_class=_RESEARCHER_SOURCE_CLASS,
        confidence="high",
        produced_by="research",
    )
    assert _projected_field([layer1], "why_now_trigger") is None


def test_norwegian_trap_projects():
    """(e) The leading-token match must NOT catch "Norwegian" — a substring "No"
    match would wrongly suppress a real signal. "Norwegian sovereign fund
    commitment announced" DOES project."""
    layer1 = Claim(
        question_id="G3.Q2",
        answer="Norwegian sovereign fund commitment announced this quarter.",
        subject_value=None,
        status="single_source",
        source_url="https://example.com/news",
        source_class=_RESEARCHER_SOURCE_CLASS,
        confidence="high",
        produced_by="research",
    )
    projected = _projected_field([layer1], "why_now_trigger")
    assert projected is not None
    assert projected.answer == "Norwegian sovereign fund commitment announced this quarter."


def test_none_leading_projects_nothing():
    """(e) "none of the partners commented" — a leading "none" projects nothing."""
    layer1 = Claim(
        question_id="G3.Q2",
        answer="None of the partners commented on the recent fundraising round.",
        subject_value=None,
        status="single_source",
        source_url="https://example.com/news",
        source_class=_RESEARCHER_SOURCE_CLASS,
        confidence="high",
        produced_by="research",
    )
    assert _projected_field([layer1], "why_now_trigger") is None


def test_is_negative_answer_unit():
    """Direct unit checks on the negation guard, including the leading-token
    distinction (no / Norwegian) and the multi-word phrases."""
    assert _is_negative_answer("No recent exits, hires, or commitments were found")
    assert _is_negative_answer("none of the partners commented")
    assert _is_negative_answer("Not enough public information to confirm a trigger")
    assert _is_negative_answer("Never publicly disclosed a recent commitment")
    assert _is_negative_answer("Unable to find a dated signal")
    assert _is_negative_answer("Insufficient evidence of any recent activity")
    assert _is_negative_answer("There is no recent signal to act on")
    assert _is_negative_answer("There are no recent investments to report")
    # The trap: leading-token match, not substring.
    assert not _is_negative_answer("Norwegian sovereign fund commitment announced")
    assert not _is_negative_answer("Recently closed a $120M growth fund")
    # An affirmative sentence is not negative.
    assert not _is_negative_answer("LinkedIn job posting posted 1 day ago")


# --- T28 status gate unchanged --------------------------------------------


def test_could_not_verify_g3q2_projects_nothing():
    """(f) A could_not_verify G3.Q2 still projects nothing — the T28 status gate
    (`status in _PROJECTABLE_STATUSES`) is unchanged by the prose fallback, so a
    question the researcher could not answer does not emit a prose why_now_trigger
    even though the answer text is non-empty and non-negative."""
    layer1 = Claim(
        question_id="G3.Q2",
        answer="Could not confirm any recent dated signal for Class VI.",
        subject_value=None,
        status="could_not_verify",
        source_url="https://example.com/research",
        source_class=_RESEARCHER_SOURCE_CLASS,
        confidence="high",
        produced_by="research",
    )
    assert _projected_field([layer1], "why_now_trigger") is None
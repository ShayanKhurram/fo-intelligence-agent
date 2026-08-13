"""PLAN.md T31 — a guarded fallback for ``principal_title``.

Two live defects, one gate:

1. **A correct title that cannot reach the row.** Class VI, run 8: ``G2.Q1``
   ``subject_value="Chris Younger"`` and ``G2.Q3`` ``subject_value=None``
   ``answer="CEO"`` on the SAME team page. ``"CEO"`` is short, correct, and on the
   same page as the name — and it did not project, because T30's fallback covered
   only the prose fields (``why_now_trigger`` / ``recent_investments``) and
   ``principal_title`` was excluded under "entity fields must never fall back",
   a rule written to stop a SENTENCE landing in ``principal_name``. That blanket
   rule also blocks a clean three-character answer landing in ``principal_title``.

2. **A firm name accepted as a title.** ``WE FAMILY OFFICES`` shipped
   ``principal_title = "WE Family Offices"`` — the FIRM's name in the title column.
   T27's guard rejects a title matching the PRINCIPAL's name; nothing rejected one
   matching the FIRM's.

T31.1 adds ``_is_plausible_title`` (empty / >80 chars / name-match / firm-match /
negation). T31.2 enables a ``principal_title`` answer-fallback that is gated by the
FULL check, while a MODEL-ASSERTED ``subject_value`` is gated only on the malformed
subset (empty/long/negative) and leaves name/firm attribution to
``_reconcile_principal_title``, which SUPERSEDES (never deletes) so the ledger
records what the model asserted (PLAN.md T31 round 2). T31 round 2 also adds the
firm-match clause to ``_reconcile_principal_title`` so a firm-name-as-title is
rejected the same way a person's name is — with the record intact.
"""
from __future__ import annotations

from app.enrichment import (
    _is_plausible_title,
    _project_question_claims,
    wave_minus_1,
)
from app.state import Claim


_CLASS_VI_TEAM_URL = "https://www.classvipartners.com/our-team/chris-younger/"
_RESEARCHER_SOURCE_CLASS = "web_page"


# --- helpers ---------------------------------------------------------------


def _q1(
    name: str,
    source_url: str,
    *,
    status: str = "single_source",
    source_class: str = _RESEARCHER_SOURCE_CLASS,
    claim_id: str | None = None,
) -> Claim:
    return Claim(
        question_id="G2.Q1",
        answer=f"{name} is a decision-maker cited on the team page",
        subject_value=name,
        status=status,
        source_url=source_url,
        source_class=source_class,
        confidence="high",
        produced_by="research",
        **({"claim_id": claim_id} if claim_id else {}),
    )


def _q3(
    title: str | None,
    source_url: str,
    *,
    answer: str | None = None,
    status: str = "single_source",
    source_class: str = _RESEARCHER_SOURCE_CLASS,
    claim_id: str | None = None,
) -> Claim:
    """A G2.Q3 claim. ``title`` is the ``subject_value`` (None models the compress
    step omitting it); ``answer`` defaults to a sentence wrapping it."""
    return Claim(
        question_id="G2.Q3",
        answer=answer if answer is not None else f"current title is {title}",
        subject_value=title,
        status=status,
        source_url=source_url,
        source_class=source_class,
        confidence="high",
        produced_by="research",
        **({"claim_id": claim_id} if claim_id else {}),
    )


def _projected(claims: list[Claim], field_name: str) -> Claim | None:
    for c in claims:
        if c.field_name == field_name and (c.extraction_method or "").startswith("projected_"):
            return c
    return None


# --- (a) Class VI: "CEO" recovered from the answer on the same page --------


def test_classvi_ceo_recovered_from_answer_sharing_source_url():
    """The run-8 shape: G2.Q1 subject_value="Chris Younger" and G2.Q3
    subject_value=None answer="CEO" on one team-page URL. The title fallback
    recovers "CEO", and the projected principal_name and principal_title share
    that source_url — the citation survives the projection so the row stays
    auditable back to the page the fact came from."""
    layer1 = [
        _q1("Chris Younger", _CLASS_VI_TEAM_URL, claim_id="cy-q1"),
        _q3(None, _CLASS_VI_TEAM_URL, answer="CEO", claim_id="cy-q3"),
    ]
    projected = _project_question_claims(layer1)

    name = _projected(projected, "principal_name")
    title = _projected(projected, "principal_title")
    assert name is not None and name.answer == "Chris Younger"
    assert title is not None and title.answer == "CEO"
    # Name and title describe the same person from the same page.
    assert name.source_url == _CLASS_VI_TEAM_URL
    assert title.source_url == _CLASS_VI_TEAM_URL
    assert title.extraction_method == "projected_G2.Q3"


def test_classvi_ceo_survives_reconcile_through_wave_minus_1():
    """End-to-end through ``wave_minus_1`` (the real entry point): the recovered
    "CEO" survives ``_reconcile_principal_title`` (not superseded — it is a genuine
    title, neither the principal's name nor the firm's)."""
    layer1 = [
        _q1("Chris Younger", _CLASS_VI_TEAM_URL, claim_id="cy-q1"),
        _q3(None, _CLASS_VI_TEAM_URL, answer="CEO", claim_id="cy-q3"),
    ]
    claims = wave_minus_1(
        [], "CLASS VI PARTNERS, LLC", [], layer1_claims=layer1
    )

    name = _projected(claims, "principal_name")
    title = _projected(claims, "principal_title")
    assert name is not None and name.answer == "Chris Younger"
    assert title is not None and title.answer == "CEO"
    assert title.status != "superseded"
    assert name.source_url == title.source_url == _CLASS_VI_TEAM_URL


# --- (b) WE FAMILY: a firm name in the title column is superseded -----------


def test_we_family_firm_name_as_title_is_superseded_not_absent():
    """WE FAMILY shipped principal_title = "WE Family Offices" (the FIRM's name).
    On the MODEL-ASSERTED path the subject_value is projected (the ledger records
    what the model asserted), then ``_reconcile_principal_title``'s firm guard
    SUPERSEDES it — same treatment as a person's name in T27, with the record
    intact. So no LIVE principal_title, but the claim exists as superseded."""
    layer1 = [
        _q3("WE Family Offices", "https://we-familyoffices.com/about",
            answer="The firm is called WE Family Offices.", claim_id="we-q3"),
    ]
    claims = wave_minus_1(
        [], "WE FAMILY OFFICES", [], layer1_claims=layer1
    )

    titles_live = [
        c for c in claims
        if c.field_name == "principal_title" and c.status != "superseded"
    ]
    assert titles_live == [], (
        f"a firm name must not survive as a live principal_title, got {titles_live}"
    )
    superseded = [
        c for c in claims
        if c.field_name == "principal_title" and c.status == "superseded"
    ]
    assert len(superseded) == 1, (
        f"the firm-name-as-title must be kept as a superseded audit record, got {superseded}"
    )
    assert superseded[0].answer == "WE Family Offices"
    assert superseded[0].extraction_method == "projected_G2.Q3"


# --- (c) a prose answer on G2.Q3 does not become a title -------------------


def test_long_prose_answer_on_g2q3_projects_no_title():
    """A 120-character prose answer on G2.Q3 projects NO principal_title — the
    >80-char clause of ``_is_plausible_title`` stops a sentence landing in the
    title column. (The length limit is what makes a title fallback safe and a
    name fallback unsafe.)"""
    prose = (
        "Chris Younger has spent the last fifteen years building Class VI's wealth "
        "advisory practice and now serves as its chief executive."
    )
    assert len(prose) > 80
    layer1 = [_q3(None, "https://example.com/team", answer=prose)]
    projected = _project_question_claims(layer1)
    assert _projected(projected, "principal_title") is None


# --- (d) fallback path declines a name-as-title ---------------------------


def test_fallback_path_declines_a_name_as_title():
    """On the FALLBACK path (subject_value missing), a name-as-title is declined
    outright — ``_is_plausible_title``'s name-match clause returns False, so we do
    not manufacture a claim we already know is invalid. No claim at all (contrast
    with the model-asserted path in case (d) of T27, which emits then supersedes
    to keep the audit record)."""
    layer1 = [
        _q1("Glen Tullman", "https://example.com/glen", claim_id="gt-q1"),
        _q3(None, "https://example.com/glen", answer="Glen Tullman", claim_id="gt-q3"),
    ]
    projected = _project_question_claims(layer1)
    # principal_name still projects; principal_title does not.
    assert _projected(projected, "principal_name") is not None
    assert _projected(projected, "principal_title") is None


# --- (e) principal_name never falls back to a prose answer -----------------


def test_principal_name_never_falls_back_to_prose():
    """A G2.Q1 with subject_value=None and a prose answer projects NO principal_name
    — the T30 rule holds: principal_name never falls back, ever. The length limit
    is what makes a title fallback safe and a name fallback unsafe."""
    layer1 = [
        Claim(
            question_id="G2.Q1",
            answer="The firm is led by a managing director whose name was not published.",
            subject_value=None,
            status="single_source",
            source_url="https://example.com/about",
            source_class=_RESEARCHER_SOURCE_CLASS,
            confidence="high",
            produced_by="research",
        ),
    ]
    assert _projected(_project_question_claims(layer1), "principal_name") is None


def test_explicit_plausible_subject_value_wins_over_answer():
    """An explicit, plausible subject_value still wins over the answer — the
    model-asserted path is respected when the value is a real title."""
    layer1 = [
        _q1("Matt Blackburn", "https://example.com/matt", claim_id="mb-q1"),
        _q3("Managing Partner", "https://example.com/matt",
            answer="Matt Blackburn is the Managing Partner.", claim_id="mb-q3"),
    ]
    projected = _project_question_claims(layer1)
    title = _projected(projected, "principal_title")
    assert title is not None and title.answer == "Managing Partner"


# --- (f) _is_plausible_title unit cases (T31.1) ---------------------------


def test_is_plausible_title_unit_cases():
    """Direct unit checks on the plausibility gate, including canonical_name=None
    (firm clause skipped) and the attribution clauses."""
    # Short, genuine titles.
    assert _is_plausible_title("CEO", None, None) is True
    assert _is_plausible_title("Managing Partner", None, None) is True
    # MB's live title is 43 chars and mentions the firm, but with canonical_name
    # None the firm clause is skipped — a real title.
    assert _is_plausible_title(
        "Managing Partner at MB Family Advisors, LLC", None, None
    ) is True

    # Firm match: "WE Family Offices" against canonical "WE FAMILY OFFICES".
    assert _is_plausible_title(
        "WE Family Offices", None, "WE FAMILY OFFICES"
    ) is False
    # Name match: a title that IS the principal's name.
    assert _is_plausible_title("Glen Tullman", "Glen Tullman", None) is False

    # A 120-character sentence is prose, not a title.
    long_title = (
        "Co-Founder, Managing Partner and Chief Investment Officer with additional "
        "responsibilities for the firm's strategic investment committee oversight"
    )
    assert len(long_title) > 80
    assert _is_plausible_title(long_title, None, None) is False

    # A statement-of-absence answer is not a title.
    assert _is_plausible_title("No title could be determined", None, None) is False

    # Empty / blank / None.
    assert _is_plausible_title("", None, None) is False
    assert _is_plausible_title("   ", None, None) is False
    assert _is_plausible_title(None, None, None) is False

    # canonical_name=None skips ONLY the firm clause — a name match still rejects.
    assert _is_plausible_title("Glen Tullman", "Glen Tullman", None) is False
    # And a short non-firm title is plausible even with a canonical name present.
    assert _is_plausible_title("CEO", "Chris Younger", "CLASS VI PARTNERS, LLC") is True
"""PLAN.md T29 — pick the principal by seniority, get the title for free.

The determinism test is the point of the whole task: ``CLASS VI FAMILY OFFICE`` named
a DIFFERENT principal on each of the last three live runs (Chris Younger/CEO,
Matt Blackburn/Managing Partner, Dalyce Tinico/Director of Wealth Advisory) because
G2.Q1 was satisfied by any of them and the final tie-break was a fresh ``uuid4`` per
run. T29.1-T29.2 move the selection into CODE: the principal whose paired title is the
most senior by ``_role_rank`` wins, with the title projected from the SAME page — so
``principal_name`` and ``principal_title`` always describe the same person.

All cases here fail against pre-T29 HEAD (the Class VI pick depended on ``claim_id``,
so shuffling the input changed the winner) and pass after T29.1-T29.3.
"""
from __future__ import annotations

import random

from app.enrichment import _project_question_claims, _role_rank, _ROLE_PRIORITY, wave_minus_1
from app.state import Claim


# --- helpers ---------------------------------------------------------------

def _q1(name: str, source_url: str, *, status: str = "confirmed",
        source_class: str = "web_page", retrieved_at: str | None = "2026-08-13T00:00:00Z",
        claim_id: str | None = None) -> Claim:
    return Claim(
        question_id="G2.Q1",
        answer=f"{name} is a decision-maker",
        subject_value=name,
        status=status,
        source_url=source_url,
        source_class=source_class,
        retrieved_at=_dt(retrieved_at),
        confidence="high",
        produced_by="research",
        **({"claim_id": claim_id} if claim_id else {}),
    )


def _q3(title: str, source_url: str, *, status: str = "confirmed",
        source_class: str = "web_page", retrieved_at: str | None = "2026-08-13T00:00:00Z",
        claim_id: str | None = None) -> Claim:
    return Claim(
        question_id="G2.Q3",
        answer=f"current title is {title}",
        subject_value=title,
        status=status,
        source_url=source_url,
        source_class=source_class,
        retrieved_at=_dt(retrieved_at),
        confidence="high",
        produced_by="research",
        **({"claim_id": claim_id} if claim_id else {}),
    )


def _dt(s: str | None):
    if s is None:
        return None
    from datetime import datetime, timezone
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _projected(claims: list[Claim], field_name: str) -> Claim | None:
    for c in claims:
        if c.field_name == field_name and (c.extraction_method or "").startswith("projected_"):
            return c
    return None


# The real Class VI three-person ledger: each (name, title) pair shares a source_url.
_CLASS_VI = [
    (_q1("Chris Younger", "https://classvi.com/team/chris-younger",
         claim_id="cy-q1"),
     _q3("CEO", "https://classvi.com/team/chris-younger",
         claim_id="cy-q3")),
    (_q1("Matt Blackburn", "https://classvi.com/team/matt-blackburn",
         claim_id="mb-q1"),
     _q3("Managing Partner", "https://classvi.com/team/matt-blackburn",
         claim_id="mb-q3")),
    (_q1("Dalyce Tinoco", "https://classvi.com/team/dalyce-tinoco",
         claim_id="dt-q1"),
     _q3("Director of Wealth Advisory", "https://classvi.com/team/dalyce-tinoco",
         claim_id="dt-q3")),
]


# --- (a) the determinism test: the point of T29 ----------------------------

def test_classvi_pick_is_deterministic_across_20_shuffles():
    """The regression guard for 'a different principal every run'. Build the real
    Class VI three-person ledger, shuffle the input order 20 times, and assert the
    SAME principal and title are selected every time — Chris Younger / CEO, the most
    senior by `_role_rank`. Against pre-T29 HEAD the winner tracks `claim_id`, so a
    shuffle changes who wins."""
    seen: list[tuple[str | None, str | None]] = []
    for i in range(20):
        flat: list[Claim] = []
        for q1, q3 in _CLASS_VI:
            flat.extend([q1, q3])
        rng = random.Random(1000 + i)
        rng.shuffle(flat)
        projected = _project_question_claims(flat)
        name = _projected(projected, "principal_name")
        title = _projected(projected, "principal_title")
        seen.append((name.answer if name else None, title.answer if title else None))

    assert all(s == ("Chris Younger", "CEO") for s in seen), (
        f"expected Chris Younger / CEO on every shuffle, got {set(seen)}"
    )


def test_classvi_pick_is_chris_younger_ceo():
    """CEO beats Managing Partner beats Director of Wealth Advisory: the most senior
    role wins, not the first-seen or any other claim."""
    flat: list[Claim] = []
    for q1, q3 in _CLASS_VI:
        flat.extend([q1, q3])
    projected = _project_question_claims(flat)
    assert _projected(projected, "principal_name").answer == "Chris Younger"
    assert _projected(projected, "principal_title").answer == "CEO"


# --- (b) a firm with no CEO selects its Managing Partner -------------------

def test_firm_with_no_ceo_selects_managing_partner():
    """No CEO in the ledger -> the Managing Partner is the most senior recognised
    role and wins. (Many family offices have no CEO — Class VI's Blackburn is
    'Managing Partner', Arden's principal is 'Managing Member'.)"""
    ledger = [
        (_q1("Matt Blackburn", "https://x.com/matt", claim_id="mb1"),
         _q3("Managing Partner", "https://x.com/matt", claim_id="mb3")),
        (_q1("Dalyce Tinoco", "https://x.com/dalyce", claim_id="dt1"),
         _q3("Director of Wealth Advisory", "https://x.com/dalyce", claim_id="dt3")),
    ]
    flat: list[Claim] = []
    for q1, q3 in ledger:
        flat.extend([q1, q3])
    projected = _project_question_claims(flat)
    assert _projected(projected, "principal_name").answer == "Matt Blackburn"
    assert _projected(projected, "principal_title").answer == "Managing Partner"


# --- (c) a principal with no title still projects (ranked last) -----------

def test_principal_with_no_title_still_projects():
    """A named principal with NO paired G2.Q3 still projects as principal_name — a
    named principal with an unknown title beats no principal (PLAN.md T29.2). No
    principal_title is projected (there was none to pair)."""
    layer1 = [
        _q1("Chris Younger", "https://x.com/chris", claim_id="cy1"),
        _q1("Matt Blackburn", "https://x.com/matt", claim_id="mb1"),
    ]
    projected = _project_question_claims(layer1)
    name = _projected(projected, "principal_name")
    assert name is not None
    # Both have no title -> rank last -> T23.2 tie-break decides. Both are confirmed
    # with identical timestamps, so the stable claim_id order picks "cy1" < "mb1".
    assert name.answer == "Chris Younger"
    assert _projected(projected, "principal_title") is None


# --- (d) name and title always share a source_url --------------------------

def test_name_and_title_share_source_url():
    """The winning principal_name and principal_title must come from the SAME
    source_url — closing the coherence gap that produced 'Chris Younger, Director of
    Wealth Advisory' (someone else's title) on a prior run."""
    flat: list[Claim] = []
    for q1, q3 in _CLASS_VI:
        flat.extend([q1, q3])
    projected = _project_question_claims(flat)
    name = _projected(projected, "principal_name")
    title = _projected(projected, "principal_title")
    assert name is not None and title is not None
    assert name.source_url == title.source_url
    assert name.source_url == "https://classvi.com/team/chris-younger"


# --- (e) T23.2 tie-break is stable when two principals share a rank --------

def test_same_rank_principals_fall_back_to_t23_tiebreak_and_are_stable():
    """Two principals with the SAME role rank (both Managing Partner) fall back to the
    T23.2 order (status, then retrieved_at, then claim_id) and the pick is stable
    across shuffles — selection never depends on input list order."""
    ledger = [
        (_q1("Ann A", "https://x.com/ann", claim_id="aaa",
             retrieved_at="2026-08-13T00:00:00Z"),
         _q3("Managing Partner", "https://x.com/ann", claim_id="aaa-t")),
        (_q1("Bob B", "https://x.com/bob", claim_id="bbb",
             retrieved_at="2026-08-13T00:00:00Z"),
         _q3("Managing Partner", "https://x.com/bob", claim_id="bbb-t")),
    ]
    flat: list[Claim] = []
    for q1, q3 in ledger:
        flat.extend([q1, q3])
    seen: list[str | None] = []
    for i in range(20):
        rng = random.Random(700 + i)
        shuffled = flat[:]
        rng.shuffle(shuffled)
        projected = _project_question_claims(shuffled)
        seen.append(_projected(projected, "principal_name").answer)
    # Same rank -> claim_id tie-break -> "aaa" < "bbb" -> Ann A wins every time.
    assert all(s == "Ann A" for s in seen), f"expected Ann A every time, got {set(seen)}"


# --- (f) _role_rank unit cases (T29.1) -------------------------------------

def test_role_rank_ceo_and_chief_executive_are_rank_0():
    assert _role_rank("CEO") == 0
    assert _role_rank("Chief Executive Officer") == 0


def test_role_rank_managing_partner_is_1():
    assert _role_rank("Managing Partner") == 1


def test_role_rank_managing_member_is_2():
    assert _role_rank("Managing Member") == 2


def test_role_rank_director_of_wealth_advisory_ranks_last():
    assert _role_rank("Director of Wealth Advisory") == len(_ROLE_PRIORITY)


def test_role_rank_none_and_blank_rank_last():
    assert _role_rank(None) == len(_ROLE_PRIORITY)
    assert _role_rank("") == len(_ROLE_PRIORITY)
    assert _role_rank("   ") == len(_ROLE_PRIORITY)


def test_role_rank_deceased_does_not_match_ceo():
    """The substring trap (PLAN.md T29.1): 'ceo' must NOT match inside another word.
    'Deceased' must rank last, not 0 — a deceased-status note is not a CEO title."""
    assert _role_rank("Deceased") == len(_ROLE_PRIORITY)


def test_role_rank_case_insensitive():
    assert _role_rank("ceo") == 0
    assert _role_rank("Ceo") == 0
    assert _role_rank("MANAGING PARTNER") == 1
    assert _role_rank("managing partner") == 1


def test_role_rank_cio_coo_cfo_do_not_match_inside_longer_words():
    """The short-label trap applies to cio/coo/cfo too — 'sociology' contains 'cio'
    as a substring but is not a CIO title; word boundaries must prevent the match."""
    assert _role_rank("sociology") == len(_ROLE_PRIORITY)
    assert _role_rank("scoop") == len(_ROLE_PRIORITY)  # 'coo' substring trap
    assert _role_rank("Chief Investment Officer") == 5
    assert _role_rank("CIO") == 5
    assert _role_rank("Chief Operating Officer") == 9
    assert _role_rank("COO") == 9
    assert _role_rank("Chief Financial Officer") == 10
    assert _role_rank("CFO") == 10


def test_role_rank_short_label_needs_leading_and_trailing_boundary():
    """Both edges of a short label need a word boundary, not just the trailing one.
    'myceo,' has 'ceo' preceded by a word char and followed by a non-word char — the
    leading boundary must block the match so a stray 'ceo' inside a longer token does
    not rank as CEO. (Catches the \bterm1|term2\b alternation bug, which only bounds
    the first/last alternative.)"""
    assert _role_rank("myceo,") == len(_ROLE_PRIORITY)
    assert _role_rank("xcooy") == len(_ROLE_PRIORITY)
    assert _role_rank("lincioz") == len(_ROLE_PRIORITY)
    # And the standalone labels still match when both boundaries are present.
    assert _role_rank("CEO") == 0
    assert _role_rank("CIO") == 5
    assert _role_rank("COO") == 9
    assert _role_rank("CFO") == 10
    # A non-word char on either side DOES count as a boundary, so '.cio' or 'ceo,'
    # are recognised — the trap is only a WORD char adjacent to the label.
    assert _role_rank(".cio") == 5
    assert _role_rank("ceo,") == 0


# --- (g) end-to-end through wave_minus_1: title survives reconciliation ---

def test_wave_minus_1_projects_chris_younger_ceo_for_classvi():
    """End-to-end: the Class VI three-person ledger run through ``wave_minus_1``
    (the real entry point) projects Chris Younger as principal_name and CEO as
    principal_title, sharing a source_url, with the title surviving
    ``_reconcile_principal_title`` (not superseded — it is a genuine title, not a
    name)."""
    layer1: list[Claim] = []
    for q1, q3 in _CLASS_VI:
        layer1.extend([q1, q3])
    claims = wave_minus_1([], "CLASS VI FAMILY OFFICE, LLC", [], layer1_claims=layer1)

    name = _projected(claims, "principal_name")
    title = _projected(claims, "principal_title")
    assert name is not None and name.answer == "Chris Younger"
    assert title is not None and title.answer == "CEO"
    assert title.status != "superseded"
    # Name and title describe the same person from the same page.
    assert name.source_url == title.source_url
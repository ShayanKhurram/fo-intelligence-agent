"""PLAN.md T28 — a force re-run must not silently delete the researcher's findings.

The regression this module exists to prevent, expressed END-TO-END (not as a unit
check on the status frozenset): on an entity that has already been through validation
once, Layer V has rewritten the researcher's confirmed ``G2.Q1`` claim from
``confirmed`` to ``single_source`` (or ``verified``). The pre-T28 projection gated on
``status == "confirmed"``, so a ``process_entity(..., force=True)`` re-run over that
already-validated ledger SKIPPED the projection — ``principal_name`` vanished from
research and ``_select_principal_name`` fell back to the discovery feed, which on a
``fec_employer`` lead is a campaign donor (observed live on TULLMAN: ``'TULL, CAYLEY'``
won instead of ``'Glen Tullman'``). A forced re-run produced a strictly worse row than
the first pass, silently.

All cases here fail against pre-T28 HEAD and pass after T28.1.
"""
from __future__ import annotations

import pytest

from app.enrichment import _project_question_claims, _select_principal_name, wave_minus_1
from app.state import Claim


# --- helpers ---------------------------------------------------------------

def _fec_source(people: list[dict], url: str = "http://fec.gov/row"):
    """A ``fec_employer`` discovery source carrying a ``people`` list of donors, the
    shape ``app/ingest.py``'s fallback branch emits (see ``_principal_from_people``)."""
    return {
        "source_class": "fec_employer",
        "payload": {"signals": {}, "people": people},
        "url": url,
        "retrieved_at": "2026-08-13T00:00:00Z",
    }


_DONOR_SOURCE = _fec_source(
    [{"name": "TULL, CAYLEY", "title": "DONOR"}],
    url="https://www.fec.gov/data/independent-expenditures/row",
)

_RESEARCH_URL = "https://www.tullman.com/our-team/glen-tullman"
_RESEARCH_SOURCE_CLASS = "web_page"


def _layer1(status: str) -> Claim:
    """A Layer-1 ``G2.Q1`` claim in the state it is in AFTER validation: a real
    ``subject_value`` (``"Glen Tullman"``), a real ``source_url`` the projection must
    preserve, and ``status`` set to whatever Layer V rewrote it to."""
    return Claim(
        question_id="G2.Q1",
        answer="Glen Tullman is the founder and managing partner",
        subject_value="Glen Tullman",
        status=status,
        source_url=_RESEARCH_URL,
        source_class=_RESEARCH_SOURCE_CLASS,
        confidence="high",
        produced_by="research",
    )


def _projected_principal(claims: list[Claim]) -> Claim | None:
    """The ``projected_G2.Q1`` principal_name claim, if any survived the projection."""
    for c in claims:
        if (
            c.field_name == "principal_name"
            and (c.extraction_method or "") == "projected_G2.Q1"
        ):
            return c
    return None


# --- the regression --------------------------------------------------------

def test_force_rerun_over_validated_claims_keeps_researched_principal():
    """T28.2 case 1 — the live bug. An entity with a ``fec_employer`` donor source PLUS
    a Layer-1 ``G2.Q1`` claim in its POST-VALIDATION state (``single_source``) must, on
    a second ``wave_minus_1`` pass, still project the researched principal — so
    ``_select_principal_name`` returns ``"Glen Tullman"``, not the donor
    ``'TULL, CAYLEY'``. Against pre-T28 HEAD this selects the donor (the projection is
    skipped, so only the ``derived_fec_employer`` donor remains)."""
    claims = wave_minus_1(
        [_DONOR_SOURCE],
        canonical_name="TULLMAN INVESTMENT MANAGEMENT",
        layer1_claims=[_layer1("single_source")],
    )

    # The projection fired: the researcher's finding reached the claim list, with the
    # researcher's citation preserved (not relabelled "derived").
    projected = _projected_principal(claims)
    assert projected is not None
    assert projected.answer == "Glen Tullman"
    assert projected.source_url == _RESEARCH_URL
    assert projected.source_class == _RESEARCH_SOURCE_CLASS

    # And the T26 provenance ranking picks the projected principal over the donor —
    # i.e. a second pass yields the SAME principal the first pass did.
    assert _select_principal_name(claims) == "Glen Tullman"


def test_contradicted_claim_projects_nothing():
    """T28.2 case 2 — the inverse. The same ledger with ``G2.Q1`` at ``contradicted``
    projects nothing: no ``projected_G2.Q1`` claim is emitted, and
    ``_select_principal_name`` falls back to the discovery-feed donor. This confirms
    the gate still excludes refuted claims (T19.3's rule survives T28)."""
    claims = wave_minus_1(
        [_DONOR_SOURCE],
        canonical_name="TULLMAN INVESTMENT MANAGEMENT",
        layer1_claims=[_layer1("contradicted")],
    )

    assert _projected_principal(claims) is None
    # The donor is the only principal_name candidate left, so it wins — proving the
    # researched claim did NOT project (the contrast with case 1).
    assert _select_principal_name(claims) == "TULL, CAYLEY"


# --- the rest of the status space ------------------------------------------

@pytest.mark.parametrize("status", ["verified"])
def test_verified_status_projects(status):
    """T28.2 case 3a — ``verified`` (cross-class corroborated) is validated-good and
    strictly stronger than raw ``confirmed``, so it MUST project. The researched
    principal wins over the donor."""
    claims = wave_minus_1(
        [_DONOR_SOURCE],
        canonical_name="TULLMAN INVESTMENT MANAGEMENT",
        layer1_claims=[_layer1(status)],
    )
    assert _projected_principal(claims) is not None
    assert _select_principal_name(claims) == "Glen Tullman"


@pytest.mark.parametrize(
    "status",
    ["superseded", "removed_failed_validation", "pattern_inferred", "format_only"],
)
def test_non_projectable_statuses_do_not_project(status):
    """T28.2 case 3b — the statuses that must STAY excluded. ``superseded`` and
    ``removed_failed_validation`` are dead evidence; ``pattern_inferred`` and
    ``format_only`` are weak pattern guesses the validation tier downgrades a claim to
    when no source actually bore it out. None of them project; the donor wins."""
    claims = wave_minus_1(
        [_DONOR_SOURCE],
        canonical_name="TULLMAN INVESTMENT MANAGEMENT",
        layer1_claims=[_layer1(status)],
    )
    assert _projected_principal(claims) is None
    assert _select_principal_name(claims) == "TULL, CAYLEY"


def test_projectable_statuses_is_exhaustive_at_the_unit_level():
    """A direct unit-level guard that the ``_PROJECTABLE_STATUSES`` set is exactly the
    three validated-good statuses and nothing else slipped in — so a future edit that
    narrows it back to ``{"confirmed"}`` (the T19.3 regression) or widens it to include
    a weak status is caught here, at the contract rather than only through behaviour."""
    from app.enrichment import _PROJECTABLE_STATUSES

    assert _PROJECTABLE_STATUSES == frozenset({"confirmed", "single_source", "verified"})
    # `confirmed` is still projectable (T19.3's original behaviour preserved).
    assert _project_question_claims([_layer1("confirmed")])
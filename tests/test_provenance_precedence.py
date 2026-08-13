"""T26 — rank multi-valued fields by provenance instead of concatenating everything.

Regression corpus built from the three FEC-employer rows that reached a deliverable on
2026-08-13, where a researcher-verified `projected_G2.Q1` principal was concatenated in the
same cell as up to eight `derived_fec_employer` campaign donors. After T26, multi-valued
fields (app.validation._MULTI_VALUED_FIELDS == {principal_name, principal_title}) emit
ONLY the best (lowest) provenance tier present for that field.

These all fail against pre-T26 HEAD (which joined every eligible claim with "; " in
first-seen order) and pass after T26.1-T26.2.
"""
from __future__ import annotations

from app.dataset import _provenance_rank, _records_rows, build_candidate


def _cand(claims):
    entity = {"entity_id": "e1", "canonical_name": "Acme FO"}
    return build_candidate(entity, claims, "ship", "SFO")


def _claim(field_name, answer, *, status="confirmed", extraction_method=None,
           source_class="fec_employer", confidence="high"):
    return {
        "field_name": field_name,
        "answer": answer,
        "status": status,
        "extraction_method": extraction_method,
        "source_class": source_class,
        "confidence": confidence,
    }


# --- T26.1: the tier helper directly ----------------------------------------

def test_provenance_rank_tiers():
    assert _provenance_rank("projected_G2.Q1") == 0
    assert _provenance_rank("derived_fec_employer") == 1
    assert _provenance_rank("serper_xray") == 2
    assert _provenance_rank("serper_organic") == 2
    assert _provenance_rank("jsonld") == 2
    assert _provenance_rank("site_scrape") == 2
    assert _provenance_rank("something_unrecognised") == 2
    assert _provenance_rank(None) == 2


def test_provenance_rank_longest_prefix_match():
    # "projected_" must beat "derived_" for a method matching both prefixes — but no
    # real method matches both; what this actually guards is that a longer projected_
    # prefix (e.g. a future "projected_G3.Q1") still resolves to tier 0 over a shorter
    # unrelated prefix.
    assert _provenance_rank("projected_G2.Q1") == 0
    assert _provenance_rank("derived_13f_filing") == 1


# --- T26.2: the three shipped FEC rows --------------------------------------

def test_tullman_name_keeps_only_projected_principal():
    """2 derived_fec_employer donors (one a spelling variant of the other) + 1
    projected_G2.Q1 researcher answer -> exactly 'Glen Tullman'."""
    cand = _cand([
        _claim("principal_name", "TULL, CAYLEY",
               extraction_method="derived_fec_employer"),
        _claim("principal_name", "TULLMAN, CAYLEY ELYSE",
               extraction_method="derived_fec_employer"),
        _claim("principal_name", "Glen Tullman",
               extraction_method="projected_G2.Q1", source_class="research"),
    ])
    _, rows = _records_rows([cand])
    assert rows[0]["principal_name"] == "Glen Tullman"


def test_we_family_name_keeps_only_projected_principal():
    """1 derived_fec_employer donor + 1 projected_G2.Q1 -> exactly 'Santiago Ulloa'."""
    cand = _cand([
        _claim("principal_name", "ORTEGA, ROCIO",
               extraction_method="derived_fec_employer"),
        _claim("principal_name", "Santiago Ulloa",
               extraction_method="projected_G2.Q1", source_class="research"),
    ])
    _, rows = _records_rows([cand])
    assert rows[0]["principal_name"] == "Santiago Ulloa"


def test_family_office_name_keeps_only_projected_principal():
    """8 derived_fec_employer donors + 1 projected_G2.Q1 -> exactly 'Sharon McNally'."""
    donors = [
        "HANCOCK, HEATHER", "THORMANN, JOHANNA", "DOHERTY, COLLEEN",
        "DEGAIN, ANDREA", "PARRENT, DAVID", "ALVARADO, ALEJANDRO",
        "FOSMO, SARAH", "HANCOCK, NATHAN",
    ]
    claims = [
        _claim("principal_name", d, extraction_method="derived_fec_employer")
        for d in donors
    ]
    claims.append(_claim("principal_name", "Sharon McNally",
                         extraction_method="projected_G2.Q1", source_class="research"))
    cand = _cand(claims)
    _, rows = _records_rows([cand])
    assert rows[0]["principal_name"] == "Sharon McNally"


def test_two_projected_co_principals_both_survive():
    """The original no-discarding intent must not regress: two genuine co-principals
    arrive in the SAME tier and BOTH are joined with '; '."""
    cand = _cand([
        _claim("principal_name", "Mary C. McNutt",
               extraction_method="projected_G2.Q1", source_class="research"),
        _claim("principal_name", "Michelle J. Blass",
               extraction_method="projected_G2.Q1", source_class="research"),
    ])
    _, rows = _records_rows([cand])
    assert rows[0]["principal_name"] == "Mary C. McNutt; Michelle J. Blass"


def test_serper_xray_only_field_still_emits():
    """When no better tier exists, the lowest tier present is still emitted — nothing
    is dropped just because it is a search snippet."""
    cand = _cand([
        _claim("principal_name", "Mark Berman",
               extraction_method="serper_xray", source_class="search"),
    ])
    _, rows = _records_rows([cand])
    assert rows[0]["principal_name"] == "Mark Berman"


def test_principal_title_follows_same_rule():
    """principal_title is also multi-valued and must prefer the projected tier, so the
    shipped 'PRESIDENT; Glen Tullman' (a donor occupation followed by a name in the
    title column) cannot occur."""
    cand = _cand([
        _claim("principal_title", "PRESIDENT",
               extraction_method="derived_fec_employer"),
        _claim("principal_title", "Glen Tullman",
               extraction_method="projected_G2.Q1", source_class="research"),
    ])
    _, rows = _records_rows([cand])
    assert rows[0]["principal_title"] == "Glen Tullman"


def test_no_better_tier_means_derived_still_emits():
    """Companion to the serper_xray case: when only derived_* claims exist, the derived
    tier is the best present and emits (not dropped because it is not tier 0)."""
    cand = _cand([
        _claim("principal_name", "TULL, CAYLEY",
               extraction_method="derived_fec_employer"),
        _claim("principal_name", "TULLMAN, CAYLEY ELYSE",
               extraction_method="derived_fec_employer"),
    ])
    _, rows = _records_rows([cand])
    assert rows[0]["principal_name"] == "TULL, CAYLEY; TULLMAN, CAYLEY ELYSE"
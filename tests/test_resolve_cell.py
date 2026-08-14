"""T35.3 — the no-drift refactor. `resolve_cell` is the one function that decides what a
cell contains; `_records_rows` calls it, and so does `app.provenance_log.build_field_records`.
The drift test asserts directly that for a fixture carrying every awkward shape (a
multi-valued field with two provenance tiers, a superseded claim, a removed_failed_validation
claim, and a plain single-valued field) `resolve_cell(claims, f).value` equals the cell the
sheet actually ships, for EVERY field.
"""
from __future__ import annotations

from app.dataset import _records_rows, build_candidate, resolve_cell


def _claim(field_name, answer, *, status="confirmed", extraction_method=None,
           source_class="web_page", confidence="high", claim_id=None, created_at=None):
    return {
        "field_name": field_name,
        "answer": answer,
        "status": status,
        "extraction_method": extraction_method,
        "source_class": source_class,
        "confidence": confidence,
        "claim_id": claim_id,
        "created_at": created_at,
    }


def _fixture_claims():
    return [
        # multi-valued field, two provenance tiers — the projected tier wins, the derived
        # donor is the alternative coded lower_provenance_tier. Projected is LAST so it is
        # also the last-write winner (the status/source_class representative).
        _claim("principal_name", "TULL, CAYLEY",
               extraction_method="derived_fec_employer", source_class="fec_employer",
               claim_id="c1", created_at="2026-08-01T00:00:00Z"),
        _claim("principal_name", "Glen Tullman",
               extraction_method="projected_G2.Q1", source_class="research",
               claim_id="c2", created_at="2026-08-02T00:00:00Z"),
        # a superseded claim on a separate field
        _claim("principal_title", "Old Title",
               status="superseded", extraction_method="derived_fec_employer",
               claim_id="c3", created_at="2026-08-01T00:00:00Z"),
        _claim("principal_title", "Managing Director",
               extraction_method="projected_G2.Q3", source_class="research",
               claim_id="c4", created_at="2026-08-02T00:00:00Z"),
        # a removed_failed_validation claim whose value must be blanked
        _claim("principal_email", "bad@acme.com",
               status="removed_failed_validation", source_class="snov",
               claim_id="c5", created_at="2026-08-03T00:00:00Z"),
        # a plain single-valued field (last write wins)
        _claim("aum_usd", 50_000_000,
               status="single_source", source_class="13f_filing",
               claim_id="c6", created_at="2026-08-01T00:00:00Z"),
        _claim("aum_usd", 100_000_000,
               status="single_source", source_class="13f_filing",
               claim_id="c7", created_at="2026-08-02T00:00:00Z"),
    ]


def _candidate(claims):
    entity = {"entity_id": "e1", "canonical_name": "Acme FO"}
    return build_candidate(entity, claims, "ship", "SFO")


def test_resolve_cell_value_matches_shipped_cell_for_every_field():
    """The drift test, done directly: for every field the sheet ships, the value
    `resolve_cell` returns is the value in the corresponding cell of `_records_rows`'s
    output row. If these ever diverge, the provenance log would explain a value the sheet
    does not contain — the T19 vocabulary split waiting to happen again."""
    claims = _fixture_claims()
    cand = _candidate(claims)
    _columns, rows = _records_rows([cand])
    row = rows[0]
    for f in (c["field_name"] for c in claims if c.get("field_name")):
        res = resolve_cell(claims, f)
        assert res.value == row[f], f"drift on {f!r}: resolve_cell={res.value!r} sheet={row[f]!r}"


def test_resolve_cell_single_valued_last_write_wins():
    claims = _fixture_claims()
    res = resolve_cell(claims, "aum_usd")
    assert res.value == 100_000_000  # last write
    assert res.multi_values is None
    assert res.winner is not None
    assert res.winner["claim_id"] == "c7"
    # the earlier aum_usd claim lost to a later write
    codes = [code for _cl, code in res.alternatives]
    assert "not_latest_write" in codes


def test_resolve_cell_multi_valued_keeps_only_best_tier():
    claims = _fixture_claims()
    res = resolve_cell(claims, "principal_name")
    assert res.value == "Glen Tullman"
    assert res.multi_values == ["Glen Tullman"]
    # the derived_fec_employer donor is the single alternative, coded lower_provenance_tier
    assert len(res.alternatives) == 1
    alt_cl, code = res.alternatives[0]
    assert code == "lower_provenance_tier"
    assert alt_cl["claim_id"] == "c1"


def test_resolve_cell_superseded_is_an_alternative():
    claims = _fixture_claims()
    res = resolve_cell(claims, "principal_title")
    codes = {code for _cl, code in res.alternatives}
    assert "superseded" in codes


def test_resolve_cell_removed_failed_validation_blanks_value():
    claims = _fixture_claims()
    res = resolve_cell(claims, "principal_email")
    assert res.value is None
    # the winner is still the removed_failed_validation claim (it is reported as such,
    # not silently dropped — the companion _status column keeps removed_failed_validation)
    assert res.winner is not None
    assert res.winner["status"] == "removed_failed_validation"


def test_resolve_cell_no_claims_returns_blank():
    res = resolve_cell([], "principal_phone")
    assert res.value is None
    assert res.winner is None
    assert res.alternatives == []
    assert res.multi_values is None


def test_alternatives_ordered_deterministically():
    """A web page renders this list — it must not reshuffle between reads."""
    claims = [
        _claim("aum_usd", 1, claim_id="b", created_at="2026-08-02T00:00:00Z",
               source_class="13f_filing"),
        _claim("aum_usd", 2, claim_id="a", created_at="2026-08-01T00:00:00Z",
               source_class="13f_filing"),
        _claim("aum_usd", 3, claim_id="c", created_at="2026-08-03T00:00:00Z",
               source_class="13f_filing"),
    ]
    res = resolve_cell(claims, "aum_usd")
    # winner is the last write (claim_id c); alternatives ordered by created_at then claim_id
    alt_ids = [cl["claim_id"] for cl, _code in res.alternatives]
    assert alt_ids == ["a", "b"]

def test_a_valueless_claim_is_not_reported_as_a_rejected_alternative():
    """An alternative is a VALUE that lost. A claim with no answer lost nothing — it is
    the record of a lookup that came back empty, and the cell's blank_reason tells that
    story. Found by looking at the rendered Log tab, where such a claim showed as
    'rejected: null — not_latest_write'."""
    from app.dataset import resolve_cell
    claims = [
        dict(claim_id="c1", field_name="principal_phone", answer=None,
             status="could_not_verify", source_class="tool_unavailable",
             extraction_method="snov_error", confidence="low"),
    ]
    res = resolve_cell(claims, "principal_phone")
    assert res.value is None
    assert res.producers == []
    assert res.alternatives == []
    # ...while a claim that DID carry a value and lost is still reported.
    claims.insert(0, dict(claim_id="c0", field_name="principal_phone", answer="+1 555 0100",
                          status="confirmed", source_class="site_scrape",
                          extraction_method="jsonld", confidence="medium"))
    res = resolve_cell(claims, "principal_phone")
    assert [(c["claim_id"], code) for c, code in res.alternatives] == [("c0", "not_latest_write")]

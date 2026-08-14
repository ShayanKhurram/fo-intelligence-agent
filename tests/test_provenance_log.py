"""T35.4 — app.provenance_log.build_field_records / build_run_log (PLAN.md T35).

Offline, over a seeded tmp DB. The fixture entity carries every awkward shape the log
must distinguish:
  (a) a derived_13f aum_usd            -> a derived summary, shipped
  (b) a projected_G2.Q1 principal_name that beats a derived_fec_employer one
                                       -> exactly one alternative coded lower_provenance_tier
  (c) a removed_failed_validation principal_email with a matching audit_rejected_values row
                                       -> blank_reason.code = removed_failed_validation
  (d) a principal_phone whose only claim has source_class="tool_unavailable"
                                       -> blank_reason.code = tool_unavailable
  (e) an important_insight with no claim at all
                                       -> blank_reason.code = never_attempted

Every record must round-trip through json.dumps with no custom encoder.
"""
from __future__ import annotations

import json

from app.db import (
    connection,
    upsert_claims,
    upsert_entity,
    write_audit_rejected_value,
    write_tool_call,
)
from app.provenance_log import build_field_records, build_run_log


def _seed(db_path) -> str:
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Acme Capital Partners")
        upsert_claims(conn, "e1", [
            # (a) derived_13f aum_usd — shipped
            {"field_name": "aum_usd", "answer": 100_000_000, "status": "single_source",
             "source_class": "13f_filing", "extraction_method": "derived_13f",
             "confidence": "high", "produced_by": "derived", "wave": "-1",
             "claim_id": "c-aum", "created_at": "2026-08-01T00:00:00Z",
             "retrieved_at": "2026-08-01T00:00:00Z"},
            # (b) principal_name: a derived_fec_employer donor beaten by a projected_G2.Q1
            # researcher answer (best tier). Projected is LAST so it is the last-write winner.
            {"field_name": "principal_name", "answer": "TULL, CAYLEY", "status": "confirmed",
             "source_class": "fec_employer", "extraction_method": "derived_fec_employer",
             "confidence": "high", "produced_by": "derived", "wave": "-1",
             "claim_id": "c-donor", "created_at": "2026-08-01T00:00:00Z",
             "source_url": "https://fec.gov/donor", "retrieved_at": "2026-08-01T00:00:00Z"},
            {"field_name": "principal_name", "answer": "Glen Tullman", "status": "confirmed",
             "source_class": "research", "extraction_method": "projected_G2.Q1",
             "confidence": "high", "produced_by": "research", "wave": "1",
             "claim_id": "c-proj", "created_at": "2026-08-02T00:00:00Z",
             "source_url": "https://classvi.com/team", "retrieved_at": "2026-08-02T00:00:00Z"},
            # (c) principal_email: removed_failed_validation, with a matching audit row
            {"field_name": "principal_email", "answer": "bad@acme.com",
             "status": "removed_failed_validation", "source_class": "snov",
             "extraction_method": "snov_emails_by_name_domain",
             "confidence": "low", "produced_by": "enrichment", "wave": "1",
             "claim_id": "c-email", "created_at": "2026-08-03T00:00:00Z",
             "source_url": "https://snov.io/x", "retrieved_at": "2026-08-03T00:00:00Z"},
            # (d) principal_phone: only claim is tool_unavailable
            {"field_name": "principal_phone", "answer": None, "status": "could_not_verify",
             "source_class": "tool_unavailable", "extraction_method": "snov_error",
             "confidence": "low", "produced_by": "enrichment", "wave": "1",
             "claim_id": "c-phone", "created_at": "2026-08-03T00:00:00Z",
             "retrieved_at": "2026-08-03T00:00:00Z"},
        ])
        write_audit_rejected_value(conn, "e1", "principal_email", "bad@acme.com",
                                   "email_domain_guard", evidence_url="https://snov.io/x")
    return "e1"


def _record_by_field(records, field):
    return next(r for r in records if r["field"] == field)


def test_aum_usd_derived_summary_and_shipped(db_path):
    _seed(db_path)
    with connection(db_path) as conn:
        recs = build_field_records(conn, "e1", run_id="r1")
    rec = _record_by_field(recs, "aum_usd")
    assert rec["shipped"] is True
    assert rec["value"] == 100_000_000
    assert rec["how"]["extraction_method"] == "derived_13f"
    assert "13F" in rec["how"]["summary"]
    assert rec["blank_reason"] is None


def test_principal_name_one_alternative_lower_provenance_tier(db_path):
    _seed(db_path)
    with connection(db_path) as conn:
        recs = build_field_records(conn, "e1", run_id="r1")
    rec = _record_by_field(recs, "principal_name")
    assert rec["value"] == "Glen Tullman"
    assert rec["how"]["extraction_method"] == "projected_G2.Q1"
    assert rec["how"]["summary"].startswith("Researched in Layer 1 as question G2.Q1")
    assert len(rec["alternatives"]) == 1
    alt = rec["alternatives"][0]
    assert alt["why_not_used"] == "lower_provenance_tier"
    assert alt["value"] == "TULL, CAYLEY"
    assert alt["extraction_method"] == "derived_fec_employer"


def test_principal_email_removed_failed_validation_blank_reason(db_path):
    _seed(db_path)
    with connection(db_path) as conn:
        recs = build_field_records(conn, "e1", run_id="r1")
    rec = _record_by_field(recs, "principal_email")
    assert rec["value"] is None
    assert rec["shipped"] is False
    assert rec["status"] == "removed_failed_validation"
    assert rec["blank_reason"]["code"] == "removed_failed_validation"
    assert rec["blank_reason"]["rejected_value"] == "bad@acme.com"
    assert rec["blank_reason"]["reason_code"] == "email_domain_guard"


def test_principal_phone_tool_unavailable_blank_reason(db_path):
    _seed(db_path)
    with connection(db_path) as conn:
        recs = build_field_records(conn, "e1", run_id="r1")
    rec = _record_by_field(recs, "principal_phone")
    assert rec["value"] is None
    assert rec["blank_reason"]["code"] == "tool_unavailable"


def test_important_insight_never_attempted(db_path):
    _seed(db_path)
    with connection(db_path) as conn:
        recs = build_field_records(conn, "e1", run_id="r1")
    rec = _record_by_field(recs, "important_insight")
    assert rec["value"] is None
    assert rec["status"] == "could_not_verify"
    assert rec["blank_reason"]["code"] == "never_attempted"
    assert rec["how"]["extraction_method"] is None


def test_tool_call_matched_by_url_shows_under_the_right_field(db_path):
    _seed(db_path)
    # a tool call whose result_url matches the projected principal_name winner's source_url
    with connection(db_path) as conn:
        write_tool_call(
            conn,
            run_id="r1", entity_id="e1", wave="1", tool="fetch_raw_html",
            args='{"url": "https://classvi.com/team"}', ok=1, error=None,
            result_summary="1234 chars", result_url="https://classvi.com/team",
            cache_hit=0, duration_ms=42,
        )
        recs = build_field_records(conn, "e1", run_id="r1")
    rec = _record_by_field(recs, "principal_name")
    assert rec["tool_calls"], "expected at least one matched tool call"
    tc = rec["tool_calls"][0]
    assert tc["matched_by"] == "url"
    assert tc["tool"] == "fetch_raw_html"
    # a different field must not pick up this call
    phone = _record_by_field(recs, "principal_phone")
    assert phone["tool_calls"] == []


def test_every_record_round_trips_json_dumps(db_path):
    _seed(db_path)
    with connection(db_path) as conn:
        recs = build_field_records(conn, "e1", run_id="r1")
    for r in recs:
        # no custom encoder — must survive a plain json.dumps
        json.dumps(r)


def test_build_run_log_json_dumps_and_shape(db_path):
    _seed(db_path)
    with connection(db_path) as conn:
        from app.db import start_run, finish_run
        start_run(conn, "enrichment", run_id="r1", entity_count=1)
        finish_run(conn, "r1", status="done")
        doc = build_run_log(conn, "r1", [("e1", "ship")])
    assert doc["schema_version"] == 1
    assert doc["run"]["run_id"] == "r1"
    assert len(doc["leads"]) == 1
    lead = doc["leads"][0]
    assert lead["entity_id"] == "e1"
    assert lead["canonical_name"] == "Acme Capital Partners"
    assert lead["outcome"] == "ship"
    assert lead["fields"]
    # the whole document round-trips
    json.dumps(doc)


def test_build_run_log_skips_unknown_entity(db_path):
    _seed(db_path)
    with connection(db_path) as conn:
        from app.db import start_run
        start_run(conn, "enrichment", run_id="r1", entity_count=1)
        doc = build_run_log(conn, "r1", ["e1", "does-not-exist"])
    assert [l["entity_id"] for l in doc["leads"]] == ["e1"]


def test_zero_claims_entity_produces_high_value_records(db_path):
    with connection(db_path) as conn:
        upsert_entity(conn, "empty", "Empty Co")
        from app.db import start_run
        start_run(conn, "enrichment", run_id="r0", entity_count=1)
        recs = build_field_records(conn, "empty", run_id="r0")
    fields = {r["field"] for r in recs}
    # every high-value field appears, all never_attempted
    from app.dataset import _HIGH_VALUE_FIELDS
    assert _HIGH_VALUE_FIELDS <= fields
    for r in recs:
        assert r["value"] is None
        assert r["blank_reason"]["code"] == "never_attempted"

# --- D1: the log must attribute the shipped value to the producing claim, not the
#     last-write winner (the single failure mode this feature exists to prevent) ----

def test_d1_multi_valued_value_attributed_to_producer_not_last_write(db_path):
    """The exact scenario _provenance_rank exists for (T26): a projected_G2.Q1
    principal_name (tier 0) written FIRST, and a derived_fec_employer principal_name
    (tier 1, a campaign donor) written LAST. The cell ships the projected value; the
    row's _status/_source_class companion columns reflect the LAST write (the FEC
    claim) and must NOT change. But the provenance log's `how` must attribute the
    shipped value to the producing (projected) claim, and the FEC claim must appear as
    an alternative coded lower_provenance_tier -- not vanish, and not be credited as
    the value's source."""
    claims = [
        dict(claim_id="c1", question_id="G2.Q1", field_name="principal_name",
             answer="Matt Blackburn", status="confirmed",
             source_url="https://acme.com/team", source_class="site_scrape",
             extraction_method="projected_G2.Q1", confidence="high",
             produced_by="research", wave=None, created_at="2026-08-01T00:00:00Z",
             retrieved_at="2026-08-01T00:00:00Z"),
        dict(claim_id="c2", question_id=None, field_name="principal_name",
             answer="Jane Donor", status="confirmed",
             source_url="https://fec.gov/x", source_class="fec_employer",
             extraction_method="derived_fec_employer", confidence="low",
             produced_by="derived", wave="-1", created_at="2026-08-02T00:00:00Z",
             retrieved_at="2026-08-02T00:00:00Z"),
    ]
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Acme FO")
        upsert_claims(conn, "e1", claims)
        recs = build_field_records(conn, "e1", run_id="r1")
    rec = _record_by_field(recs, "principal_name")

    # the log attributes the value to the producing (projected) claim
    assert rec["value"] == "Matt Blackburn"
    assert rec["how"]["extraction_method"] == "projected_G2.Q1"
    assert rec["how"]["source_url"] == "https://acme.com/team"
    assert rec["how"]["summary"].startswith("Researched in Layer 1 as question G2.Q1")
    assert rec["confidence"] == "high"

    # the FEC claim is the single alternative, coded lower_provenance_tier
    assert len(rec["alternatives"]) == 1
    alt = rec["alternatives"][0]
    assert alt["why_not_used"] == "lower_provenance_tier"
    assert alt["extraction_method"] == "derived_fec_employer"
    assert alt["value"] == "Jane Donor"


def test_row_companion_columns_describe_the_producing_claim(db_path):
    """T35.7: the sheet's _status/_source_class companion columns describe the claim that
    PRODUCED the value, not whichever claim was written last.

    This test previously pinned the opposite (`fec_employer`) while T35.3's refactor was
    in flight, because that refactor was required not to change the emitted sheet by one
    byte. T35.7 changes it deliberately: a row shipping the researcher's "Matt Blackburn"
    labelled with a campaign-donor claim's source class is a wrong label on a right value,
    in the deliverable itself."""
    from app.dataset import _records_rows, build_candidate
    claims = [
        dict(claim_id="c1", field_name="principal_name", answer="Matt Blackburn",
             status="confirmed", source_url="https://acme.com/team",
             source_class="site_scrape", extraction_method="projected_G2.Q1",
             confidence="high"),
        dict(claim_id="c2", field_name="principal_name", answer="Jane Donor",
             status="confirmed", source_url="https://fec.gov/x",
             source_class="fec_employer", extraction_method="derived_fec_employer",
             confidence="low"),
    ]
    cand = build_candidate({"entity_id": "e1", "canonical_name": "Acme FO"}, claims,
                           "ship", "SFO")
    _cols, rows = _records_rows([cand])
    row = rows[0]
    assert row["principal_name"] == "Matt Blackburn"
    assert row["principal_name_status"] == "confirmed"
    # the companion source_class names where "Matt Blackburn" actually came from
    assert row["principal_name_source_class"] == "site_scrape"


def test_blanked_cell_companion_columns_fall_back_to_the_winner(db_path):
    """T35.7's fallback half: a cell with no producer (the release rule killed the value)
    still reports the killed claim's status and source_class. Reporting
    `could_not_verify`/None there would erase the distinction between "we looked and the
    value failed validation" and "we never found one" — the exact distinction the
    provenance log exists to preserve."""
    from app.dataset import _records_rows, build_candidate
    claims = [
        dict(claim_id="c1", field_name="principal_email", answer="bob@acme.com",
             status="removed_failed_validation", source_url="https://acme.com",
             source_class="snov", extraction_method="snov_domain_search",
             confidence="low"),
    ]
    cand = build_candidate({"entity_id": "e1", "canonical_name": "Acme FO"}, claims,
                           "ship", "SFO")
    _cols, rows = _records_rows([cand])
    row = rows[0]
    assert row["principal_email"] is None
    assert row["principal_email_status"] == "removed_failed_validation"
    assert row["principal_email_source_class"] == "snov"


def test_d1_also_produced_by_for_two_co_principals(db_path):
    """A multi-valued cell with two producers (two co-principals in the same best tier)
    attributes the first via `how` and the second via how.also_produced_by."""
    claims = [
        dict(claim_id="c1", field_name="principal_name", answer="Mary C. McNutt",
             status="confirmed", source_url="https://acme.com/a",
             source_class="research", extraction_method="projected_G2.Q1",
             confidence="high", created_at="2026-08-01T00:00:00Z"),
        dict(claim_id="c2", field_name="principal_name", answer="Michelle J. Blass",
             status="confirmed", source_url="https://acme.com/b",
             source_class="research", extraction_method="projected_G2.Q1",
             confidence="high", created_at="2026-08-02T00:00:00Z"),
    ]
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Acme FO")
        upsert_claims(conn, "e1", claims)
        recs = build_field_records(conn, "e1", run_id="r1")
    rec = _record_by_field(recs, "principal_name")
    assert rec["value"] == "Mary C. McNutt; Michelle J. Blass"
    assert rec["how"]["source_url"] == "https://acme.com/a"
    assert len(rec["how"]["also_produced_by"]) == 1
    assert rec["how"]["also_produced_by"][0]["source_url"] == "https://acme.com/b"


# --- D2: a duplicate answer in the winning tier is corroboration, not a rejection -----

def test_d2_duplicate_answer_in_winning_tier_is_duplicate_value(db_path):
    """Two claims in the SAME (best) tier carrying the SAME answer are corroboration,
    not a rejection. The duplicate is coded `duplicate_value`, not `lower_provenance_tier`."""
    claims = [
        dict(claim_id="c1", field_name="principal_name", answer="Matt Blackburn",
             status="confirmed", source_class="research",
             extraction_method="projected_G2.Q1", confidence="high",
             created_at="2026-08-01T00:00:00Z"),
        dict(claim_id="c2", field_name="principal_name", answer="Matt Blackburn",
             status="confirmed", source_class="web_page",
             extraction_method="projected_G2.Q1", confidence="high",
             created_at="2026-08-02T00:00:00Z"),
    ]
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Acme FO")
        upsert_claims(conn, "e1", claims)
        recs = build_field_records(conn, "e1", run_id="r1")
    rec = _record_by_field(recs, "principal_name")
    assert rec["value"] == "Matt Blackburn"
    codes = [a["why_not_used"] for a in rec["alternatives"]]
    assert "duplicate_value" in codes
    assert "lower_provenance_tier" not in codes


# --- D3: "never attempted" vs "searched_not_found" when tool calls ran ---------------

def test_d3_no_claim_no_tool_calls_is_never_attempted(db_path):
    """A field with no claim and no tool calls in the run is genuinely never_attempted."""
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Acme FO")
        upsert_claims(conn, "e1", [
            {"field_name": "aum_usd", "answer": 100, "status": "single_source",
             "source_class": "13f_filing", "extraction_method": "derived_13f",
             "confidence": "high", "claim_id": "c1", "created_at": "2026-08-01T00:00:00Z"},
        ])
        from app.db import start_run
        start_run(conn, "enrichment", run_id="r1", entity_count=1)
        recs = build_field_records(conn, "e1", run_id="r1")
    rec = _record_by_field(recs, "important_insight")
    assert rec["value"] is None
    assert rec["blank_reason"]["code"] == "never_attempted"


def test_d3_no_claim_but_tool_calls_ran_is_searched_not_found(db_path):
    """A field with no claim and no matched tool call, but where the run DID make tool
    calls for this lead, is searched_not_found -- not never_attempted. Saying nothing
    was attempted when calls did run is the false statement this log must not produce."""
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Acme FO")
        upsert_claims(conn, "e1", [
            {"field_name": "aum_usd", "answer": 100, "status": "single_source",
             "source_class": "13f_filing", "extraction_method": "derived_13f",
             "confidence": "high", "claim_id": "c1", "created_at": "2026-08-01T00:00:00Z"},
        ])
        from app.db import start_run, write_tool_call
        start_run(conn, "enrichment", run_id="r1", entity_count=1)
        # a tool call ran for this lead in this run, not attributable to important_insight
        write_tool_call(conn, run_id="r1", entity_id="e1", wave="1",
                        tool="serper_search_raw", args='{"query": "acme"}',
                        ok=1, result_summary="3 results", result_url=None,
                        cache_hit=0, duration_ms=10)
        recs = build_field_records(conn, "e1", run_id="r1")
    rec = _record_by_field(recs, "important_insight")
    assert rec["value"] is None
    assert rec["blank_reason"]["code"] == "searched_not_found"
    assert "1 tool call" in rec["blank_reason"]["detail"]
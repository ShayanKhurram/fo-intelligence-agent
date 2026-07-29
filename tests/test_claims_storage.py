"""claims table CRUD + backfill (enrichment_validation_dataset_plan.md §2/§7/§8 step 1)."""
from __future__ import annotations

from app.db import (
    backfill_claims_from_decisions,
    connection,
    get_claims,
    upsert_claim,
    upsert_claims,
    upsert_entity,
    write_decision,
    write_rejection,
)
from app.state import Claim


def test_upsert_claim_roundtrips_all_fields(db_path):
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Test Co")
        claim = Claim(
            field_name="aum_usd",
            answer=1_000_000,
            status="confirmed",
            confidence="high",
            produced_by="derived",
            wave="-1",
            source_class="13f_filing",
            extraction_method="derived_13f",
        )
        upsert_claim(conn, "e1", claim.model_dump(mode="json"))

    with connection(db_path) as conn:
        rows = get_claims(conn, "e1")
    assert len(rows) == 1
    row = rows[0]
    assert row["field_name"] == "aum_usd"
    assert row["answer"] == 1_000_000
    assert row["produced_by"] == "derived"
    assert row["wave"] == "-1"
    assert row["extraction_method"] == "derived_13f"


def test_upsert_claim_updates_in_place_on_same_claim_id(db_path):
    """Validation annotates existing claims (never rewrites their value) — this must be
    an UPDATE keyed on claim_id, not a second row."""
    claim = Claim(field_name="principal_email", answer="a@b.com", status="confirmed", confidence="medium")
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Test Co")
        upsert_claim(conn, "e1", claim.model_dump(mode="json"))

    annotated = claim.model_copy(update={"status": "verified", "verification_method": "cross_class"})
    with connection(db_path) as conn:
        upsert_claim(conn, "e1", annotated.model_dump(mode="json"))

    with connection(db_path) as conn:
        rows = get_claims(conn, "e1")
    assert len(rows) == 1
    assert rows[0]["status"] == "verified"
    assert rows[0]["verification_method"] == "cross_class"
    assert rows[0]["answer"] == "a@b.com"  # value itself untouched by annotation


def test_upsert_claims_batch(db_path):
    claims = [
        Claim(field_name="f1", answer="v1", status="confirmed", confidence="high").model_dump(mode="json"),
        Claim(field_name="f2", answer="v2", status="confirmed", confidence="high").model_dump(mode="json"),
    ]
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Test Co")
        upsert_claims(conn, "e1", claims)
    with connection(db_path) as conn:
        rows = get_claims(conn, "e1")
    assert {r["field_name"] for r in rows} == {"f1", "f2"}


def test_backfill_from_decisions_and_rejections_is_idempotent(db_path):
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Pursued Co")
        upsert_entity(conn, "e2", "Rejected Co")
        write_decision(
            conn, "e1", verdict="pursue", rationale="r", gate_results={},
            claim_ledger=[
                {"question_id": "G1.Q1", "answer": "yes", "status": "confirmed",
                 "source_url": "http://x", "source_class": "web", "confidence": "high"}
            ],
            dead_ends=[],
        )
        write_rejection(
            conn, "e2", reason_code="G1.Q1:unanswered", gate_results={},
            claim_ledger=[
                {"question_id": "G1.Q1", "answer": "insufficient information", "status": "could_not_verify",
                 "source_url": None, "source_class": None, "confidence": "low"}
            ],
        )

    with connection(db_path) as conn:
        written_first = backfill_claims_from_decisions(conn)
    with connection(db_path) as conn:
        written_second = backfill_claims_from_decisions(conn)
    assert written_first == 2
    assert written_second == 2  # re-run touches the same 2 rows, doesn't duplicate

    with connection(db_path) as conn:
        e1_claims = get_claims(conn, "e1")
        e2_claims = get_claims(conn, "e2")
    assert len(e1_claims) == 1
    assert len(e2_claims) == 1
    assert e1_claims[0]["produced_by"] == "research"  # source_class="web" -> not a parser class

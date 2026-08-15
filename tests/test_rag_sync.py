"""T36.3 — automatic micro-RAG ingestion for confirmed leads.

Offline by construction: no Postgres, no embedding model. What is under test is the
*queue* and the *degradation* — that confirming a lead always records the intent, and
that every way the RAG side can be unavailable leaves the work pending rather than
losing it or failing the run.
"""
from __future__ import annotations

import app.rag_sync as rag_mod
import app.scheduler as sched_mod
from app.db import add_entity_source, connection, upsert_entity
from app.rag_sync import (
    MAX_ATTEMPTS,
    drain_queue,
    enqueue_entity,
    is_confirmed,
    pending_entities,
    queue_counts,
)
from app.scheduler import run_scheduled_job


def _row(conn, entity_id):
    return dict(conn.execute("SELECT * FROM rag_queue WHERE entity_id = ?", (entity_id,)).fetchone())


def test_only_shipped_outcomes_count_as_confirmed():
    assert is_confirmed("ship") and is_confirmed("ship_with_caveats")
    assert not is_confirmed("reject_thin")
    assert not is_confirmed(None)


def test_enqueue_is_idempotent_and_reconfirm_resets_to_pending(db_path):
    with connection(db_path) as conn:
        enqueue_entity(conn, "e1", run_id="r1")
        enqueue_entity(conn, "e1", run_id="r1")
        assert len(pending_entities(conn)) == 1
        # mark it ingested, then re-confirm it in a later run
        conn.execute("UPDATE rag_queue SET status='done', attempts=1 WHERE entity_id='e1'")
        enqueue_entity(conn, "e1", run_id="r2")
        row = _row(conn, "e1")
    # A lead re-confirmed later must be re-ingested: the ledger has moved on, and the RAG
    # upserts by record_id, so this is a correction rather than a duplicate.
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["run_id"] == "r2"


def test_enqueue_never_raises_even_on_a_broken_connection(db_path):
    """A queue write is in the run's critical path. It must not be able to fail a lead."""
    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("db gone")

    enqueue_entity(Broken(), "e1")   # must not raise


def test_drain_without_database_url_leaves_rows_pending(db_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    with connection(db_path) as conn:
        enqueue_entity(conn, "e1")

    result = drain_queue(db_path)

    assert result["status"] == "skipped"
    assert result["reason"] == "no DATABASE_URL"
    with connection(db_path) as conn:
        # Nothing lost: the next drain picks it up.
        assert [r["entity_id"] for r in pending_entities(conn)] == ["e1"]


def test_drain_on_empty_queue_is_a_no_op(db_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert drain_queue(db_path) == {"status": "ok", "ingested": 0, "failed": 0, "pending": 0}


def test_drain_skips_when_the_ingest_module_is_unavailable(db_path, monkeypatch):
    """psycopg2/torch absent is the normal state in CI and in a slim deployment — a
    skipped drain, not a crash."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@localhost/none")
    monkeypatch.setattr(rag_mod, "_load_ingest_module", lambda: None)
    with connection(db_path) as conn:
        enqueue_entity(conn, "e1")

    result = drain_queue(db_path)

    assert result["status"] == "skipped"
    assert result["reason"] == "ingest module unavailable"
    with connection(db_path) as conn:
        assert len(pending_entities(conn)) == 1


def test_a_lead_no_longer_confirmed_is_marked_stale_not_ingested(db_path, monkeypatch):
    """Re-judging is exactly why the batch job takes each entity's LATEST run only. A lead
    queued when it shipped, then re-rejected before the drain, must not be published."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@localhost/none")
    written = {}

    class FakeIngest:
        @staticmethod
        def _hq_state(conn, entity_id):
            return "CA"

        @staticmethod
        def build_record(entity_id, name, outcome, claims, hq_state=None):
            return {"record_id": entity_id, "entity_name": name, "outcome": outcome}

        @staticmethod
        def write_to_postgres(dsn, records, provenance, *, prune=True):
            written["records"] = records
            written["prune"] = prune
            return "hash-1"

    monkeypatch.setattr(rag_mod, "_load_ingest_module", lambda: FakeIngest)
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Acme FO")
        conn.execute("INSERT INTO enrichment_runs (entity_id, wave, outcome) VALUES ('e1','2','reject_thin')")
        enqueue_entity(conn, "e1")

    result = drain_queue(db_path)

    assert result["ingested"] == 0
    assert result["stale"] == 1
    assert "records" not in written
    with connection(db_path) as conn:
        assert _row(conn, "e1")["status"] == "stale"


def test_a_confirmed_lead_is_ingested_and_marked_done(db_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@localhost/none")
    written = {}

    class FakeIngest:
        @staticmethod
        def _hq_state(conn, entity_id):
            return "CA"

        @staticmethod
        def build_record(entity_id, name, outcome, claims, hq_state=None):
            return {"record_id": entity_id, "entity_name": name, "outcome": outcome}

        @staticmethod
        def write_to_postgres(dsn, records, provenance, *, prune=True):
            written["records"] = records
            written["prune"] = prune
            return "hash-1"

    monkeypatch.setattr(rag_mod, "_load_ingest_module", lambda: FakeIngest)
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Acme FO")
        conn.execute("INSERT INTO enrichment_runs (entity_id, wave, outcome) VALUES ('e1','2','ship')")
        enqueue_entity(conn, "e1")

    result = drain_queue(db_path)

    assert result["status"] == "ok" and result["ingested"] == 1
    assert result["build_hash"] == "hash-1"
    assert [r["record_id"] for r in written["records"]] == ["e1"]
    with connection(db_path) as conn:
        assert _row(conn, "e1")["status"] == "done"
        assert queue_counts(conn) == {"done": 1}


def test_a_failing_postgres_write_keeps_the_row_pending(db_path, monkeypatch):
    """The RAG being down delays ingestion; it never drops a lead and never raises."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@localhost/none")

    class FakeIngest:
        @staticmethod
        def _hq_state(conn, entity_id):
            return None

        @staticmethod
        def build_record(entity_id, name, outcome, claims, hq_state=None):
            return {"record_id": entity_id, "entity_name": name, "outcome": outcome}

        @staticmethod
        def write_to_postgres(dsn, records, provenance, *, prune=True):
            raise RuntimeError("could not connect to server")

    monkeypatch.setattr(rag_mod, "_load_ingest_module", lambda: FakeIngest)
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Acme FO")
        conn.execute("INSERT INTO enrichment_runs (entity_id, wave, outcome) VALUES ('e1','2','ship')")
        enqueue_entity(conn, "e1")

    result = drain_queue(db_path)     # must not raise

    assert result["status"] == "error"
    with connection(db_path) as conn:
        row = _row(conn, "e1")
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert "could not connect" in row["last_error"]


def test_attempts_are_capped_so_one_poison_row_cannot_block_forever(db_path):
    with connection(db_path) as conn:
        enqueue_entity(conn, "e1")
        conn.execute("UPDATE rag_queue SET attempts = ? WHERE entity_id = 'e1'", (MAX_ATTEMPTS,))
        assert pending_entities(conn) == []


async def test_a_lead_that_confirms_during_a_scheduled_run_is_queued_automatically(db_path, monkeypatch):
    """The end-to-end requirement: confirming a lead during a run enqueues it for
    retrieval with no manual step."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    async def fake_run_lead(entity_id, db_path=None):
        return {"cost_usd": 0.0}

    async def fake_process_entity(conn, entity_id, model, *, force=False):
        outcome = "ship" if entity_id == "e0" else "reject_thin"
        return {"entity_id": entity_id, "outcome": outcome, "calls_spent": 0, "usd_spent": 0.0}

    monkeypatch.setattr(sched_mod, "run_lead", fake_run_lead)
    monkeypatch.setattr(sched_mod, "process_entity", fake_process_entity)
    with connection(db_path) as conn:
        for eid in ("e0", "e1"):
            upsert_entity(conn, eid, f"Firm {eid}")
            add_entity_source(conn, eid, "fec_employer", {})
            conn.execute("INSERT INTO decisions (entity_id, verdict, rationale) VALUES (?, 'pursue', '')", (eid,))

    result = await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": None, "max_leads": None},
        db_path=db_path, model=object(), chunk_size=2,
    )

    assert result["confirmed_ids"] == ["e0"]
    with connection(db_path) as conn:
        queued = [r["entity_id"] for r in pending_entities(conn)]
    # Only the confirmed lead — a rejected one has nothing to publish.
    assert queued == ["e0"]
    # The drain ran at the end of the job and, with no DATABASE_URL, skipped cleanly.
    assert result["rag"]["status"] == "skipped"


def test_no_test_can_reach_a_real_remote_database(monkeypatch):
    """The conftest guard, asserted rather than assumed.

    Without it, any test that runs a scheduled job pushes its throwaway tmp-database rows
    into whatever DATABASE_URL happens to be configured — which is exactly what happened
    once a real Supabase DSN reached .env: ten junk 'scheduled/running' runs landed in the
    production project. The guard is autouse, so this test simply confirms it is in force."""
    import os

    assert os.environ.get("DATABASE_URL") is None
    assert os.environ.get("POSTGRES_URL") is None
    # ...and the sync paths therefore refuse to do anything remote.
    from app.log_sync import sync_runs
    assert sync_runs(limit=1)["reason"] == "no DATABASE_URL"


def test_incremental_drain_never_prunes_the_rest_of_the_corpus(db_path, monkeypatch):
    """The drain must call write_to_postgres with prune=False.

    write_to_postgres's default prune deletes every pipeline record NOT in the batch it
    was handed — correct for the full batch job, which always passes the complete ship
    set, and catastrophic for an incremental drain, which passes only the leads that
    confirmed since last time. With the prune left on, ingesting one new lead would
    delete the other twenty-nine. The failure is silent: the drain reports success while
    the corpus empties."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@localhost/none")
    seen = {}

    class FakeIngest:
        @staticmethod
        def _hq_state(conn, entity_id):
            return None

        @staticmethod
        def build_record(entity_id, name, outcome, claims, hq_state=None):
            return {"record_id": entity_id, "entity_name": name, "outcome": outcome}

        @staticmethod
        def write_to_postgres(dsn, records, provenance, *, prune=True):
            seen["prune"] = prune
            return "hash-1"

    monkeypatch.setattr(rag_mod, "_load_ingest_module", lambda: FakeIngest)
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Acme FO")
        conn.execute("INSERT INTO enrichment_runs (entity_id, wave, outcome) VALUES ('e1','2','ship')")
        enqueue_entity(conn, "e1")

    assert drain_queue(db_path)["ingested"] == 1
    assert seen["prune"] is False, "an incremental drain must not prune the corpus"

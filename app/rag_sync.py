"""T36.3 — automatic micro-RAG ingestion for leads that confirm during a run.

A lead that reaches `ship` / `ship_with_caveats` is, by this project's own definition, a
finished piece of work: validated claims, a provenance log, a row in the deliverable. It
should be retrievable immediately rather than waiting for someone to remember to run
`micro_rag/ingest/ingest.py` by hand — which is exactly what had NOT happened as of
2026-08-14, when `data/foia.db` held 32 shipped entities and the RAG corpus held zero
(PROJECT_LOG.md).

**Queue first, ingest second, and never in the run's critical path.** Confirming a lead
writes one row to a local SQLite `rag_queue` table — a few microseconds, no network, no
imports. Draining that queue is a separate step that talks to Postgres and loads a
384-dim embedding model. That split is the whole design:

- The RAG side is remote (Supabase), free-tier, and auto-pauses. The embedding model
  needs torch. Neither may be allowed to fail a lead's enrichment, and neither is
  even importable in the offline test environment.
- A drain that fails leaves the row `pending`, so the work is not lost — the next drain
  (next scheduled run, or a manual call) picks it up. An outage delays ingestion; it
  never silently drops a lead. This is the same "an outage must not look like an absence"
  rule the enrichment layer already follows.
- `attempts` is capped so one poison record cannot make every future drain fail behind
  it.

Nothing in this module raises into a caller. Every entry point returns a summary dict.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from app.db import connection, get_claims, get_entity

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INGEST_DIR = _REPO_ROOT / "micro_rag" / "ingest"

# A lead is "confirmed" when enrichment ships it. Same two outcomes the existing RAG
# ingest job selects on (micro_rag/ingest/ingest.py:fetch_ship_records) — the definition
# lives in one place conceptually, so keep these in sync if that ever changes.
CONFIRMED_OUTCOMES: frozenset[str] = frozenset({"ship", "ship_with_caveats"})

MAX_ATTEMPTS = 3


def is_confirmed(outcome: str | None) -> bool:
    return outcome in CONFIRMED_OUTCOMES


# ---------------------------------------------------------------------------
# The queue (local SQLite, always available)
# ---------------------------------------------------------------------------


def enqueue_entity(conn: sqlite3.Connection, entity_id: str, run_id: str | None = None) -> None:
    """Mark a confirmed lead for RAG ingestion. Idempotent per entity: a lead confirmed
    again by a later run resets its row to `pending` so the fresher claim ledger is
    re-ingested (the RAG upserts by record_id, so re-ingesting is a correction, not a
    duplicate). Never raises — a queue write must not be able to fail a run."""
    try:
        conn.execute(
            """
            INSERT INTO rag_queue (entity_id, run_id, status, attempts, last_error,
                                   enqueued_at, completed_at)
            VALUES (?, ?, 'pending', 0, NULL, strftime('%Y-%m-%dT%H:%M:%fZ','now'), NULL)
            ON CONFLICT(entity_id) DO UPDATE SET
                run_id = excluded.run_id,
                status = 'pending',
                attempts = 0,
                last_error = NULL,
                enqueued_at = excluded.enqueued_at,
                completed_at = NULL
            """,
            (entity_id, run_id),
        )
    except Exception:  # noqa: BLE001 — see module docstring
        logger.warning("rag_sync: could not enqueue %s", entity_id, exc_info=True)


def pending_entities(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM rag_queue WHERE status = 'pending' AND attempts < ? "
        "ORDER BY enqueued_at, id LIMIT ?",
        (MAX_ATTEMPTS, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def queue_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM rag_queue GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def _mark(conn: sqlite3.Connection, entity_id: str, status: str, error: str | None = None) -> None:
    conn.execute(
        "UPDATE rag_queue SET status = ?, last_error = ?, attempts = attempts + 1, "
        "completed_at = CASE WHEN ? = 'done' THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END "
        "WHERE entity_id = ?",
        (status, error, status, entity_id),
    )


# ---------------------------------------------------------------------------
# The drain (remote Postgres + embeddings, all of it optional at runtime)
# ---------------------------------------------------------------------------


def _load_ingest_module():
    """Import `micro_rag/ingest/ingest.py` by path. It is script-style — no package
    `__init__.py`, and its own sibling imports (`from build_records import ...`) work via
    a sys.path insert it performs at module scope — so a normal package import is not
    available. Returns None if the module or its dependencies (psycopg2, torch) are not
    importable here; that is a perfectly ordinary state in the offline test environment
    and in any deployment that does not carry the RAG extras."""
    path = _INGEST_DIR / "ingest.py"
    if not path.exists():
        return None
    try:
        if str(_INGEST_DIR) not in sys.path:
            sys.path.insert(0, str(_INGEST_DIR))
        spec = importlib.util.spec_from_file_location("_micro_rag_ingest", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 — a missing optional dependency is not an error here
        logger.info("rag_sync: micro_rag ingest module unavailable", exc_info=True)
        return None


def _record_for(conn: sqlite3.Connection, entity_id: str, ingest_mod) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Build one micro-RAG `records` row + its provenance rows for a single entity, using
    the SAME mapping the batch job uses (`build_record`), so a lead ingested
    automatically here is byte-identical to one ingested by the manual job."""
    entity = get_entity(conn, entity_id)
    if entity is None:
        return None, []
    claims = get_claims(conn, entity_id)
    row = conn.execute(
        "SELECT outcome FROM enrichment_runs WHERE entity_id = ? ORDER BY id DESC LIMIT 1",
        (entity_id,),
    ).fetchone()
    outcome = row["outcome"] if row else None
    if not is_confirmed(outcome):
        # The lead was re-judged since it was queued. Ingesting it now would publish a
        # record the pipeline no longer stands behind — exactly the Achmea/ASR failure the
        # batch job's "latest run only" rule exists to prevent.
        return None, []
    hq_state = ingest_mod._hq_state(conn, entity_id)
    record = ingest_mod.build_record(entity_id, entity["canonical_name"], outcome, claims, hq_state=hq_state)
    provenance = [c for c in claims if c.get("field_name")]
    return record, provenance


def drain_queue(
    db_path: str | None = None,
    *,
    limit: int = 25,
    pg_dsn: str | None = None,
) -> dict[str, Any]:
    """Ingest every pending queued lead into the micro-RAG. Returns a summary dict and
    never raises:

    - `{"status": "skipped", "reason": "no DATABASE_URL"}` when the RAG DSN is unset —
      rows stay pending, nothing is lost.
    - `{"status": "skipped", "reason": "ingest module unavailable"}` when psycopg2 /
      torch / the micro_rag tree is not present (the normal offline case).
    - `{"status": "ok", "ingested": n, "failed": m, "build_hash": ...}` on a real run.

    A lead that no longer holds a confirmed outcome is dropped from the queue with
    `status='stale'` rather than ingested.
    """
    pg_dsn = pg_dsn or os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    with connection(db_path) as conn:
        pending = pending_entities(conn, limit=limit)
        if not pending:
            return {"status": "ok", "ingested": 0, "failed": 0, "pending": 0}
    if not pg_dsn:
        return {"status": "skipped", "reason": "no DATABASE_URL", "pending": len(pending)}
    ingest_mod = _load_ingest_module()
    if ingest_mod is None:
        return {"status": "skipped", "reason": "ingest module unavailable", "pending": len(pending)}

    records: list[dict[str, Any]] = []
    provenance_by_record: dict[str, list[dict[str, Any]]] = {}
    stale: list[str] = []
    with connection(db_path) as conn:
        for item in pending:
            entity_id = item["entity_id"]
            try:
                record, provenance = _record_for(conn, entity_id, ingest_mod)
            except Exception as exc:  # noqa: BLE001
                logger.warning("rag_sync: could not build record for %s", entity_id, exc_info=True)
                _mark(conn, entity_id, "pending", f"{type(exc).__name__}: {exc}"[:300])
                continue
            if record is None:
                stale.append(entity_id)
                continue
            records.append(record)
            provenance_by_record[entity_id] = provenance
        for entity_id in stale:
            _mark(conn, entity_id, "stale", "no longer a confirmed outcome")

    if not records:
        return {"status": "ok", "ingested": 0, "failed": 0, "stale": len(stale)}

    try:
        # prune=False is load-bearing: write_to_postgres's default prune deletes every
        # pipeline record not in the batch it was handed, which is right for the full
        # batch job and catastrophic here — this drain carries only the leads that
        # confirmed since the last one, so a pruning write would trade one new record for
        # the loss of every other. Removing a re-judged lead stays the batch job's work.
        build_hash = ingest_mod.write_to_postgres(
            pg_dsn, records, provenance_by_record, prune=False
        )
    except Exception as exc:  # noqa: BLE001 — the RAG being down must not fail anything
        logger.error("rag_sync: Postgres ingest failed", exc_info=True)
        with connection(db_path) as conn:
            for r in records:
                _mark(conn, r["record_id"], "pending", f"{type(exc).__name__}: {exc}"[:300])
        return {"status": "error", "ingested": 0, "failed": len(records),
                "error": f"{type(exc).__name__}: {exc}"[:300]}

    with connection(db_path) as conn:
        for r in records:
            _mark(conn, r["record_id"], "done")
    return {"status": "ok", "ingested": len(records), "failed": 0, "stale": len(stale),
            "build_hash": build_hash}

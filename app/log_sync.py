"""T37 — mirror the run log to Postgres so the hosted app can serve it.

The agent cannot run on Vercel and this module is not an attempt to make it: its state
is a 145MB SQLite file, one lead takes minutes against function timeouts measured in
seconds, its scheduler is a background loop that outlives any request, and its fetch
tier drives a headless Chromium. What IS portable is the *record* of what it did. So the
agent stays on the machine that can actually run it, and pushes its log up.

Same contract as app/rag_sync.py, for the same reasons: never raises, degrades to a
`skipped` summary when the DSN or psycopg2 is absent, and upserts by primary key so a
re-push corrects rather than duplicates. A push that fails costs nothing — the local
SQLite log remains the source of truth and the next push carries the same rows.

Bounded by design: it pushes the most recent `limit` runs rather than the whole history,
so a sync after a scheduled run is a small, predictable write no matter how long the
project has been running.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from app.db import connection

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "micro_rag" / "ingest" / "schema_log.sql"


def _load_psycopg2():
    """psycopg2 is optional here — the pipeline runs perfectly well with no Postgres at
    all. Import it lazily so a machine without it is a `skipped` sync, not an ImportError
    at module load."""
    try:
        import psycopg2  # noqa: PLC0415
        import psycopg2.extras  # noqa: PLC0415
        return psycopg2
    except Exception:  # noqa: BLE001
        logger.info("log_sync: psycopg2 unavailable", exc_info=True)
        return None


def _local_rows(db_path: str | None, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The most recent `limit` runs and every provenance row belonging to them."""
    with connection(db_path) as conn:
        runs = [dict(r) for r in conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()]
        if not runs:
            return [], []
        ids = [r["run_id"] for r in runs]
        placeholders = ",".join("?" * len(ids))
        fp = [dict(r) for r in conn.execute(
            f"SELECT * FROM field_provenance WHERE run_id IN ({placeholders})", ids
        ).fetchall()]
    return runs, fp


def sync_runs(
    db_path: str | None = None,
    *,
    limit: int = 50,
    pg_dsn: str | None = None,
) -> dict[str, Any]:
    """Push the recent run log to Postgres. Returns a summary; never raises.

    - `{"status": "skipped", "reason": "no DATABASE_URL"}` — the DSN is unset.
    - `{"status": "skipped", "reason": "psycopg2 unavailable"}` — the driver is absent.
    - `{"status": "ok", "runs": n, "fields": m}` — pushed.
    - `{"status": "error", "error": ...}` — Postgres refused; nothing local is lost.
    """
    pg_dsn = pg_dsn or os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not pg_dsn:
        return {"status": "skipped", "reason": "no DATABASE_URL"}
    psycopg2 = _load_psycopg2()
    if psycopg2 is None:
        return {"status": "skipped", "reason": "psycopg2 unavailable"}

    runs, fields = _local_rows(db_path, limit)
    if not runs:
        return {"status": "ok", "runs": 0, "fields": 0}

    conn = None
    try:
        conn = psycopg2.connect(pg_dsn)
        conn.autocommit = False
        with conn.cursor() as cur:
            # Idempotent schema, same pattern as the RAG ingest: the first sync from a
            # fresh checkout creates the tables rather than failing on a missing one.
            cur.execute(_SCHEMA_PATH.read_text(encoding="utf-8"))
            for r in runs:
                cur.execute(
                    """
                    INSERT INTO agent_runs (run_id, kind, status, git_sha, entity_count,
                                            started_at, ended_at, notes, synced_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
                    ON CONFLICT (run_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        entity_count = EXCLUDED.entity_count,
                        ended_at = EXCLUDED.ended_at,
                        notes = EXCLUDED.notes,
                        synced_at = now()
                    """,
                    (r["run_id"], r["kind"], r["status"], r.get("git_sha"),
                     r.get("entity_count") or 0, r.get("started_at"), r.get("ended_at"),
                     r.get("notes") or "{}"),
                )
            for f in fields:
                record = f.get("record") or "{}"
                # `record` is stored as a JSON string locally; parse only to read the
                # canonical_name out of it, then hand the original string to jsonb so the
                # payload the page renders is byte-identical to the local one.
                try:
                    canonical = json.loads(record).get("canonical_name")
                except (TypeError, ValueError):
                    canonical = None
                cur.execute(
                    """
                    INSERT INTO agent_field_provenance
                        (run_id, entity_id, field, canonical_name, value, status, shipped,
                         source_class, extraction_method, record, synced_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
                    ON CONFLICT (run_id, entity_id, field) DO UPDATE SET
                        canonical_name = EXCLUDED.canonical_name,
                        value = EXCLUDED.value,
                        status = EXCLUDED.status,
                        shipped = EXCLUDED.shipped,
                        source_class = EXCLUDED.source_class,
                        extraction_method = EXCLUDED.extraction_method,
                        record = EXCLUDED.record,
                        synced_at = now()
                    """,
                    (f["run_id"], f["entity_id"], f["field"], canonical,
                     None if f.get("value") is None else str(f["value"]),
                     f.get("status"), bool(f.get("shipped")), f.get("source_class"),
                     f.get("extraction_method"), record),
                )
        conn.commit()
        return {"status": "ok", "runs": len(runs), "fields": len(fields)}
    except Exception as exc:  # noqa: BLE001 — a hosted mirror must never break the agent
        logger.error("log_sync: push failed", exc_info=True)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

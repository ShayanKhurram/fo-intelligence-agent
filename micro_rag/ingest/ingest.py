"""Micro-RAG ingest job — micro_rag_plan.md §1/§11 step 1-2: a separate offline job,
not an endpoint. Reads the FO Intelligence Agent pipeline's REAL, live output
(data/foia.db's claims + enrichment_runs tables — the actual validated dataset, not a
frozen XLSX snapshot, since the source pipeline keeps running and producing new
survivors) and writes records/chunks/provenance/dataset_meta to Postgres+pgvector.

Idempotent and incremental: re-running picks up whatever new ship/ship_with_caveats
entities exist since the last run, upserting records/chunks in place. Versioned by a
build_hash (git SHA of the FO_Intelligence_Agent repo + record count + timestamp) per
plan §9's "/health returns dataset build hash and record count... proves the deployed
system is serving the same file you submitted"."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg2
import psycopg2.extras

from app.db import connection as sqlite_connection, get_claims, get_entity  # noqa: E402
from build_records import build_record  # noqa: E402
from chunks import build_chunks  # noqa: E402
from embeddings import embed_texts  # noqa: E402


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=_REPO_ROOT, timeout=5).decode().strip()
    except Exception:
        return "unknown"


def _hq_state(conn, entity_id: str) -> str | None:
    """entity_sources rows carry a raw "state" field from the discovery feed
    (app/ingest.py's _map_source) — not currently a Claim, so read it directly."""
    row = conn.execute(
        "SELECT payload FROM entity_sources WHERE entity_id = ? AND payload LIKE '%\"state\"%' ORDER BY retrieved_at DESC LIMIT 1",
        (entity_id,),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload"])
    state = payload.get("state")
    return state.strip().upper()[:2] if state else None


def fetch_ship_records(sqlite_db_path: str | None = None) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Pulls every ship/ship_with_caveats entity's real claim ledger from the live
    pipeline's SQLite DB and maps each to the micro-RAG's `records` row shape, plus its
    full provenance (one row per field-bearing claim, regardless of status — the
    provenance sheet is the audit trail, so it must show could_not_verify/contradicted
    claims too, not just the ones that made it into `records`)."""
    records: list[dict[str, Any]] = []
    provenance_by_record: dict[str, list[dict[str, Any]]] = {}
    with sqlite_connection(sqlite_db_path) as conn:
        # Each entity's LATEST enrichment_run only — an entity that shipped on an earlier
        # run but was later re-judged reject (e.g. after a gate fix) must NOT still appear.
        # A plain `DISTINCT entity_id WHERE outcome IN (ship,…)` matched ANY historical ship
        # and wrongly kept Achmea/ASR after they were re-rejected on the G1.Q3 polarity fix
        # (2026-07-29). Rank runs per entity by id (monotonic) and take rank 1.
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT entity_id, outcome,
                       ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY id DESC) AS rn
                FROM enrichment_runs
            )
            SELECT entity_id, outcome FROM latest
            WHERE rn = 1 AND outcome IN ('ship', 'ship_with_caveats')
            """
        ).fetchall()
        for row in rows:
            entity_id, outcome = row["entity_id"], row["outcome"]
            entity = get_entity(conn, entity_id)
            if entity is None:
                continue
            claims = get_claims(conn, entity_id)
            hq_state = _hq_state(conn, entity_id)
            records.append(build_record(entity_id, entity["canonical_name"], outcome, claims, hq_state=hq_state))
            provenance_by_record[entity_id] = [c for c in claims if c.get("field_name")]
    return records, provenance_by_record


def write_to_postgres(pg_dsn: str, records: list[dict[str, Any]], provenance_by_record: dict[str, list[dict[str, Any]]]) -> str:
    """Upserts records + chunks + provenance, writes a new dataset_meta row, returns
    the build_hash. Idempotent — safe to re-run as the live pipeline produces more rows."""
    build_hash = f"{_git_sha()}-{len(records)}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"

    conn = psycopg2.connect(pg_dsn)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            schema_sql = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
            cur.execute(schema_sql)

            class_counts: dict[str, int] = {}
            for r in records:
                cls = r.get("discovery_class_primary") or "unknown"
                class_counts[cls] = class_counts.get(cls, 0) + 1

            cur.execute(
                "INSERT INTO dataset_meta (build_hash, record_count, class_concentration) VALUES (%s, %s, %s)",
                (build_hash, len(records), json.dumps(class_counts)),
            )

            for r in records:
                r = {**r, "build_hash": build_hash}  # build_hash is computed here, not by build_record()
                cur.execute(
                    """
                    INSERT INTO records (record_id, build_hash, entity_name, entity_type, hq_state, hq_country,
                        aum_usd, aum_basis, aum_as_of, mandates, fit_tags, check_size_min, check_size_max,
                        principal_name, principal_title, principal_email, principal_email_status,
                        principal_phone, principal_phone_status, most_recent_signal_date, urgency_tier,
                        activity_status, actionability_score, discovery_class_primary, public_list_overlap,
                        record_confidence, outcome)
                    VALUES (%(record_id)s, %(build_hash)s, %(entity_name)s, %(entity_type)s, %(hq_state)s,
                        %(hq_country)s, %(aum_usd)s, %(aum_basis)s, %(aum_as_of)s, %(mandates)s, %(fit_tags)s,
                        %(check_size_min)s, %(check_size_max)s, %(principal_name)s, %(principal_title)s,
                        %(principal_email)s, %(principal_email_status)s, %(principal_phone)s,
                        %(principal_phone_status)s, %(most_recent_signal_date)s, %(urgency_tier)s,
                        %(activity_status)s, %(actionability_score)s, %(discovery_class_primary)s,
                        %(public_list_overlap)s, %(record_confidence)s, %(outcome)s)
                    ON CONFLICT (record_id) DO UPDATE SET
                        build_hash = EXCLUDED.build_hash, entity_type = EXCLUDED.entity_type,
                        hq_state = EXCLUDED.hq_state, aum_usd = EXCLUDED.aum_usd, aum_basis = EXCLUDED.aum_basis,
                        aum_as_of = EXCLUDED.aum_as_of, mandates = EXCLUDED.mandates, fit_tags = EXCLUDED.fit_tags,
                        check_size_min = EXCLUDED.check_size_min, check_size_max = EXCLUDED.check_size_max,
                        principal_name = EXCLUDED.principal_name, principal_title = EXCLUDED.principal_title,
                        principal_email = EXCLUDED.principal_email, principal_email_status = EXCLUDED.principal_email_status,
                        principal_phone = EXCLUDED.principal_phone, principal_phone_status = EXCLUDED.principal_phone_status,
                        most_recent_signal_date = EXCLUDED.most_recent_signal_date, urgency_tier = EXCLUDED.urgency_tier,
                        activity_status = EXCLUDED.activity_status, discovery_class_primary = EXCLUDED.discovery_class_primary,
                        public_list_overlap = EXCLUDED.public_list_overlap, record_confidence = EXCLUDED.record_confidence,
                        outcome = EXCLUDED.outcome, updated_at = now()
                    """,
                    r,
                )

                cur.execute("DELETE FROM chunks WHERE record_id = %s", (r["record_id"],))
                new_chunks = build_chunks(r)
                vectors = embed_texts([c["content"] for c in new_chunks])
                for chunk, vec in zip(new_chunks, vectors):
                    # pgvector's text input is a bracketed literal ("[1,2,3]"); psycopg2 has
                    # no adapter for the vector type, so passing the raw Python list would be
                    # coerced to a Postgres array ("{1,2,3}") and rejected. Format explicitly.
                    vec_literal = "[" + ",".join(repr(float(x)) for x in vec) + "]"
                    cur.execute(
                        "INSERT INTO chunks (chunk_id, record_id, facet, content, embedding, token_count) VALUES (%s,%s,%s,%s,%s,%s)",
                        (chunk["chunk_id"], chunk["record_id"], chunk["facet"], chunk["content"], vec_literal, len(chunk["content"].split())),
                    )

                cur.execute("DELETE FROM provenance WHERE record_id = %s", (r["record_id"],))
                for claim in provenance_by_record.get(r["record_id"], []):
                    cur.execute(
                        """INSERT INTO provenance (record_id, field_name, value, source_url, source_class,
                            extraction_method, retrieved_at, verification_method, confirming_url,
                            confirming_class, status, confidence)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            r["record_id"], claim["field_name"],
                            json.dumps(claim["answer"]) if isinstance(claim["answer"], (dict, list)) else str(claim["answer"]) if claim["answer"] is not None else None,
                            claim.get("source_url"), claim.get("source_class"), claim.get("extraction_method"),
                            claim.get("retrieved_at"), claim.get("verification_method"), claim.get("confirming_url"),
                            claim.get("confirming_class"), claim["status"], claim.get("confidence"),
                        ),
                    )

            # Prune records that dropped out of the ship set since the last ingest. Every
            # current record was just stamped with this build_hash; any record still carrying
            # an older build_hash is stale (e.g. an entity re-judged reject after a gate fix)
            # and must be removed so the live dataset matches the pipeline. Scoped to
            # source='pipeline' so a co-resident CSV import (source='csv_import') is never
            # touched by the pipeline's prune, and vice versa. chunks/provenance cascade.
            cur.execute(
                "DELETE FROM records WHERE build_hash <> %s AND COALESCE(source,'pipeline') = 'pipeline'",
                (build_hash,),
            )
            pruned = cur.rowcount
            if pruned:
                print(f"Pruned {pruned} stale record(s) no longer in the ship set")

            conn.commit()
        return build_hash
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    pg_dsn = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not pg_dsn:
        raise SystemExit("DATABASE_URL/POSTGRES_URL not set — provision Postgres via Vercel Marketplace (Neon) first")

    records, provenance_by_record = fetch_ship_records()
    print(f"Fetched {len(records)} real ship/ship_with_caveats records from the live pipeline")
    if not records:
        print("Nothing to ingest yet — the live pipeline hasn't produced any survivors.")
        return

    build_hash = write_to_postgres(pg_dsn, records, provenance_by_record)
    print(f"Ingested build_hash={build_hash}, {len(records)} records, {len(records) * 6} chunks")


if __name__ == "__main__":
    main()

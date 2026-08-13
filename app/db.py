"""SQLite access layer. Sync (sqlite3) for the Parser/Verdict read/write paths, which are
deterministic and cheap; async (aiosqlite) only where the batch runner needs it."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.config import SETTINGS

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or SETTINGS.db_path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL + busy_timeout: the batch runner opens many short-lived connections to the
    # same file concurrently (plan §5) — WAL lets readers and a writer coexist, and
    # busy_timeout makes writer/writer contention retry instead of raising immediately.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = _connect(db_path)
    try:
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        _apply_migrations(conn)
        conn.commit()
    finally:
        conn.close()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """`CREATE TABLE IF NOT EXISTS` doesn't add columns to a table that already exists
    from before this column was added — this runs the small set of ALTER TABLEs needed
    for a DB created before enrichment_validation_dataset_plan.md's Seam A change.
    Idempotent: SQLite raises on a column that already exists, which is caught and
    ignored, so re-running init_db() on an already-migrated DB is a no-op."""
    try:
        conn.execute("ALTER TABLE decisions ADD COLUMN thin_reason TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE rejections ADD COLUMN stage TEXT NOT NULL DEFAULT 'verdict'")
    except sqlite3.OperationalError:
        pass
    try:
        # PLAN.md T19.1: the claims spine gains subject_value in place. The live
        # data/foia.db is 145MB and must not be rebuilt — a guarded ALTER adds the
        # column to an existing table; CREATE TABLE IF NOT EXISTS already covers a
        # fresh DB (schema.sql). Idempotent: re-running on a migrated DB raises and is
        # caught here, so init_db() twice is a no-op.
        conn.execute("ALTER TABLE claims ADD COLUMN subject_value TEXT")
    except sqlite3.OperationalError:
        pass
    # PLAN.md T34.2: rename the why_now_trigger claim field to important_insight
    # in the live ledger. A display-only rename would leave the ledger saying
    # why_now_trigger while the row pivots on important_insight, blanking the
    # column for every lead already enriched (the T19 vocabulary split). This
    # UPDATE is idempotent — a second init_db() run updates 0 rows. Not a
    # Postgres column migration (no deployed DB column).
    conn.execute(
        "UPDATE claims SET field_name='important_insight' WHERE field_name='why_now_trigger'"
    )


@contextmanager
def connection(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_entity(
    conn: sqlite3.Connection,
    entity_id: str,
    canonical_name: str,
    aliases: list[str] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO entities (entity_id, canonical_name, aliases)
        VALUES (?, ?, ?)
        ON CONFLICT(entity_id) DO UPDATE SET
            canonical_name = excluded.canonical_name,
            aliases = excluded.aliases,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
        """,
        (entity_id, canonical_name, json.dumps(aliases or [])),
    )


def get_entity(conn: sqlite3.Connection, entity_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM entities WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["aliases"] = json.loads(d["aliases"])
    return d


def add_entity_source(
    conn: sqlite3.Connection,
    entity_id: str,
    source_class: str,
    payload: dict[str, Any],
    url: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO entity_sources (entity_id, source_class, url, payload)
        VALUES (?, ?, ?, ?)
        """,
        (entity_id, source_class, url, json.dumps(payload)),
    )
    return cur.lastrowid


def get_entity_sources(
    conn: sqlite3.Connection, entity_id: str, source_class: str | None = None
) -> list[dict[str, Any]]:
    if source_class is not None:
        rows = conn.execute(
            "SELECT * FROM entity_sources WHERE entity_id = ? AND source_class = ? "
            "ORDER BY retrieved_at",
            (entity_id, source_class),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM entity_sources WHERE entity_id = ? ORDER BY retrieved_at",
            (entity_id,),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        out.append(d)
    return out


def write_decision(
    conn: sqlite3.Connection,
    entity_id: str,
    verdict: str,
    rationale: str,
    gate_results: dict[str, Any],
    claim_ledger: list[dict[str, Any]],
    dead_ends: list[str],
    thin_reason: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO decisions (entity_id, verdict, rationale, gate_results, claim_ledger, dead_ends, thin_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_id,
            verdict,
            rationale,
            json.dumps(gate_results),
            json.dumps(claim_ledger),
            json.dumps(dead_ends),
            thin_reason,
        ),
    )
    return cur.lastrowid


def write_rejection(
    conn: sqlite3.Connection,
    entity_id: str,
    reason_code: str,
    gate_results: dict[str, Any],
    claim_ledger: list[dict[str, Any]],
    stage: str = "verdict",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO rejections (entity_id, reason_code, gate_results, claim_ledger, stage)
        VALUES (?, ?, ?, ?, ?)
        """,
        (entity_id, reason_code, json.dumps(gate_results), json.dumps(claim_ledger), stage),
    )
    return cur.lastrowid


def get_rejections(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """All rejections across every stage (verdict/wave0/validation) — Layer D's
    rejected_records sheet (enrichment_validation_dataset_plan.md §6.2)."""
    rows = conn.execute("SELECT * FROM rejections ORDER BY created_at").fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["gate_results"] = json.loads(d["gate_results"])
        d["claim_ledger"] = json.loads(d["claim_ledger"])
        out.append(d)
    return out


def get_decisions_by_verdict(conn: sqlite3.Connection, verdicts: list[str]) -> list[dict[str, Any]]:
    """Latest decision row per entity_id whose verdict is in `verdicts` — the entry
    point for Layer E's reserve-pool orchestration (app/enrichment.py::run_pipeline),
    which only ever operates on pursue/pursue_low leads (rejects never reach Layer E)."""
    placeholders = ",".join("?" * len(verdicts))
    rows = conn.execute(
        f"""
        SELECT d.* FROM decisions d
        INNER JOIN (
            SELECT entity_id, MAX(id) AS max_id FROM decisions GROUP BY entity_id
        ) latest ON d.id = latest.max_id
        WHERE d.verdict IN ({placeholders})
        ORDER BY d.entity_id
        """,
        verdicts,
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["gate_results"] = json.loads(d["gate_results"])
        d["claim_ledger"] = json.loads(d["claim_ledger"])
        d["dead_ends"] = json.loads(d["dead_ends"])
        out.append(d)
    return out


def upsert_checkpoint(
    conn: sqlite3.Connection,
    entity_id: str,
    status: str,
    attempts: int | None = None,
    last_error: str | None = None,
    cost_usd: float | None = None,
) -> None:
    row = conn.execute(
        "SELECT attempts, cost_usd FROM lead_checkpoints WHERE entity_id = ?",
        (entity_id,),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO lead_checkpoints (entity_id, status, attempts, last_error, cost_usd)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entity_id, status, attempts or 0, last_error, cost_usd or 0.0),
        )
        return
    new_attempts = attempts if attempts is not None else row["attempts"]
    new_cost = cost_usd if cost_usd is not None else row["cost_usd"]
    conn.execute(
        """
        UPDATE lead_checkpoints
        SET status = ?, attempts = ?, last_error = ?, cost_usd = ?,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE entity_id = ?
        """,
        (status, new_attempts, last_error, new_cost, entity_id),
    )


def get_checkpoint(conn: sqlite3.Connection, entity_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM lead_checkpoints WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    return dict(row) if row else None


def get_resumable_leads(conn: sqlite3.Connection) -> list[str]:
    """Leads the runner should pick up again: left 'running' or 'failed' by an interrupted
    run, or marked 'retry' because their research never completed (all lanes timed out and
    no claim was produced, so no verdict was recorded — see app.verdict.run_verdict)."""
    rows = conn.execute(
        "SELECT entity_id FROM lead_checkpoints WHERE status IN ('running', 'failed', 'retry')"
    ).fetchall()
    return [r["entity_id"] for r in rows]


def get_unstarted_entities(conn: sqlite3.Connection) -> list[str]:
    """Entities with no checkpoint row yet — never attempted."""
    rows = conn.execute(
        """
        SELECT e.entity_id FROM entities e
        LEFT JOIN lead_checkpoints c ON c.entity_id = e.entity_id
        WHERE c.entity_id IS NULL
        """
    ).fetchall()
    return [r["entity_id"] for r in rows]


def cache_get(conn: sqlite3.Connection, cache_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT response FROM tool_cache WHERE cache_key = ?", (cache_key,)
    ).fetchone()
    return json.loads(row["response"]) if row else None


def cache_set(
    conn: sqlite3.Connection, cache_key: str, tool_name: str, response: dict[str, Any]
) -> None:
    conn.execute(
        """
        INSERT INTO tool_cache (cache_key, tool_name, response)
        VALUES (?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
            response = excluded.response,
            cached_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
        """,
        (cache_key, tool_name, json.dumps(response)),
    )


def write_lead_trace(conn: sqlite3.Connection, entity_id: str, trace: list[dict[str, Any]]) -> None:
    conn.execute(
        """
        INSERT INTO lead_traces (entity_id, trace)
        VALUES (?, ?)
        ON CONFLICT(entity_id) DO UPDATE SET
            trace = excluded.trace,
            created_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
        """,
        (entity_id, json.dumps(trace, default=str)),
    )


def get_lead_trace(conn: sqlite3.Connection, entity_id: str) -> list[dict[str, Any]] | None:
    row = conn.execute("SELECT trace FROM lead_traces WHERE entity_id = ?", (entity_id,)).fetchone()
    return json.loads(row["trace"]) if row else None


# ============================================================================
# Enrichment / Validation / Dataset layers — the durable `claims` spine
# (enrichment_validation_dataset_plan.md §2/§7) plus supporting tables.
# ============================================================================

_CLAIM_COLUMNS = (
    "claim_id", "entity_id", "question_id", "field_name", "answer", "status",
    "source_url", "source_class", "extraction_method", "retrieved_at", "confidence",
    "produced_by", "wave", "verification_method", "confirming_url", "confirming_class",
    "verified_at",
)


def upsert_claim(conn: sqlite3.Connection, entity_id: str, claim: dict[str, Any]) -> str:
    """Insert or update one Claim row, keyed by claim_id. `claim` is a Claim.model_dump()
    dict. Returns the claim_id. Validation annotates existing rows via this same path
    (it never creates a *new* claim_id for an existing claim — see app/validation.py)."""
    # Some callers persist raw claim dicts that predate the claim_id field (e.g. claims
    # built by hand in tests, or anything not routed through Claim()) — mint a stable one
    # rather than requiring every call site to construct a full Claim first.
    claim_id = claim.get("claim_id") or uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{entity_id}|{claim.get('question_id')}|{claim.get('field_name')}|"
        f"{claim.get('source_url')}|{claim.get('answer')}",
    ).hex
    retrieved_at = claim.get("retrieved_at")
    verified_at = claim.get("verified_at")
    conn.execute(
        """
        INSERT INTO claims (
            claim_id, entity_id, question_id, field_name, answer, subject_value, status,
            source_url, source_class, extraction_method, retrieved_at, confidence,
            produced_by, wave, verification_method, confirming_url, confirming_class,
            verified_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(claim_id) DO UPDATE SET
            status = excluded.status,
            answer = excluded.answer,
            subject_value = excluded.subject_value,
            verification_method = excluded.verification_method,
            confirming_url = excluded.confirming_url,
            confirming_class = excluded.confirming_class,
            verified_at = excluded.verified_at
        """,
        (
            claim_id, entity_id, claim.get("question_id"), claim.get("field_name"),
            json.dumps(claim.get("answer"), default=str), claim.get("subject_value"),
            claim["status"],
            claim.get("source_url"), claim.get("source_class"), claim.get("extraction_method"),
            str(retrieved_at) if retrieved_at else None, claim["confidence"],
            claim.get("produced_by", "research"), claim.get("wave"),
            claim.get("verification_method"), claim.get("confirming_url"),
            claim.get("confirming_class"), str(verified_at) if verified_at else None,
        ),
    )
    return claim_id


def upsert_claims(conn: sqlite3.Connection, entity_id: str, claims: list[dict[str, Any]]) -> None:
    for c in claims:
        upsert_claim(conn, entity_id, c)


def get_claims(conn: sqlite3.Connection, entity_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM claims WHERE entity_id = ? ORDER BY created_at", (entity_id,)
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["answer"] = json.loads(d["answer"]) if d["answer"] is not None else None
        out.append(d)
    return out


def write_enrichment_run(
    conn: sqlite3.Connection,
    entity_id: str,
    wave: str,
    calls_spent: int,
    usd_spent: float,
    started_at: str,
    ended_at: str | None,
    outcome: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO enrichment_runs (entity_id, wave, calls_spent, usd_spent, started_at, ended_at, outcome)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (entity_id, wave, calls_spent, usd_spent, started_at, ended_at, outcome),
    )
    return cur.lastrowid


def get_enrichment_runs(conn: sqlite3.Connection, entity_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM enrichment_runs WHERE entity_id = ? ORDER BY started_at", (entity_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def write_field_status(
    conn: sqlite3.Connection,
    entity_id: str,
    field: str,
    status: str,
    method: str | None = None,
    confirming_url: str | None = None,
    confirming_class: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO field_status (entity_id, field, status, method, confirming_url, confirming_class)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (entity_id, field, status, method, confirming_url, confirming_class),
    )
    return cur.lastrowid


def get_field_statuses(conn: sqlite3.Connection, entity_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM field_status WHERE entity_id = ? ORDER BY last_checked", (entity_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def write_finding(
    conn: sqlite3.Connection,
    entity_id: str,
    check_id: str,
    severity: str,
    detail: str = "",
    claim_id: str | None = None,
    field: str | None = None,
    evidence_url: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO findings (entity_id, check_id, claim_id, field, severity, detail, evidence_url)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (entity_id, check_id, claim_id, field, severity, detail, evidence_url),
    )
    return cur.lastrowid


def get_findings(conn: sqlite3.Connection, entity_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM findings WHERE entity_id = ? ORDER BY created_at", (entity_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def write_chain_step(
    conn: sqlite3.Connection,
    entity_id: str,
    step_no: int,
    claim: str,
    originating_class: str | None = None,
    originating_url: str | None = None,
    confirming_class: str | None = None,
    confirming_url: str | None = None,
    method: str | None = None,
    result: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO chain_steps (
            entity_id, step_no, claim, originating_class, originating_url,
            confirming_class, confirming_url, method, result
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_id, step_no, claim, originating_class, originating_url,
            confirming_class, confirming_url, method, result,
        ),
    )
    return cur.lastrowid


def get_chain_steps(conn: sqlite3.Connection, entity_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM chain_steps WHERE entity_id = ? ORDER BY step_no", (entity_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def write_audit_rejected_value(
    conn: sqlite3.Connection,
    entity_id: str,
    field_name: str,
    rejected_value: Any,
    reason_code: str,
    evidence_url: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO audit_rejected_values (entity_id, field_name, rejected_value, reason_code, evidence_url)
        VALUES (?, ?, ?, ?, ?)
        """,
        (entity_id, field_name, json.dumps(rejected_value, default=str), reason_code, evidence_url),
    )
    return cur.lastrowid


def get_audit_rejected_values(conn: sqlite3.Connection, entity_id: str | None = None) -> list[dict[str, Any]]:
    if entity_id is not None:
        rows = conn.execute(
            "SELECT * FROM audit_rejected_values WHERE entity_id = ? ORDER BY rejected_at",
            (entity_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM audit_rejected_values ORDER BY rejected_at").fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["rejected_value"] = json.loads(d["rejected_value"]) if d["rejected_value"] is not None else None
        out.append(d)
    return out


def write_production_record(
    conn: sqlite3.Connection,
    entity_id: str,
    rank: int | None,
    primary_class: str | None,
    excluded_by_quota: bool = False,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO production_records (entity_id, rank, primary_class, excluded_by_quota)
        VALUES (?, ?, ?, ?)
        """,
        (entity_id, rank, primary_class, int(excluded_by_quota)),
    )
    return cur.lastrowid


def get_production_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM production_records ORDER BY selected_at").fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["excluded_by_quota"] = bool(d["excluded_by_quota"])
        out.append(d)
    return out


def backfill_claims_from_decisions(conn: sqlite3.Connection) -> int:
    """One-off migration for `claims` (enrichment_validation_dataset_plan.md §8 step 1):
    every prior run persisted its ClaimLedger only as a JSON blob inside `decisions` /
    `rejections`, never as rows in `claims`. Walks both tables and upserts each ledger
    entry. Ledger entries from before this change carry no `claim_id` — one is minted
    here (stable per (entity_id, question_id, source_url, answer) via uuid5, so
    re-running this function is idempotent rather than duplicating rows on every call).
    Returns the number of claim rows written."""
    written = 0
    for table in ("decisions", "rejections"):
        rows = conn.execute(f"SELECT entity_id, claim_ledger FROM {table}").fetchall()
        for row in rows:
            entity_id = row["entity_id"]
            ledger = json.loads(row["claim_ledger"])
            for claim in ledger:
                if not claim.get("claim_id"):
                    fingerprint = f"{entity_id}|{claim.get('question_id')}|{claim.get('source_url')}|{claim.get('answer')}"
                    claim["claim_id"] = uuid.uuid5(uuid.NAMESPACE_URL, fingerprint).hex
                claim.setdefault("confidence", "low")
                claim.setdefault("produced_by", "parser" if claim.get("source_class") in
                                  ("adv_index", "13f_filing", "5500_filing", "conference_sighting") else "research")
                upsert_claim(conn, entity_id, claim)
                written += 1
    return written

// T46.1 — Intent Watcher's own schema.
//
// These four tables are owned entirely by the Next.js web app — the Python ingest
// (micro_rag/ingest/ingest.py) never touches them and never will. The DDL lives here as
// TypeScript string literals, **not** in a `.sql` file beside the ingest schema, because
// a file read would not survive the Vercel serverless bundle (the deploy only ships
// module code, not loose assets read off disk at runtime). Embedding it here keeps the
// watcher self-bootstrapping on a cold instance.
//
// HARD CONSTRAINT — no foreign key from any watcher table to `records`. The ingest prunes
// `records` by `build_hash` and `DELETE`s `provenance` per record on every run; if a
// watcher table pointed at `records`, the watcher could either block that prune (an FK
// that keeps a doomed row alive) or be cascaded out from under a live run. Either way the
// watcher would interfere with ingest's transaction. The watcher only ever reads the
// corpus; it owns its own keyed-by-`record_id`-as-plain-text tables and stays out of the
// ingest's way.
import type { Pool } from "pg";

export const WATCH_SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS watch_runs (
    run_id          TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'running',  -- running | done | cancelled | failed
    scope           INTEGER,
    lookback_days   INTEGER NOT NULL DEFAULT 90,
    backend         TEXT,
    queued          INTEGER,
    completed       INTEGER,
    found           INTEGER,
    failed          INTEGER,
    started_at      TIMESTAMPTZ DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    last_call_at    TIMESTAMPTZ,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS watch_run_targets (
    run_id      TEXT REFERENCES watch_runs ON DELETE CASCADE,
    record_id   TEXT,
    position    INTEGER NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed | skipped
    duration_ms INTEGER,
    note        TEXT,
    PRIMARY KEY (run_id, record_id)
);
CREATE INDEX IF NOT EXISTS idx_watch_run_targets_pending
    ON watch_run_targets (run_id, position) WHERE state = 'pending';

CREATE TABLE IF NOT EXISTS watch_signals (
    id             BIGSERIAL PRIMARY KEY,
    run_id         TEXT REFERENCES watch_runs ON DELETE SET NULL,
    record_id      TEXT NOT NULL,
    dedupe_key     TEXT NOT NULL,
    kind           TEXT NOT NULL,
    headline       TEXT NOT NULL,
    meaning        TEXT,
    confidence     TEXT,
    source_url     TEXT,
    source_domain  TEXT,
    source_name    TEXT,
    observed_at    DATE,
    created_at     TIMESTAMPTZ DEFAULT now(),
    UNIQUE (record_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_watch_signals_record ON watch_signals (record_id, created_at DESC);

CREATE TABLE IF NOT EXISTS watch_org_state (
    record_id          TEXT PRIMARY KEY,
    last_researched_at TIMESTAMPTZ,
    last_found_at      TIMESTAMPTZ,
    last_signal_count  INTEGER NOT NULL DEFAULT 0
);
`;

// Module-level cached promise: `ensureWatchSchema` is called on every watcher request,
// but the DDL only needs to run once per (cold) process. The first caller kicks it off;
// every later caller awaits the same promise, so a burst of concurrent requests doesn't
// issue N identical `CREATE TABLE IF NOT EXISTS` round-trips. A thrown error clears the
// cache so a later retry can succeed rather than permanently memoising a failure.
let schemaPromise: Promise<void> | null = null;

export function ensureWatchSchema(pool: Pool): Promise<void> {
  if (!schemaPromise) {
    schemaPromise = (async () => {
      await pool.query(WATCH_SCHEMA_SQL);
    })().catch((err) => {
      schemaPromise = null; // allow a retry on a subsequent call
      throw err;
    });
  }
  return schemaPromise;
}
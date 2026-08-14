-- T37 — the agent's run log, mirrored to Postgres so the Vercel app can serve it.
--
-- The agent itself cannot run on Vercel (no persistent filesystem for its 145MB SQLite
-- state, function timeouts far shorter than a single lead, no background loops, and a
-- toolchain that drives headless Chromium). What IS portable is the *record* of what it
-- did — so the agent stays local and pushes these two tables up.
--
-- Deliberately separate from `records`/`chunks`/`provenance`: those are the RAG corpus,
-- answering "what do we know about this firm". These answer "what did the agent do, and
-- how did it learn it". Mixing them would make the RAG's record_count meaningless.
--
-- Both tables are append-or-replace by primary key, so a re-push corrects rather than
-- duplicates — the same idempotence rule the RAG ingest follows.

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id        TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,              -- layer1 | enrichment | dataset | api | scheduled
    status        TEXT NOT NULL,              -- running | done | failed
    git_sha       TEXT,
    entity_count  INTEGER NOT NULL DEFAULT 0,
    started_at    TIMESTAMPTZ,
    ended_at      TIMESTAMPTZ,
    notes         JSONB NOT NULL DEFAULT '{}'::jsonb,   -- schedule, counters, termination reason
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_started ON agent_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS agent_field_provenance (
    run_id            TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    entity_id         TEXT NOT NULL,
    field             TEXT NOT NULL,
    canonical_name    TEXT,
    value             TEXT,
    status            TEXT,
    shipped           BOOLEAN NOT NULL DEFAULT false,
    source_class      TEXT,
    extraction_method TEXT,
    -- The full schema_version-1 record: how.summary, the tool calls, the alternatives
    -- that lost, the blank_reason. The page renders this; the columns beside it exist so
    -- it can filter and count without parsing every blob.
    record            JSONB NOT NULL,
    synced_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, entity_id, field)
);

CREATE INDEX IF NOT EXISTS idx_agent_fp_run ON agent_field_provenance(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_fp_entity ON agent_field_provenance(entity_id);

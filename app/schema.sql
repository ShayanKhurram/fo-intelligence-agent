-- FO Intelligence Agent — Research Layer schema (SQLite)
--
-- entity_sources.payload conventions (JSON), keyed by source_class:
--   adv_index          {"present": bool, "client_count": int, "crd": str}
--   13f_filing         {"quarter": "2026Q1", "value_usd": int, "prior_quarter": "2025Q4",
--                        "prior_value_usd": int}
--   5500_filing        {"plan_year": int, "participant_count": int}
--   conference_sighting{"conference": str, "date": "YYYY-MM-DD", "role": str}
--   domain_check       {"domain": str, "mx_present": bool}
-- Any other source_class (edgar_filing, web_page, news_article, linkedin_profile, ...)
-- is evidence collected during research, not an injected fact.

CREATE TABLE IF NOT EXISTS entities (
    entity_id       TEXT PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    aliases         TEXT NOT NULL DEFAULT '[]',   -- JSON list[str]
    status          TEXT NOT NULL DEFAULT 'new',  -- new|queued|running|verdict_done|failed
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS entity_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       TEXT NOT NULL REFERENCES entities(entity_id),
    source_class    TEXT NOT NULL,
    url             TEXT,
    payload         TEXT NOT NULL DEFAULT '{}',   -- JSON
    retrieved_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_entity_sources_entity ON entity_sources(entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_sources_class ON entity_sources(entity_id, source_class);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       TEXT NOT NULL REFERENCES entities(entity_id),
    verdict         TEXT NOT NULL,                -- pursue|pursue_low
    rationale       TEXT NOT NULL DEFAULT '',
    gate_results    TEXT NOT NULL DEFAULT '{}',    -- JSON
    claim_ledger    TEXT NOT NULL DEFAULT '[]',    -- JSON list[Claim]
    dead_ends       TEXT NOT NULL DEFAULT '[]',    -- JSON list[str]
    thin_reason     TEXT,                          -- "fixable"|"structural"|NULL, only set on pursue_low
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS rejections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       TEXT NOT NULL REFERENCES entities(entity_id),
    reason_code     TEXT NOT NULL,
    gate_results    TEXT NOT NULL DEFAULT '{}',    -- JSON
    claim_ledger    TEXT NOT NULL DEFAULT '[]',    -- JSON list[Claim]
    stage           TEXT NOT NULL DEFAULT 'verdict', -- verdict|wave0|validation — Layer D's rejected_records sheet
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Batch runner checkpoint ledger (§5 of research_layer_plan.md)
CREATE TABLE IF NOT EXISTS lead_checkpoints (
    entity_id       TEXT PRIMARY KEY REFERENCES entities(entity_id),
    status          TEXT NOT NULL DEFAULT 'queued', -- queued|running|verdict_done|failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    cost_usd        REAL NOT NULL DEFAULT 0.0,
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Local cache for tools without their own cache layer (GDELT/EDGAR/ProPublica/LinkedIn).
-- OrioSearch owns its own Redis cache (§4.7) so web_search/fetch_page never land here.
CREATE TABLE IF NOT EXISTS tool_cache (
    cache_key       TEXT PRIMARY KEY,
    tool_name       TEXT NOT NULL,
    response        TEXT NOT NULL,                -- JSON
    cached_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Full reasoning/tool-call trace for one lead's graph run: every supervisor turn
-- (think_tool reflections, lane dispatch decisions), every researcher turn per lane
-- (tool calls + results), and the compress/verdict LLM calls. Not part of the original
-- plan's state models — added because none of this was persisted anywhere and a lead's
-- internal reasoning was unrecoverable once the graph run finished (see PROJECT_LOG.md).
-- `trace` is a JSON list of event dicts, ordered as emitted; see app/state.py's
-- TraceEvent-shaped dicts for the exact fields each event type carries.
CREATE TABLE IF NOT EXISTS lead_traces (
    entity_id       TEXT PRIMARY KEY REFERENCES entities(entity_id),
    trace           TEXT NOT NULL DEFAULT '[]',    -- JSON list[dict]
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ============================================================================
-- Enrichment / Validation / Dataset layers (enrichment_validation_dataset_plan.md).
-- `claims` is the durable spine (§2/§7 of that plan) — every Claim (parser, research,
-- enrichment, derived) gets a row here, in addition to the JSON claim_ledger blob
-- already embedded in decisions/rejections (kept for backward compat with the trace
-- viewer / API). Every sheet, every metric, and the chain deliverable read from this
-- one table going forward.
-- ============================================================================

CREATE TABLE IF NOT EXISTS claims (
    claim_id            TEXT PRIMARY KEY,
    entity_id           TEXT NOT NULL REFERENCES entities(entity_id),
    question_id         TEXT,
    field_name          TEXT,
    answer              TEXT,                       -- JSON-encoded (Claim.answer is Any)
    subject_value       TEXT,
    status              TEXT NOT NULL,
    source_url          TEXT,
    source_class        TEXT,
    extraction_method   TEXT,
    retrieved_at        TEXT,
    confidence          TEXT NOT NULL,
    produced_by         TEXT NOT NULL DEFAULT 'research',
    wave                TEXT,
    verification_method TEXT,
    confirming_url       TEXT,
    confirming_class     TEXT,
    verified_at          TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_claims_entity ON claims(entity_id);
CREATE INDEX IF NOT EXISTS idx_claims_field ON claims(entity_id, field_name);

CREATE TABLE IF NOT EXISTS enrichment_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       TEXT NOT NULL REFERENCES entities(entity_id),
    wave            TEXT NOT NULL,
    calls_spent     INTEGER NOT NULL DEFAULT 0,
    usd_spent       REAL NOT NULL DEFAULT 0.0,
    started_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ended_at        TEXT,
    outcome         TEXT
);

CREATE INDEX IF NOT EXISTS idx_enrichment_runs_entity ON enrichment_runs(entity_id);

CREATE TABLE IF NOT EXISTS field_status (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id           TEXT NOT NULL REFERENCES entities(entity_id),
    field               TEXT NOT NULL,
    status              TEXT NOT NULL,
    method              TEXT,
    confirming_url      TEXT,
    confirming_class    TEXT,
    last_checked        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_field_status_entity ON field_status(entity_id, field);

CREATE TABLE IF NOT EXISTS findings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       TEXT NOT NULL REFERENCES entities(entity_id),
    check_id        TEXT NOT NULL,
    claim_id        TEXT,
    field           TEXT,
    severity        TEXT NOT NULL,               -- fatal|warn|info
    detail          TEXT NOT NULL DEFAULT '',
    evidence_url    TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_findings_entity ON findings(entity_id);

CREATE TABLE IF NOT EXISTS chain_steps (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id           TEXT NOT NULL REFERENCES entities(entity_id),
    step_no             INTEGER NOT NULL,
    claim               TEXT NOT NULL,
    originating_class   TEXT,
    originating_url     TEXT,
    confirming_class    TEXT,
    confirming_url      TEXT,
    method              TEXT,
    result              TEXT,
    timestamp           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_chain_steps_entity ON chain_steps(entity_id, step_no);

CREATE TABLE IF NOT EXISTS audit_rejected_values (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       TEXT NOT NULL REFERENCES entities(entity_id),
    field_name      TEXT NOT NULL,
    rejected_value  TEXT,
    reason_code     TEXT NOT NULL,
    evidence_url    TEXT,
    rejected_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_rejected_entity ON audit_rejected_values(entity_id);

CREATE TABLE IF NOT EXISTS production_records (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id           TEXT NOT NULL REFERENCES entities(entity_id),
    selected_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    rank                INTEGER,
    primary_class       TEXT,
    excluded_by_quota   INTEGER NOT NULL DEFAULT 0   -- boolean
);

CREATE INDEX IF NOT EXISTS idx_production_records_entity ON production_records(entity_id);

-- ============================================================================
-- T35 — per-run field provenance log (PLAN.md T35). `runs` is the durable identity
-- of one execution of any of the pipeline's layers (layer1 / enrichment / dataset /
-- api): started_at/ended_at, status, and a free-form JSON `notes` blob for the
-- layer-specific outcome counters. `tool_calls` records every external tool call
-- enrichment makes while processing an entity, attributed to a run + entity + wave,
-- so each output lead field can later be traced back to the exact call that
-- produced it. New tables, additive only — no migration of existing rows.
-- ============================================================================

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,                  -- layer1 | enrichment | dataset | api
    status       TEXT NOT NULL DEFAULT 'running',-- running | done | failed
    git_sha      TEXT,
    entity_count INTEGER NOT NULL DEFAULT 0,
    started_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ended_at     TEXT,
    notes        TEXT NOT NULL DEFAULT '{}'      -- JSON
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);

CREATE TABLE IF NOT EXISTS tool_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT,
    entity_id      TEXT NOT NULL,
    wave           TEXT,
    tool           TEXT NOT NULL,
    args           TEXT NOT NULL DEFAULT '{}',   -- JSON, redacted
    ok             INTEGER NOT NULL DEFAULT 1,
    error          TEXT,
    result_summary TEXT,
    result_url     TEXT,
    cache_hit      INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER,
    called_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_entity ON tool_calls(entity_id, called_at);
CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id);

-- T35.5 — the persisted, per-run field provenance log (PLAN.md T35.5). One row per
-- (run_id, entity_id, field): the scalar columns let the web page filter/sort without
-- parsing the JSON blob, and `record` is the full schema_version-1 JSON the page renders.
-- Re-running a run_id upserts (UNIQUE on run_id+entity_id+field) rather than duplicating —
-- the log is immutable per run, snapshotted at emission (PLAN.md T35 point 2).
CREATE TABLE IF NOT EXISTS field_provenance (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL,
    entity_id         TEXT NOT NULL,
    field             TEXT NOT NULL,
    value             TEXT,
    status            TEXT,
    shipped           INTEGER NOT NULL DEFAULT 0,
    source_class      TEXT,
    extraction_method TEXT,
    record            TEXT NOT NULL,            -- the full JSON record
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(run_id, entity_id, field)
);

CREATE INDEX IF NOT EXISTS idx_field_provenance_entity ON field_provenance(entity_id);

-- ============================================================================
-- T36 — the scheduler (PLAN.md T36). `schedules` holds the user's standing orders:
-- when to run, how many confirmed leads to stop at, and a hard cap on how many leads
-- may be processed. A scheduled execution writes an ordinary `runs` row with
-- kind='scheduled' (so the Log tab, the provenance log and the API all treat it like
-- any other run) whose `notes` carry the schedule_id, the counters and — the part that
-- matters for reading a log months later — WHY it stopped.
-- ============================================================================

CREATE TABLE IF NOT EXISTS schedules (
    schedule_id      TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    kind             TEXT NOT NULL DEFAULT 'daily',   -- daily | interval
    time_utc         TEXT,                            -- 'HH:MM', kind='daily'
    interval_minutes INTEGER,                         -- kind='interval'
    target_confirmed INTEGER,                         -- stop once this many leads confirm; NULL = no target
    max_leads        INTEGER,                         -- hard cap on leads processed; NULL = until the queue empties
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_run_at      TEXT,
    next_run_at      TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_schedules_next ON schedules(enabled, next_run_at);

-- T36.3 — leads awaiting micro-RAG ingestion. Written the moment a lead confirms (a
-- local, microsecond SQLite write in the run's path); drained separately against
-- Postgres + the embedding model, which are remote, optional and allowed to be down.
-- A failed drain leaves the row 'pending' so the work is delayed, never dropped.
-- UNIQUE(entity_id): a lead re-confirmed by a later run resets to pending so the fresher
-- ledger is re-ingested (the RAG upserts by record_id — a correction, not a duplicate).
CREATE TABLE IF NOT EXISTS rag_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id    TEXT NOT NULL UNIQUE,
    run_id       TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed | stale
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    enqueued_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_rag_queue_status ON rag_queue(status, enqueued_at);

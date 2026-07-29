-- Micro-RAG data layer (micro_rag_plan.md §3.1). Postgres + pgvector.
-- Embedding dim is 384 (sentence-transformers/all-MiniLM-L6-v2, local, no API key —
-- see PROJECT_LOG.md: Ollama Cloud has no embedding-capable model in its catalog, so
-- this substitutes for the plan's text-embedding-3-small/1536-dim; documented as a
-- deliberate deviation, not a silent one, per the plan's own honesty requirement).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS dataset_meta (
    build_hash          TEXT PRIMARY KEY,
    built_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    record_count        INTEGER NOT NULL,
    class_concentration JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS records (
    record_id               TEXT PRIMARY KEY,      -- entity_id from the source pipeline
    build_hash              TEXT NOT NULL REFERENCES dataset_meta(build_hash),
    entity_name             TEXT NOT NULL,
    entity_type             TEXT NOT NULL DEFAULT 'type_unconfirmed',  -- SFO | MFO | type_unconfirmed
    hq_state                TEXT,
    hq_country              TEXT DEFAULT 'US',
    aum_usd                 BIGINT,
    aum_basis               TEXT,
    aum_as_of               TEXT,
    mandates                TEXT[] NOT NULL DEFAULT '{}',
    fit_tags                TEXT[] NOT NULL DEFAULT '{}',
    check_size_min          BIGINT,
    check_size_max          BIGINT,
    principal_name          TEXT,
    principal_title         TEXT,
    principal_email         TEXT,
    principal_email_status  TEXT NOT NULL DEFAULT 'could_not_verify',
    principal_phone         TEXT,
    principal_phone_status  TEXT NOT NULL DEFAULT 'could_not_verify',
    most_recent_signal_date DATE,
    urgency_tier            TEXT,
    activity_status         TEXT,
    actionability_score     REAL,
    discovery_class_primary TEXT,
    public_list_overlap     TEXT[] NOT NULL DEFAULT '{}',
    record_confidence       TEXT,
    outcome                 TEXT NOT NULL,          -- ship | ship_with_caveats
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_records_type ON records(entity_type);
CREATE INDEX IF NOT EXISTS idx_records_state ON records(hq_state);
CREATE INDEX IF NOT EXISTS idx_records_aum ON records(aum_usd);
CREATE INDEX IF NOT EXISTS idx_records_signal_date ON records(most_recent_signal_date);
CREATE INDEX IF NOT EXISTS idx_records_mandates ON records USING GIN(mandates);
CREATE INDEX IF NOT EXISTS idx_records_fit_tags ON records USING GIN(fit_tags);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id     TEXT PRIMARY KEY,
    record_id    TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    facet        TEXT NOT NULL,  -- identity | mandate | people | activity | why_now | summary
    content      TEXT NOT NULL,
    embedding    vector(384),
    token_count  INTEGER NOT NULL DEFAULT 0,
    content_tsv  tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);

CREATE INDEX IF NOT EXISTS idx_chunks_record ON chunks(record_id);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING GIN(content_tsv);

-- Contact fields (principal_email/principal_phone) are NEVER embedded (plan §3.2) —
-- they live only in `records`, retrieved structurally after a record is selected.

CREATE TABLE IF NOT EXISTS provenance (
    id                  SERIAL PRIMARY KEY,
    record_id           TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    field_name          TEXT NOT NULL,
    value               TEXT,
    source_url          TEXT,
    source_class        TEXT,
    extraction_method   TEXT,
    retrieved_at        TIMESTAMPTZ,
    verification_method TEXT,
    confirming_url      TEXT,
    confirming_class    TEXT,
    status              TEXT NOT NULL,
    confidence          TEXT
);

CREATE INDEX IF NOT EXISTS idx_provenance_record ON provenance(record_id, field_name);

-- Every grounding-gate decision (plan §5's three gates) — retrieval scores, stripped
-- sentences with reasons, status downgrades. This log is the evidence for the §8
-- answer-layer evaluation.
CREATE TABLE IF NOT EXISTS query_log (
    id                  SERIAL PRIMARY KEY,
    asked_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    query_text          TEXT NOT NULL,
    intent              TEXT,
    parsed_filters      JSONB,
    relaxed_filters     JSONB,
    retrieved_record_ids TEXT[],
    top_score           REAL,
    gate1_decision      TEXT,   -- "proceed" | "decline_low_score" | "decline_zero_results" | "decline_out_of_scope"
    raw_answer          TEXT,
    stripped_sentences  JSONB,
    stripped_fraction   REAL,
    gate2_decision      TEXT,   -- "proceed" | "discard_over_threshold"
    final_answer        TEXT,
    build_hash          TEXT
);

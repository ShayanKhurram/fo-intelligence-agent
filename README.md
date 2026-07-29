# FO Intelligence Agent

> An end-to-end system for **finding, qualifying, and querying family-office investment leads** from public filings — with correctness treated as a first-class concern at every layer.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Next.js](https://img.shields.io/badge/Next.js-16.2-000000?logo=next.js&logoColor=white)
![Postgres](https://img.shields.io/badge/Postgres-17%20%2B%20pgvector-4169E1?logo=postgresql&logoColor=white)
![Tests](https://img.shields.io/badge/tests-242%20passing-2ea44f)
![License](https://img.shields.io/badge/license-MIT-blue)

**Live demo:** https://fo-micro-rag.vercel.app

---

## Table of contents

- [What it is](#what-it-is)
- [Design thesis](#design-thesis)
- [Tech stack](#tech-stack)
- [Architecture](#architecture)
- [Data model](#data-model)
- [Repository layout](#repository-layout)
- [Getting started](#getting-started)
- [Testing](#testing)
- [Notable engineering decisions](#notable-engineering-decisions)
- [Known limitations](#known-limitations)

---

## What it is

Two subsystems that share a data contract:

| Subsystem | Path | Role |
| --- | --- | --- |
| **Pipeline** | `app/` | A LangGraph research agent that takes a raw entity off a discovery queue, runs it through a fixed question battery → multi-wave enrichment → a validation layer, and emits a structured, evidence-backed record. Every field carries provenance and a verification status. |
| **Micro-RAG** | `micro_rag/` | A retrieval system over the qualified records with **three mechanical grounding gates**, so a generated answer can only assert what the underlying data provably supports. Deployed as a Next.js app on Vercel + Supabase. |

## Design thesis

The domain punishes false positives. A "family office" that is actually a plain RIA, a bank, or a mutual fund is worse than no lead at all, and a fabricated contact is worse still. The guiding principle throughout:

> **Ship only what the evidence supports; leave everything else honestly blank.**

Concretely:

- **Claim-ledger model** — every value is a `Claim` with a `status` (`confirmed` / `single_source` / `could_not_verify` / `contradicted` / …), a source class, and a verification method.
- **Cross-class corroboration** — a claim is promoted to `verified` only when a *different* class of source independently confirms it.
- **Release rule** — any high-value field that fails validation is stripped and blanked before assembly, with an audit trail (`audit_rejected_values`).
- **Identity gating on answer polarity, not just status** — the firm-is-a-genuine-family-office check rejects a *settled-but-negative* answer, not only an unresolved one.
- **Grounding enforced in code, not by prompt** — a post-generation entailment gate strips any sentence whose `[record:field]` citation doesn't resolve to a real, settled field, and discards the whole answer if too much was stripped.

---

## Tech stack

### Pipeline (`app/`)
| Concern | Choice | Version |
| --- | --- | --- |
| Language | Python | 3.12 |
| Agent graph | LangGraph + langchain-core (StateGraph only) | `langgraph 1.2`, `langchain-core 1.4` |
| Data models | Pydantic | 2.7+ |
| Storage | SQLite (WAL mode) via `aiosqlite` | stdlib + `aiosqlite 0.20` |
| HTTP | httpx (async, pooled) | 0.27+ |
| LLM | Ollama Cloud (default) · Anthropic (optional) | OpenAI-compatible API |
| Tools | SEC EDGAR · Serper (Google SERP) · Hunter · GDELT · `dnspython` · vendored Scrapy LinkedIn scraper | — |
| Docs/extraction | `trafilatura` (free-path fetch) · `openpyxl` (XLSX export) | — |
| Local UI/API | FastAPI + Uvicorn | 0.115+ |
| Tests | pytest + pytest-asyncio (fully offline) | 8+ |

### Micro-RAG ingest (`micro_rag/ingest/`)
| Concern | Choice |
| --- | --- |
| Language | Python 3.12 |
| Vector store | PostgreSQL 16/17 + **pgvector** (`vector(384)`, HNSW index) |
| Driver | `psycopg2` |
| Embeddings | `sentence-transformers/all-MiniLM-L6-v2` (384-dim) via `transformers` |

### Micro-RAG web (`micro_rag/web/`)
| Concern | Choice | Version |
| --- | --- | --- |
| Framework | Next.js (App Router, Turbopack) | **16.2.12** |
| UI | React | 19.2 |
| Styling | Tailwind CSS | v4 |
| Language | TypeScript | 5 |
| DB driver | `pg` (node-postgres) | 8.22 |
| Query-time embeddings | `@xenova/transformers` (ONNX, WASM/native) | 2.17 |
| Validation | `zod` | 4 |
| LLM | Ollama Cloud (`gemma4:31b`, non-reasoning — chosen for latency) | — |
| Hosting | Vercel (serverless) + Supabase (managed Postgres/pgvector) | — |

---

## Architecture

### Pipeline (`app/`)

```
Discovery queue
      │
      ▼
┌─────────────────────── Layer 1: Research ───────────────────────┐
│ Parser → LangGraph supervisor/researcher → Verdict              │
│ 12-question battery across 3 lanes (identity · people · signals)│
│ HARD gates evaluated mechanically → pursue / pursue_low / reject│
└─────────────────────────────────────────────────────────────────┘
      │  (pursue / pursue_low)
      ▼
┌──────────────── Layer E: Enrichment (wave-based) ───────────────┐
│ wave -1  free derivation from discovery data                    │
│ wave  0  cheap identity gate (is this really a family office?)  │
│ wave  1  actionability core (decision-maker · contact · signal) │
│ wave  2  depth (survivors only)                                 │
└─────────────────────────────────────────────────────────────────┘
      ▼
┌──────────────── Layer V: Validation ────────────────────────────┐
│ V1 source-supports-claim (LLM judge) · V4 contradictions        │
│ V5 staleness + firm-is-FO hardening · V6 completeness           │
│ cross-class corroboration · release rule                        │
└─────────────────────────────────────────────────────────────────┘
      ▼
   Layer D: dataset assembly → SQLite (source of truth)
```

### Micro-RAG query path (`micro_rag/web/`)

```
query ──▶ understandQuery (heuristic: filters + intent, ~0ms)
      ──▶ hybridRetrieve  (structured pre-filter → pgvector semantic
                           + lexical ts_rank → Reciprocal Rank Fusion)
      ──▶ Gate 1  retrieval floor (decline if nothing clears threshold)
      ──▶ generateAnswer (single Ollama call, tagged [record:field])
      ──▶ Gate 2  entailment verify (strip untagged/invalid sentences;
                                     discard answer if >30% stripped)
      ──▶ Gate 3  status propagation (contradicted/unverified fields are
                                      never put in front of the model)
```

- **Filter relaxation** — never silently falls through to an unfiltered search; each relaxed constraint is reported.
- **Failure taxonomy** — distinct, honest messages for out-of-scope / no-match / low-confidence / ungroundable, instead of a hallucinated answer.

---

## Data model

### Pipeline — SQLite (`app/schema.sql`)
Source of truth for everything the pipeline produces.

| Table | Purpose |
| --- | --- |
| `entities` | canonical name + aliases per lead |
| `entity_sources` | raw discovery-feed rows (13F, ADV, etc.) |
| `claims` | the claim ledger — one row per field/question answer, with `status`, `source_class`, `verification_method`, `wave` |
| `decisions` | Layer-1 verdicts (`pursue`/`pursue_low`) + rationale + `thin_reason` |
| `rejections` | rejected leads + `stage` + reason code |
| `enrichment_runs` | per-entity Layer E/V outcome + cost bookkeeping |
| `field_status`, `findings` | validation output (V1–V6) |
| `audit_rejected_values` | release-rule kills (what was blanked and why) |

### Micro-RAG — Postgres + pgvector (`micro_rag/ingest/schema.sql`)

| Table | Purpose |
| --- | --- |
| `records` | one row per qualified entity (typed columns: `entity_type`, `hq_state`, `aum_usd`, `mandates text[]`, principal fields, `outcome`, `source`) |
| `chunks` | 6 facet chunks/record; `embedding vector(384)` with an **HNSW** index + a generated `tsvector` column for lexical rank |
| `provenance` | per-field audit trail the grounding layer cites from (`status` gates what the model may see) |
| `query_log` | every query's parsed filters, retrieved ids, gate decisions, strip fraction — for evaluation |

Two data sources coexist in `records` via a `source` column (`pipeline` vs `csv_import`); each ingest prunes only its own source, and pipeline records win on `record_id` overlap.

---

## Repository layout

```
.
├── app/                     # Python pipeline (Layers 1 / E / V / D)
│   ├── graph.py             #   LangGraph wiring
│   ├── researcher.py        #   researcher lane (ReAct over tools)
│   ├── verdict.py           #   mechanical gates + LLM verdict
│   ├── enrichment.py        #   waves -1..2 + reserve-pool orchestration
│   ├── validation.py        #   V1/V4/V5/V6, cross-class rule, release rule
│   ├── dataset.py           #   Layer D assembly
│   ├── db.py / schema.sql   #   SQLite storage
│   ├── tools/               #   EDGAR, Serper, Hunter, GDELT, DNS, key rotation
│   └── llm.py               #   Ollama Cloud / Anthropic chat models
├── micro_rag/
│   ├── ingest/              # Python: SQLite/CSV → Postgres+pgvector
│   │   ├── build_records.py #   claim ledger → record row
│   │   ├── chunks.py        #   6-facet chunker (+ contact-leak guard)
│   │   ├── embeddings.py    #   local MiniLM embeddings
│   │   ├── ingest.py        #   pipeline-record ingest
│   │   ├── ingest_csv.py    #   external CSV ingest (co-resident source)
│   │   └── schema.sql       #   records / chunks / provenance / query_log
│   └── web/                 # Next.js 16 app (retrieval + grounding + UI)
│       ├── app/api/         #   /query, /health, /record/[id]
│       ├── lib/             #   retrieval, grounding, ollama, embeddings, ...
│       ├── components/      #   SearchApp, ProvenanceDrawer
│       └── scripts/         #   fetch-model.sh
├── tests/                   # 242 offline tests (pytest)
├── vendor/                  # vendored LinkedIn scraper (Scrapy)
├── docs/                    # design specs (the "why" behind each layer)
├── run_enrichment.py        # driver: Layer E/V/D over pursue/pursue_low leads
├── run_layer1_next.py       # driver: Layer-1 triage over the next N leads
└── requirements.txt
```

---

## Getting started

### Prerequisites
- Python 3.12
- Node.js 20+ (for the RAG web app)
- PostgreSQL 16/17 with the `pgvector` extension — Supabase, Neon, or `pgvector/pgvector` in Docker (RAG only)

### 1. Pipeline

```bash
pip install -r requirements.txt
cp .env.example .env            # fill in keys (Serper, Hunter, Ollama, ...)

python run_layer1_next.py 200   # triage the next 200 discovery leads (Layer 1)
python run_enrichment.py        # enrich/validate/assemble the qualified leads
```

### 2. Micro-RAG

```bash
# ingest (Python)
export DATABASE_URL=postgresql://...          # Postgres + pgvector
python micro_rag/ingest/ingest.py             # pipeline records
python micro_rag/ingest/ingest_csv.py         # + external CSV (optional)

# web app
cd micro_rag/web
npm install
bash scripts/fetch-model.sh                   # one-time: ~23MB embedding model
cp .env.local.example .env.local              # DATABASE_URL + OLLAMA_API_KEY
npm run dev                                    # http://localhost:3000
```

Deploy: `vercel deploy --prod` with `DATABASE_URL` + `OLLAMA_API_KEY` in the project env. The ONNX embedding model is force-traced into the serverless function via `next.config.ts`.

---

## Testing

```bash
PYTHONPATH=. python -m pytest -q     # 242 passing, fully offline
```

The LLM layer falls back to a deterministic fake model when no API key is set, so the suite needs no network or credentials.

---

## Notable engineering decisions

- **Correctness caught by reading real output, not just tests.** Several integrity bugs — an identity gate matching on claim *status* rather than answer polarity; a citation-tagging gate splitting on abbreviation periods and orphaning tags — were found by auditing the actual shipped rows, then locked down with regression tests.
- **Grounding is mechanical.** The entailment gate is plain code over `[record:field]` tags resolved against a provenance fact-sheet — prompt instructions don't count toward it.
- **Idempotent, re-judgeable enrichment.** Completed entities aren't re-processed by default; a `force` flag re-judges them when gate logic changes, so a policy change doesn't silently replay stale outcomes.
- **Latency, measured not guessed.** Per-phase timing showed retrieval at 0.75s and the LLM calls dominating; the query-understanding LLM call was replaced with a deterministic parser and the reasoning model swapped for a plain one, cutting query time ~50s → ~15s.

## Known limitations

- **RAG latency floor (~15s/query)** sits entirely in the single Ollama Cloud generation call; a faster inference provider (e.g. Anthropic, already supported) would bring it to a few seconds.
- **Superlative/aggregate queries** ("largest AUM") use hybrid retrieval, which returns a *relevant* set rather than a true `MAX`; the structured aggregate path exists but isn't always routed to.
- **Discovery is upstream and out of scope** — the pipeline qualifies leads it's handed; sourcing the discovery queue is a separate concern.

---

*Design specifications for each layer live in [`docs/`](./docs).*

## License

MIT

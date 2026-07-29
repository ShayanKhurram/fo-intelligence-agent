# Research Layer — Implementation Plan (Parser → Supervisor → Researchers → Verdict)

Reference architecture: LangChain `open_deep_research` (ODR), as vendored in the repo you linked.
ODR is built to research **one open-ended topic and write a prose report**. This layer processes
**many leads against a fixed question battery and emits a structured verdict**. That difference
drives every adaptation below.

---

## 1. What we keep from ODR vs. what we change

| ODR component | Verdict | Why |
|---|---|---|
| Two-node supervisor (`supervisor` plans → `supervisor_tools` executes) | **Keep** | Clean separation of reasoning from side effects; battle-tested |
| `think_tool` reflection before delegate/finish decisions | **Keep** | Measurably improves multi-hop judgment; you already drew it |
| Parallel researcher subgraphs, concurrency cap + overflow handling | **Keep** | ODR caps spawns at `max_concurrent_research_units` and queues overflow — copy this |
| Per-researcher compression before returning to supervisor | **Keep, change output type** | ODR compresses to prose; we compress to a structured **ClaimSet** or provenance dies |
| Separate state classes (AgentState / SupervisorState / ResearcherState) | **Keep** | Matches the three state boxes in your diagram exactly |
| Token-limit retry + iteration caps | **Keep** | Free robustness |
| `clarify_with_user` node | **Drop** | No human in the loop; leads come from the triage queue |
| `write_research_brief` (LLM call) | **Replace with deterministic Parser** | Our brief is assembled from the DB, not inferred from chat. Zero LLM cost, fully reproducible |
| Open-ended topic decomposition by supervisor | **Constrain to 3 fixed lanes** | Qualification is a known shape; free-form decomposition wastes budget and drifts |
| `final_report_generation` (prose report) | **Replace with Verdict node** | Output is `pursue / pursue_low / reject` + gate results, not an essay |
| Nothing (ODR runs once per query) | **Add: batch runner** | We run this graph ~150 times with shared cache, checkpoints, global budget |

---

## 2. Graph topology

```
[triage queue]
      │  (one lead at a time, N leads concurrently via batch runner)
      ▼
  Parser  (deterministic, no LLM)
      │  LeadBrief
      ▼
┌──────────────── supervisor subgraph ────────────────┐
│  supervisor  ──►  supervisor_tools  ──► (loop)      │
│     tools: think_tool, ConductResearch(lane, …),    │
│            ResearchComplete                         │
│                                                     │
│  ConductResearch spawns researcher subgraphs        │
│  in parallel (max_concurrent_research_units)        │
│                                                     │
│  ┌── researcher subgraph (per lane) ──┐             │
│  │ researcher ──► researcher_tools ──►│ (loop)      │
│  │        └──► compress_to_claims ────┘             │
│  └─────────────────────────────────────┘            │
└─────────────────────────────────────────────────────┘
      │  ClaimLedger (all lanes merged)
      ▼
  Verdict  (gate evaluation → pursue / pursue_low / reject)
      │
      ├─ pursue / pursue_low ──► layer-2 queue  (full ClaimLedger travels with it)
      └─ reject ──────────────► rejections table (reason_code + evidence)
```

The "Iff Pass Send All Research" box in your diagram = Verdict. One correction to its
semantics: **rejects also send their research** — to the rejections table, not forward.
That log is the "what we refused to ship" audit artifact. Nothing is discarded either way.

---

## 3. State models

```python
# state.py — mirrors ODR's three-level state split

class Claim(BaseModel):
    question_id: str            # e.g. "G1.Q3"  (gate.question)
    answer: str
    status: Literal["confirmed", "could_not_verify", "contradicted"]
    source_url: str | None
    source_class: str | None    # which authority class the evidence came from
    retrieved_at: datetime | None
    confidence: Literal["high", "medium", "low"]

class LeadBrief(BaseModel):                 # Parser output
    entity_id: str
    canonical_name: str
    aliases: list[str]
    injected_facts: dict                    # 13F deltas, ADV flag, 5500 headcount,
                                            # conference sightings, source_class_count
    questions: list[QuestionSpec]           # the battery, with gate + unknown-policy
    budget: LeadBudget                      # max_tool_calls, max_iterations, max_usd

class SupervisorState(TypedDict):           # per-lead
    lead_brief: LeadBrief
    supervisor_messages: Annotated[list, add_messages]
    lanes_dispatched: dict[str, int]        # lane -> times run (re-dispatch cap)
    claims: Annotated[list[Claim], operator.add]
    calls_spent: int
    iterations: int

class ResearcherState(TypedDict):           # per-lane, isolated context
    lane: str
    instructions: str                       # supervisor's task for this lane
    lead_brief_slim: dict                   # name, aliases, injected facts only
    researcher_messages: Annotated[list, add_messages]
    raw_notes: list[str]
    tool_calls_used: int
```

ODR accumulates prose `notes` via reducers; we accumulate `Claim` objects the same way
(`operator.add` reducer). Raw notes stay inside the researcher subgraph and never
reach the supervisor — only compressed claims cross that boundary. That is ODR's
context-isolation insight, kept intact.

---

## 4. Node specs

### 4.1 Parser (deterministic — replaces ODR's clarify + write_research_brief)

No LLM. Assembles the LeadBrief:

1. Load entity + all `entity_sources` rows + candidate raw payloads.
2. Compute injected facts from the DB: ADV present? (+ client counts), 13F value +
   quarter-over-quarter deltas, 5500 participant count, conference sightings,
   discovery-class corroboration count, domain/MX check result.
3. Attach the question battery. Each `QuestionSpec` carries:
   `{question_id, text, gate: HARD|SOFT, on_unknown: reject|ship_with_label|deprioritize}`.
4. Attach budget (defaults: `max_tool_calls=8`, `max_iterations=2`, per-lane cap 5).
5. Pre-answer what the DB already answers: e.g. ADV question gets a `Claim` with
   `source_class="adv_index"` before any agent runs. The supervisor starts with
   those claims already in state — it should never spend a call on them.

### 4.2 supervisor (LLM node)

System prompt contains: role, the three lanes and what each covers, gate semantics,
current claims so far, remaining budget, and the rule **"dispatch only lanes whose
HARD-gate questions are still unanswered."**

Tools (same triad as ODR):
- `think_tool(reflection)` — forced first call on every supervisor turn (ODR pattern)
- `ConductResearch(lane, instructions)` — lane ∈ {identity_and_type, people, activity_signals}
- `ResearchComplete()` — allowed only when every HARD question has status ≠ unanswered,
  or budget exhausted (then Verdict handles unknowns per `on_unknown` policy)

### 4.3 supervisor_tools (execution node)

Copy ODR's mechanics directly:
- Collect `ConductResearch` calls from the last AI message.
- Run up to `max_concurrent_research_units` (set to 3 — one per lane) via
  `asyncio.gather`; overflow queues to next iteration (ODR does exactly this).
- Each returns a ClaimSet → merge into `claims`, increment `lanes_dispatched`.
- Re-dispatch rule: a lane may run at most **2×** (initial + one refinement). ODR's
  default `max_researcher_iterations` is higher because it does open research; we
  are qualifying, not writing a dissertation.
- On `ResearchComplete` or caps hit → route to Verdict.

### 4.4 researcher (per-lane subgraph)

ODR's researcher → researcher_tools loop, with lane-scoped toolsets:

| Lane | Answers | Tools |
|---|---|---|
| `identity_and_type` | exists? current name? FO affirmative evidence? SFO/MFO read? RIA-in-costume? defunct? | `edgar_search`, `web_search`, `fetch_page`, `think_tool` |
| `people` | named decision-maker? findable at all? title recency? | `web_search` (incl. SERP x-ray), `fetch_page`, `linkedin_lookup` (tiered), `think_tool` |
| `activity_signals` | deploys capital? recent exits/hires/commitments? scandal check? | `news_search`, `web_search`, `think_tool` |

Tool implementations and setup: see §4.7.

Caps per researcher: `max_react_tool_calls=5`, hard timeout 120s. On cap: stop and
compress whatever exists — a partial lane is a valid result.

**Search-result summarization:** ODR summarizes raw search results with a cheap model
before they enter researcher context. Keep this, with one added rule: the summarizer
must preserve source URLs and any dates verbatim. Those two fields are the provenance
payload; a summary that drops them is corrupt.

### 4.5 compress_to_claims (per researcher — the biggest divergence from ODR)

ODR compresses findings to cited prose. Here, compression emits **structured claims
only** — one `Claim` per battery question the lane touched, plus optional
`extra_findings` for surprises worth keeping.

Rules enforced in the prompt and validated in code (Pydantic, retry once on parse fail):
- Every claim carries `source_url` or explicitly `status="could_not_verify"`.
- No claim without a `question_id`. No prose blobs.
- Contradictions between sources → `status="contradicted"` with both URLs in the answer,
  never silently resolved. The Verdict node — not a compressor — decides what
  a contradiction means.

### 4.7 Tool stack — all free, self-hosted or keyless

No paid vendors anywhere in this layer. Six tools total.

| Tool | Implementation | Cost | Key needed |
|---|---|---|---|
| `web_search` | OrioSearch `POST /search` | free, self-hosted | no |
| `fetch_page` | OrioSearch `POST /extract` | free, self-hosted | no |
| `news_search` | GDELT DOC 2.0 API | free | no |
| `edgar_search` | SEC EDGAR full-text + submissions API | free | no (UA header required) |
| `nonprofit_lookup` | ProPublica Nonprofits API | free | no |
| `linkedin_lookup` | `python-scrapy-playbook/linkedin-python-scrapy-scraper` | free | no |
| `think_tool` | no external call | free | — |

#### OrioSearch — covers `web_search` + `fetch_page`

Self-hosted SearXNG meta-search + FastAPI + Redis, with a **Tavily-compatible response
schema**. Since ODR defaults to Tavily, this is close to a drop-in.

```bash
git clone https://github.com/vkfolio/orio-search
cd oriosearch
docker compose up --build
```

Three services come up: API (`:8000`), SearXNG (`:8080`), Redis.

Endpoints used:
- `POST /search` → `{query, topic, max_results}` → results with `title`, `url`, `content`, `score`
- `POST /extract` → page content extraction (this is our `fetch_page`; no Firecrawl needed)
- `GET /tool-schema` → OpenAI-compatible function definitions, pass straight to the model as tools
- `GET /health` → wire into the batch runner's preflight

`config.yaml` settings that matter here:

```yaml
llm:
  enabled: false          # IMPORTANT — we do NOT want synthesized answers.
                          # Researchers need raw results + URLs for provenance.
                          # A synthesized answer with no source URL is an unusable claim.
search:
  backend: "searxng"
  backend_fallback: true  # falls back to DuckDuckGo when SearXNG is throttled
rate_limit:
  enabled: true
  search_rate: "30/minute"
```

**Redis caching is built in — this replaces the `shared_cache` requirement in §5.** Delete
that line from the runner spec; OrioSearch owns it.

**Known risk:** SearXNG scrapes public engines, and they throttle under concurrency. With
8 leads in flight expect intermittent empty result sets. Keep `backend_fallback: true`,
and if the pilot shows empties, drop batch concurrency to 4–5. Treat an empty search as
`could_not_verify`, never as "no evidence exists."

#### GDELT — `news_search`

Keyless, free, global, dated. Base call:

```
https://api.gdeltproject.org/api/v2/doc/doc
  ?query="<entity name>"
  &mode=ArtList
  &format=json
  &startdatetime=YYYYMMDDHHMMSS
  &maxrecords=75
```

Returns `url`, `title`, `seendate`, `domain`. `seendate` is the field that makes the
activity lane work — it is the dated part of every recency claim.

#### SEC EDGAR — `edgar_search`

Reuse the connector already built for discovery; expose it to the agent with two methods:
- full-text search over filings (entity name, alias, principal surname)
- `https://data.sec.gov/submissions/CIK##########.json` for filing history / current name

`User-Agent` with a real contact email is mandatory or SEC blocks you. **Verify the
full-text endpoint path before wiring it** — same rule as every other endpoint in
`sources/ENDPOINTS.md`.

#### ProPublica Nonprofits — `nonprofit_lookup`

`https://projects.propublica.org/nonprofits/api/v2/search.json?q=<name>` then org detail.
Keyless. Used for foundation/officer overlap corroboration only.

#### `linkedin_lookup` — tiered, never primary

Scrapy scraper, wired into the `people` lane as **tier 2 only**:

1. **Tier 1:** SERP x-ray via OrioSearch — `site:linkedin.com/in "<name>" "family office"`.
   Snippets frequently contain the current title outright. If Q9 is answered here, stop.
2. **Tier 2:** escalate to `linkedin_lookup` only when tier 1 leaves Q9 unanswered.
3. Cache by profile URL — the same person gets hit by multiple leads.
4. Throttle well below every other tool; it is the fragile dependency.
5. **Degrade, never fail:** on block, error, or empty, the lane returns Q9 as
   `could_not_verify` and continues. A blocked scraper must never kill a lead or stall
   the batch.

LinkedIn blocks unauthenticated Scrapy aggressively, so expect tier 2 to decay over a
long run. That is exactly why it sits behind tier 1 rather than in front of it.

#### Layer 2 note

Email and phone verification were specced against Hunter and Twilio. Both are paid. Free
substitutes exist — MX lookup plus SMTP `RCPT TO` probe for deliverability, and
`libphonenumber` for phone *format* validity. Be honest in the dataset about the
difference: format-valid is not the same claim as line-verified, and labelling it as
though it were is the kind of thing the audit is looking for.

### 4.6 Verdict (replaces final_report_generation)

Hybrid, in this order:
1. **Code first:** evaluate HARD gates mechanically from claim statuses.
   Any HARD gate with `contradicted` or (unanswered + `on_unknown=reject`) → reject
   with reason_code. No LLM needed, no LLM allowed to override.
2. **LLM second:** for survivors, one call with full ClaimLedger → weigh SOFT-gate
   quality (capital plausibility, reachability strength, signs of life) →
   `pursue` vs `pursue_low` + one-paragraph rationale.
3. Write everything: verdict, rationale, gate results, full ClaimLedger →
   `decisions` table. Rejects → `rejections` with reason_code + evidence.
4. `pursue*` leads enqueue for layer 2 **with the ClaimLedger and a dead_ends list
   attached** so layer 2 never re-researches covered ground.

---

## 5. Batch runner (ODR has nothing here — this is new)

```
runner:
  concurrency: 8 leads in flight        # asyncio semaphore
  checkpoint: per-lead status in SQLite (queued → running → verdict_done → failed)
  resume: on restart, re-run 'running' + 'failed' leads only
  global_budget: max_usd hard stop; warn at 75%
  shared_cache: handled by OrioSearch's Redis (§4.7). Add a small local cache only
                for GDELT / EDGAR / ProPublica / LinkedIn responses.
  rate_limits: per-tool token buckets (SEC 10/s shared across ALL leads — enforce
               globally, not per-lead, or 8 concurrent leads will trip the SEC ban).
               Same global treatment for linkedin_lookup, at a much lower rate.
  preflight:   GET orio-search /health before the batch starts; abort early if down
```

Per-lead cost target: ~10–14 LLM calls worst case
(1 parser=0, supervisor ≤3 turns, 3 lanes × (≤5 tool loop + 1 compress), 1–2 verdict).
At ~150 leads this is real money — set `max_usd` from your actual model pricing
before the full run, and do a 10-lead pilot first to measure.

## 6. Model assignment (ODR's role-split, applied)

| Role | Model tier | Why |
|---|---|---|
| Search-result summarization | cheapest | High volume, low judgment |
| Researchers | mid | Tool-use competence needed, per-call volume high |
| Supervisor + Verdict LLM pass | strongest you can afford | Few calls, all the judgment |
| compress_to_claims | mid, structured-output-capable | Schema fidelity matters most |

## 7. Failure handling (extends ODR's)

- Token-limit exceeded → ODR-style retry with truncated context (keep).
- Researcher subgraph crash → lane marked `failed`, supervisor may re-dispatch once
  if budget allows; else Verdict runs with that lane's questions as unanswered.
- Compression parse failure → one retry with error appended; then fall back to
  storing raw notes flagged `uncompressed` and mark affected questions
  `could_not_verify`. Never fabricate claims to satisfy the schema.
- A lead that fails 2 full attempts → status `failed`, logged, skipped. Do not let
  one pathological lead stall the batch.

## 8. Build order

| Step | Deliverable |
|---|---|
| 1 | State models + QuestionSpec battery encoded (gates + on_unknown policies) |
| 2 | Parser + injected-facts assembly; unit test against 3 real entities from your DB |
| 3 | One researcher lane end-to-end (identity_and_type) incl. compress_to_claims |
| 4 | Supervisor two-node loop + parallel dispatch + caps |
| 5 | Verdict node (code gates first, then LLM pass) + decisions/rejections writes |
| 6 | Batch runner: checkpoints, shared cache, global rate limiter, cost meter |
| 7 | 10-lead pilot → inspect ClaimLedgers by hand → tune prompts/caps → full run |

Step 7 is not optional. The pilot is where you discover the supervisor dispatching
lanes it doesn't need, compressors dropping URLs, or budgets set wrong — at 10-lead
cost instead of 150-lead cost.

---

## 9. Remaining cost

Tools are free (§4.7). The only spend in this layer is model inference. If that also
needs to be zero, OrioSearch's own config shows the pattern — point the LLM roles at a
local Ollama endpoint. Expect a quality drop most visibly at the supervisor and Verdict
nodes, which is where judgment concentrates; the researcher and summarization roles
tolerate smaller models much better. Measure it in the 10-lead pilot rather than
assuming either way.

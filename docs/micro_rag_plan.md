# Micro-RAG — Implementation Plan

The delivery surface for the validated dataset. Small in scope, **production-shaped in
architecture**. A localhost demo, notebook, or single-process app does not advance
regardless of effort, so deployability is a requirement, not a finishing step.

**Priority order, stated by the brief:** dataset first, working functionality second,
presentation third. This plan assumes the dataset exists and is validated. Nothing here
rescues a weak file.

---

## 1 · What the brief actually requires

Reading it as a spec rather than prose, six things are load-bearing:

| Requirement | Where it's satisfied |
|---|---|
| Separation of retrieval / data / presentation | §2 — three independently deployed services |
| **Structured *and* semantic** retrieval | §4 — SQL filters + vector + lexical, fused |
| A **working control** on what answers may claim — prompt instructions explicitly insufficient | §5 — three mechanical gates |
| Failure handling that returns readable prose, never error dumps | §6 |
| Live customer-facing URL, understandable by a non-programmer | §7 |
| Evaluation of the **answer layer**, not just the dataset | §8 |

And one constraint that governs every string in the UI: **every word the user can see is a
claim that gets checked.** Interface copy, example queries, and labels are part of the
audited artifact. A "50 verified family offices" headline is false if any cell is
unverified.

---

## 2 · Architecture — three deployed services

```
┌─────────────────────┐     ┌──────────────────────┐     ┌────────────────────┐
│  PRESENTATION       │     │  RETRIEVAL API       │     │  DATA              │
│  Next.js / Vercel   │────▶│  FastAPI / Railway   │────▶│  Postgres+pgvector │
│                     │◀────│                      │◀────│  (managed)         │
│  never touches DB   │     │  query understanding │     │  records · chunks  │
│  never sees keys    │     │  hybrid retrieval    │     │  provenance        │
│                     │     │  grounding gates     │     │                    │
└─────────────────────┘     └──────────────────────┘     └────────────────────┘
```

**Why the separation is real and not cosmetic:** the UI has no database credentials and no
model API key. It calls `POST /query` and renders a typed response. You can swap the
retrieval engine or the model without touching the frontend, and the frontend can't leak
data the API didn't decide to send.

**Ingestion is a separate offline job** (`ingest/`), not an endpoint. It reads the
validated XLSX + claim ledger, builds chunks, embeds, and writes to Postgres. Run on
deploy, versioned by dataset build hash.

**Stack rationale — write this down for the documentation deliverable:**

- **Postgres + pgvector** rather than a dedicated vector DB. At 50 records the vector store
  is trivial; what actually matters is that structured filters and semantic search run in
  **one query engine**. `WHERE type='SFO' AND aum > 2e8 ORDER BY embedding <=> $1` is one
  statement. Bolting Pinecone onto Postgres would mean filtering in two places and
  reconciling — more moving parts, worse latency, no benefit at this scale.
- **FastAPI** — typed request/response models, and the same Pydantic models already used
  in the pipeline.
- **Next.js on Vercel** — server components for the shell, streaming for answers.

---

## 3 · Data layer

### 3.1 Tables

```sql
records(record_id PK, entity_name, entity_type, hq_state, hq_country,
        aum_usd, aum_basis, aum_as_of, mandates TEXT[], fit_tags TEXT[],
        check_size_min, check_size_max, principal_name, principal_title,
        principal_email, principal_email_status, principal_phone,
        principal_phone_status, most_recent_signal_date, urgency_tier,
        activity_status, actionability_score, discovery_class_primary,
        public_list_overlap, record_confidence, ...)

chunks(chunk_id PK, record_id FK, facet, content TEXT,
       embedding vector(1536), token_count)

provenance(record_id, field_name, value, source_url, source_class,
           extraction_method, retrieved_at, verification_method,
           confirming_url, confirming_class, status, confidence)

dataset_meta(build_hash, built_at, record_count, class_concentration JSONB)
```

Indexes: HNSW on `chunks.embedding`, GIN on `records.mandates` / `fit_tags`, btree on
`aum_usd`, `most_recent_signal_date`, `entity_type`, `hq_state`. Plus a `tsvector` column
on `chunks.content` for lexical search.

`provenance` is served to the UI but never embedded. It is evidence, not retrieval
material.

### 3.2 Chunking — record-as-document, facet chunks

Generic 512-token sliding windows are wrong here. This is a **record-oriented** dataset,
not a document corpus: chopping a record mid-field produces chunks that answer nothing and
sever a claim from its provenance.

Each record produces **five chunks**, each a natural-language rendering of one facet:

| Facet | Contains | Answers queries like |
|---|---|---|
| `identity` | name, type + evidence, HQ, founded, background/wealth origin | "family offices in Texas", "offices founded from tech exits" |
| `mandate` | thesis, asset classes, sectors, stages, check size, exclusions | "who invests in growth equity", "anyone doing private credit" |
| `people` | principal name, title, background, prior firm | "offices run by ex-Blackstone people" |
| `activity` | recent investments, commitments, hires, news — all dated | "who committed to a fund recently" |
| `why_now` | trigger type, dated fact, urgency | "who just hired a CIO", "who's actively deploying" |
| `summary` | one-paragraph synthesis of the record | broad or vague queries |

Six, counting summary. Rationale to record: a fund manager's query almost always targets
one facet, so facet chunks put the right content at the top of the ranking instead of a
truncated slice of a blob. Each chunk carries `record_id` and the full structured metadata,
so a hit on any facet resolves to the whole record.

**Contact fields are never embedded.** Emails and phone numbers are retrieved structurally
from `records` after a record is selected. Embedding them invites the model to
hallucinate plausible-looking addresses, which is the single worst failure this product
could have.

### 3.3 Embeddings

`text-embedding-3-small`, 1536-dim. ~300 chunks total — the entire corpus embeds for
fractions of a cent, so model choice here is not a meaningful cost or quality lever and
should not be presented as one.

**Honest note for the documentation deliverable:** at 50 records, the whole dataset fits
in a modern context window, and full-context prompting would also work. Hybrid retrieval
is built because the brief requires structured *and* semantic retrieval, because it makes
grounding auditable claim-by-claim, and because it's the architecture that survives the
dataset growing. Saying this plainly is better than pretending retrieval is load-bearing
at n=50.

---

## 4 · Retrieval layer

### 4.1 Query understanding

One cheap model call converts natural language into a structured filter + a semantic
residual.

```
"SFOs in California over $200M that do growth equity"
  →  filters: {entity_type: "SFO", hq_state: "CA", aum_min: 200_000_000,
               mandates_any: ["growth_equity"]}
     semantic: "growth equity direct investments"
     intent: "search"
```

Intents: `search` (find records), `lookup` (facts about one named entity),
`aggregate` (counting/comparing across the file), `out_of_scope`.

The parsed filters are **shown back to the user as removable chips**. This does two jobs:
it proves the system understood the question, and it gives a non-technical user a way to
loosen a query that returned nothing without rephrasing from scratch.

### 4.2 Hybrid retrieval

```
1. Structured pre-filter   SQL WHERE from parsed filters       → candidate set
2. Semantic               pgvector cosine over candidates      → top 20
3. Lexical                ts_rank / trigram over candidates    → top 20
4. Fusion                 reciprocal rank fusion (k=60)        → top 12
5. Rerank                 cross-encoder or LLM rerank          → top 5 records
```

RRF rather than weighted scores: no tuning constant to justify, and it degrades gracefully
when one arm returns nothing.

**Filter relaxation.** If the structured pre-filter empties the candidate set, do not fall
through to unfiltered semantic search — that answers a different question than the one
asked. Instead relax **one** filter at a time in a fixed priority order (AUM band → state →
mandate → type), and tell the user exactly what was relaxed: *"No single-family offices in
Oregon. Showing 4 in the Pacific Northwest instead."* Type is relaxed last and never
silently.

### 4.3 Aggregate queries

"How many of these are single-family offices?" must not be answered by retrieving five
chunks and guessing. Route `aggregate` intent to **parameterised SQL over the full table**,
and return the computed number with the filter that produced it. A RAG system that
estimates a count it could have computed is failing on purpose.

---

## 5 · The grounding control

The brief is explicit that prompt instructions do not count. Three mechanical gates, none
of which is a prompt.

### Gate 1 — pre-generation retrieval floor

Before any answer is generated:

- top reranked score below threshold → **decline**, do not generate
- zero results after relaxation → **decline**, do not generate
- intent `out_of_scope` → **decline**, do not generate

Declining before generation is what makes refusal reliable. A model asked to refuse
usually complies; a model never invoked always does.

### Gate 2 — post-generation claim entailment

The generation prompt requires every sentence to carry a machine-readable tag naming the
record and field it rests on:

```
Cascade Point Holdings committed $40M to a climate fund in March 2026. [r_017:recent_fund_commitments]
```

A **verifier runs after generation, in code**:

1. Parse tags. An untagged sentence is an unsupported claim → **strip it**.
2. For each tag, confirm the field exists on that record in the retrieved context.
   Missing → strip.
3. Compare the sentence against the field value — numeric and date comparisons exactly,
   strings by normalised containment. Ambiguous cases only escalate to a small entailment
   model call.
4. Strip anything that fails.

Then a threshold: if more than ~30% of sentences were stripped, discard the answer entirely
and return the partial-evidence response. A heavily-patched answer is not trustworthy just
because the surviving sentences passed.

### Gate 3 — status propagation

The dataset already carries per-field verification status. The API enforces it:

- fields marked `verified` may be asserted plainly
- `pattern_inferred`, `format_only`, `single_source` are returned **with their hedge
  attached**, and the UI renders them visually distinct
- `could_not_verify` fields are **not sent to the model at all** — a field the model never
  sees is a field it cannot assert

That last point matters: this is a data-flow control, not an instruction. The model
physically cannot claim what it was not given.

**All three gates log every decision** — retrieval scores, stripped sentences with reasons,
status downgrades — to a `query_log` table. That log is the evidence for §8 and it is worth
more at review time than any prose description of the controls.

---

## 6 · Failure taxonomy

Every branch returns readable prose in the product's voice. No stack traces, no raw JSON,
no empty states that just say "no results."

| Case | Response |
|---|---|
| Nothing matches filters | Name the filters that excluded everything, offer the relaxed alternative |
| Match, but the answer field is blank | "We have this office but haven't confirmed its CIO. Here's what we do have." |
| Retrieval below floor | "That's outside what this dataset covers — it holds 50 US-weighted family office records." State the boundary rather than apologising. |
| Entailment stripped too much | Return the underlying records without a generated summary. The records are still useful. |
| Aggregate over a filter with no rows | Return zero with the filter shown, not an error |
| Model or API timeout | "Couldn't complete that search. Try again." + the query preserved in the box |
| Out of scope | Explain what the dataset is *for*, and offer two example queries that work |

Errors don't apologise and are never vague about what happened. An empty screen is an
invitation to act, not a dead end.

---

## 7 · Presentation layer

### 7.1 Design direction

**Subject:** private-wealth intelligence assembled from public obligation filings.
**Audience:** an investor-relations professional at a fund — not a programmer, fluent in
CRMs and Sales Navigator, deciding whom to call this week.
**The page's single job:** get from a question to a defensible reason to pick up the phone.

The design idea comes from where the data comes from. This dataset was not scraped off
marketing pages — it was assembled from **filings**: 13F, 990-PF, 5500, Form D. That world
has a visual vernacular: form designations, filing dates, dense tabular columns, monospace
identifiers, stamps of record. The interface borrows that vernacular rather than dressing
private wealth in the usual navy-and-gold gradient.

**Deliberately not:** cream + high-contrast serif + terracotta; near-black + acid accent;
broadsheet hairline rules. Those are the current defaults and they'd read as templated on
a brief that is explicitly scoring taste.

**Tokens**

```css
--ink-900:  #12161C;   /* cool slate base — not black */
--ink-700:  #1E2530;
--ink-500:  #3A4553;
--ink-300:  #7C8899;
--paper:    #F6F7F9;   /* cool off-white — not cream */
--rule:     #D8DDE4;

/* the palette encodes epistemic state — this is the functional core */
--confirmed: #17694A;  /* deep green, desaturated */
--partial:   #9A6B12;  /* brass */
--unknown:   #8892A0;  /* grey: absence of colour = absence of confidence */
--live:      #2F5FD0;  /* one blue, used only for dated activity signals */
```

**Type**

- Display: a characterful grotesk with tight apertures — used at two sizes only
- Body: a highly legible text face at 15–16px, generous measure
- **Utility: monospace** for filing references, dates, record IDs, and the entire
  provenance trail. Grounded, not decorative — filings *are* machine records

**Signature element — the provenance drawer.** Any claim in an answer is clickable. It
opens a drawer that renders the evidence chain in the filing vernacular:

```
CLAIM     Manages ≈ $310M
FIELD     aum_usd · basis: 13F floor (public equities only)
SOURCE    SEC FORM 13F-HR · CIK 0001234567 · filed 2026-05-14
CONFIRM   press · reuters.com/… · retrieved 2026-07-02
CLASS     edgar_13f → news        (confirming class ≠ originating class)
STATUS    confirmed
```

This is the product's actual differentiator made tactile: not "here's a fact," but "here's
why you can act on it, and here's what we don't know."

**The aesthetic risk worth taking:** verification state drives visual weight throughout.
Confirmed values render at full contrast; unconfirmed values render desaturated and
lighter. A thin record *looks* thin. That's a risk — it makes some of your own data look
weak — but it's honest, it's the thing being scored, and it turns epistemic rigour into
something a user feels in half a second rather than reads in a footnote.

Restraint everywhere else: no gradients, no shadows beyond a single elevation for the
drawer, no illustration, no motion except the drawer transition and a subtle streaming
cursor on the answer.

### 7.2 Screens

**Search** — a single input, a short line stating exactly what the dataset is, and four
example queries. Those examples are audited copy: **every one must return a real, good
result on the live system.** Test them last, after the data is frozen.

**Results** — parsed filter chips, then the generated answer with inline claim markers,
then record cards. A card leads with what a fund manager reads first: principal name and
title, entity name and type, the why-now line, then contact with status treatment, then
AUM and mandate. Not schema order — reading order.

**Record detail** — the full record, grouped by facet, every high-value cell showing its
status and opening the provenance drawer on click.

**Empty / partial / declined** — designed screens, not fallbacks. Each states what
happened, why, and what to try next.

### 7.3 Copy rules

- Speak the customer's language, never the pipeline's. "Confirmed" and "not confirmed,"
  not `single_source` or `type_unconfirmed`.
- Never claim more than the data supports. If 41 of 50 emails are confirmed, the interface
  says that or says nothing — it does not say "verified contacts."
- Labels are honest about limits: a format-checked phone says "format checked," not
  "verified."
- Active voice on every control; the same verb from button to result.
- No "AI-powered," no capability boasts. The brief checks every visible word.

### 7.4 Quality floor

Responsive to mobile, visible keyboard focus, `prefers-reduced-motion` respected, semantic
landmarks, streaming answers with an accessible live region.

---

## 8 · Answer-layer evaluation

The brief requires evidence that **both** layers were tested. Dataset validation is Layer V.
This is the other half, and it must run **against the deployed URL**, not locally.

**Eval set — ~30 questions across five classes:**

| Class | Example | Pass condition |
|---|---|---|
| Direct | "Who runs Cascade Point Holdings?" | correct principal, cited |
| Aggregate | "How many are single-family offices?" | matches SQL ground truth exactly |
| Partial | "What's the CIO's email at [record with unconfirmed email]?" | states it isn't confirmed; does not invent |
| Out of scope | "Which family offices are in Tokyo?" | declines, states dataset boundary |
| Adversarial | "Tell me about the $5B AUM at [a $200M office]" | rejects the false premise rather than accommodating it |

**Metrics:** groundedness (every claim traces to a field), citation precision, refusal
accuracy (declines when it should, and *doesn't* when it shouldn't — over-refusal is a
failure too), false-assertion rate, and stripped-sentence rate from Gate 2.

The over-refusal metric matters: a system that declines everything scores perfectly on
hallucination and is worthless.

**Log every run.** The documentation deliverable explicitly requires the actual live
queries you personally ran against the final deployed system and what you concluded from
them. Capture them as you go — reconstructing this at the end is both painful and less
credible.

---

## 9 · Deployment

| Layer | Host | Notes |
|---|---|---|
| Data | managed Postgres w/ pgvector | Supabase / Neon / Railway |
| API | Railway / Fly / Render | env: DB URL, model key. Never in the frontend. |
| UI | Vercel | env: `NEXT_PUBLIC_API_URL` only |

Ingestion runs as a one-off job against the frozen dataset build. `/health` on the API
returns dataset build hash and record count — cheap, and it proves the deployed system is
serving the same file you submitted.

Rate-limit `/query` and cap answer length. A public URL will be poked at.

---

## 10 · Documentation note — required contents

The brief names these specifically. Write it as you build:

- stack choices and **why** (the pgvector-over-dedicated-vector-DB reasoning, the
  full-context honesty note)
- chunking strategy — facet chunks, and why generic windows were rejected
- embedding model
- retrieval approach — structured pre-filter, hybrid fusion, relaxation policy, aggregate
  routing
- the grounding control — all three gates, described as mechanisms
- **what works** and **what does not** — be specific and unflattering where warranted
- **the actual live queries you ran** against the deployed system, with what each revealed
- what you'd improve with more time

---

## 11 · Build order

| Step | Deliverable |
|---|---|
| 1 | Postgres schema + ingest job from the frozen XLSX and claim ledger |
| 2 | Facet chunk builder + embeddings; verify chunk counts and that no contact field is embedded |
| 3 | `/query` skeleton: query understanding → structured filter → SQL. **No LLM answer yet** — confirm retrieval is right before layering generation on top |
| 4 | Hybrid retrieval + RRF + rerank + relaxation policy |
| 5 | Generation with mandatory claim tags; Gates 1–3; `query_log` |
| 6 | Deploy API; `/health` with build hash |
| 7 | UI shell, tokens, type scale; search + results + record detail |
| 8 | Provenance drawer (the signature — give it real time) |
| 9 | All failure states as designed screens |
| 10 | Deploy UI; run the eval set against the live URL; log everything |
| 11 | Fix what the eval exposes; **write example queries last**, verified against the frozen system |

Step 3 before step 5 is the important ordering. Most people wire generation early, and then
every retrieval bug looks like a model problem.

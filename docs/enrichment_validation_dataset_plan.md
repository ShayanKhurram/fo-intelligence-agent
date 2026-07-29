# Enrichment → Validation → Dataset — Integrated Implementation Plan

Covers the three layers after research, **and the seams between them.** The seams are the
part that usually breaks: each layer works in isolation and the provenance dies in transit.

---

## 1. Where these sit

```
discovery connectors → resolve → triage
        │
        ▼
LAYER 1  Parser → Supervisor → 3 researcher lanes → Verdict
        │   emits: verdict (pursue | pursue_low | reject), ClaimLedger, thin_reason
        │
        ▼   ── SEAM A ──
LAYER E  Enrichment    wave −1 (derive) → wave 0 (gate) → wave 1 (core) → wave 2 (depth)
        │   appends to the SAME ClaimLedger
        │
        ▼   ── SEAM B ──
LAYER V  Validation    deterministic checks → adjudication → outcome
        │   annotates claims, does NOT rewrite them
        │
        ▼   ── SEAM C ──
LAYER D  Dataset       select 50 (quota-bound) → assemble 6 sheets → emit XLSX + CSV
        │
        ▼
        RAG ingestion (out of scope here)
```

---

## 2. The spine: one ClaimLedger, four writers

**Everything hangs off a single `ClaimLedger` per entity.** Parser seeds it, researchers
append, enrichment appends, validation annotates, the dataset layer renders it. No layer
invents its own record format; if it did, provenance would have to be re-derived and it
would silently degrade at each hop.

### Required change to the existing `Claim` model

```python
class Claim(BaseModel):
    claim_id: str
    question_id: str | None        # existing — layer 1 battery
    field_name: str | None         # ── ADD THIS ──
    answer: Any
    status: ClaimStatus
    source_url: str | None
    source_class: str | None
    extraction_method: str
    retrieved_at: datetime
    confidence: Literal["high", "medium", "low"]
    produced_by: Literal["parser", "research", "enrichment", "derived"]
    wave: str | None               # ── ADD THIS ── "-1" | "0" | "1" | "2"

    # written by validation only, never by producers
    verification_method: str | None = None
    confirming_url: str | None = None
    confirming_class: str | None = None
    verified_at: datetime | None = None
```

`field_name` is the join key to the dataset schema. **Add it now.** Without it, Layer D
becomes a translation exercise from question IDs to columns; with it, Layer D is a
`groupby`. This is the single highest-leverage integration decision in this document.

`ClaimStatus` enum — one vocabulary across all layers, matching the dataset spec:
`verified` · `single_source` · `pattern_inferred` · `format_only` · `could_not_verify` ·
`contradicted` · `removed_failed_validation`

---

## 3. Seam contracts

### SEAM A — Verdict → Enrichment

```python
class EnrichmentInput(BaseModel):
    entity_id: str
    verdict: Literal["pursue", "pursue_low"]
    thin_reason: Literal["fixable", "structural"] | None   # ── ADD to Verdict node ──
    claim_ledger: list[Claim]
    injected_facts: dict          # 13F, ADV, 5500, conference — from Parser
    dead_ends: list[str]          # URLs/queries already tried and empty
    triage_score: int
    discovery_classes: list[str]
```

**Two changes to the existing Verdict node:**
1. Emit `thin_reason` on `pursue_low` — `fixable` (missing AUM/thesis/signals, enrichment
   can fill) vs `structural` (no decision-maker findable, no channel, no deploy evidence).
   Structural-thin is near-unfillable and will die at V6 completeness anyway.
2. Emit `dead_ends`, so enrichment never re-runs a query layer 1 already exhausted.

### SEAM B — Enrichment → Validation

```python
class ValidationInput(BaseModel):
    entity_id: str
    claim_ledger: list[Claim]     # extended, same object
    waves_completed: list[str]
    wave0_findings: list[Finding] # gates already run — do not redo blindly
    budget_spent: BudgetRecord
```

### SEAM C — Validation → Dataset

```python
class DatasetInput(BaseModel):
    entity_id: str
    outcome: Literal["ship", "ship_with_caveats", "reject"]
    claim_ledger: list[Claim]     # now carrying verification fields
    field_statuses: list[FieldStatus]
    findings: list[Finding]
    caveats: list[str]
    chain: list[ChainStep]
    type_final: Literal["SFO", "MFO", "type_unconfirmed"]
```

---

## 4. LAYER E — Enrichment

Ordered by **kill-power**, not by schema order. Each wave is a gate.

### Wave −1 — derive (zero API calls, run on `pursue` AND `pursue_low`)

Pure function over tables you already own. Fills a large slice of the schema for free.

| Derived field | From |
|---|---|
| `aum_usd` + `aum_basis="13f_floor"` + `aum_as_of` | 13F `tableValueTotal` |
| `investing_mandates`, `sector_focus`, `direct_vs_fund` | 13F holdings composition |
| `recent_investments` | 13F quarter-over-quarter deltas |
| `why_now_trigger` (`concentration_pain`, `fresh_liquidity`) | 13F deltas |
| `principal_name`, `principal_title` | FEC occupation, 990-PF officers, conference roster |
| `principal_phone` | Form 5500 sponsor phone |
| `headcount` | 5500 participant count |
| `why_now_trigger` (`access_window`) | conference agenda, future-dated |
| `discovery_class_*` | `entity_sources` table |
| `public_list_overlap` | set intersection vs scraped public list |

Emit as claims with `produced_by="derived"`, `source_class` = the originating connector,
`wave="-1"`.

**Run this before deciding anything about the reserve pool.** Some `pursue_low` records
promote to `pursue` here at zero cost — thin capital evidence and missing principals are
exactly what wave −1 fills.

### Wave 0 — gates (~2 calls)

Reuse the validation layer's deterministic functions, called early on layer-1 data:
V4 contradictions, V5 staleness, domain/MX existence, ADV re-check, firm-is-FO hardening.

This is **not** duplicating Layer V. Same functions, different input: here they run on
pre-enrichment data to kill cheaply; in Layer V they run again on enriched data. Wave 0
findings pass forward in `ValidationInput.wave0_findings` so Layer V can skip re-running
any check whose inputs didn't change.

Fatal → reject immediately, at 2 calls instead of 16.

### Wave 1 — actionability core (~6 calls)

Three things only: **named decision-maker with a role date · one working contact channel ·
one dated signal.**

| Target | Tier 1 | Tier 2 | Tier 3 |
|---|---|---|---|
| decision-maker | derived (wave −1) | site team page (free fetch) | Serper x-ray |
| role currency | Serper x-ray snippet | recent press | linkedin_lookup |
| email | site scrape (free fetch → `/scrape`) | pattern + MX probe | Hunter **domain search** |
| phone | 5500 (derived) | site contact page | registry |
| dated signal | 13F delta (derived) | GDELT | Serper `/news` |

**Credit discipline.** Every Serper `/search`, `/scrape`, and `/news` call spends from one
global pool (`research_layer_plan.md` §4.7). Two rules that matter most in this wave:

- **Free fetch first.** `httpx` + `trafilatura` before Serper `/scrape` on every site
  fetch. Many SFO sites are static single-pagers — the free path handles them, and site
  scraping is the highest-volume fetch consumer in the pipeline.
- **Site team pages often carry JSON-LD** `Person` / `Organization` blocks. When you do
  spend a `/scrape` credit, parse the JSON-LD before the prose — principal name and title
  frequently come straight out of it, which can close wave 1 in one call.

**Hunter budget:** free tier is 50 credits/month. Use domain search (one credit → all
addresses + pattern for a domain), never individual finds. Reserve remaining credits for
verification of the highest-value records only.

**Gate:** no decision-maker OR no contact channel → stop. V6 completeness will reject it
at validation regardless, so wave 2 spend is pure waste.

### Wave 2 — depth (~8 calls, survivors only)

`investing_thesis`, `background`, `check_size_range`, `stage_focus`, `geography_focus`,
`do_not_pitch`, `secondary_contact_*`, `corporate_linkedin`, `principal_linkedin`,
`principal_background`, remaining why-now triggers, `recent_fund_commitments`,
`recent_key_hires`, `recent_news`, `outreach_hook`, `fit_tags`.

Tools: GDELT (→ Serper `/news` fallback), Serper `/search`, `fetch_page`
(httpx+trafilatura → Serper `/scrape`), job postings, EDGAR (Form D for new vehicles).
Full tool stack and credit rules: `research_layer_plan.md` §4.7. No judgment calls — fit
was decided at layer 1. That makes wave 2 parallelizable and tolerant of a cheaper model.

`outreach_hook` is authored by the model from the highest-confidence trigger; if no
trigger exists, leave blank rather than inventing one.

### Reserve pool control flow

```python
def run_pipeline():
    all_leads = load(verdict__in=["pursue", "pursue_low"])
    wave_minus_1(all_leads)                    # free, everyone
    promote_upgrades()                         # pursue_low → pursue where wave −1 filled gaps

    process(load(verdict="pursue"))            # waves 0→1→2, then Layer V
    survivors = count_outcomes(["ship", "ship_with_caveats"])

    while survivors < 50 and reserve_budget_remaining():
        gap = 50 - survivors
        draw = select_reserve(
            n=int(gap * 1.5),
            thin_reason="fixable",             # never draw structural-thin
            order_by=["discovery_class_count", "triage_score"],
        )
        if not draw: break
        process(draw)
        survivors = count_outcomes(["ship", "ship_with_caveats"])
```

Cap reserve spend at ~20% of total enrichment budget. If two draws still can't fill 50,
that's a discovery-pool problem, not an enrichment one — more budget won't fix it.

---

## 5. LAYER V — Validation

Unchanged in substance from the validation plan; two integration adjustments:

**Skip-if-unchanged.** For each check in V4/V5, compare the hash of its input claims
against `wave0_findings`. Unchanged → carry the wave-0 result forward. Changed (enrichment
added or replaced a claim) → re-run. Prevents paying twice for identical work while
guaranteeing enriched data actually gets checked.

**Validation annotates, never rewrites.** It writes `verification_method`,
`confirming_url`, `confirming_class`, `verified_at`, and flips `status` on existing claims.
It does not create new value claims. A validator that produces values is a second
enrichment layer with no oversight.

**Cross-class rule applies across the whole ledger**, including enrichment-produced claims.
`CONFIRMING_CLASSES` must have entries for the enrichment sources — `site_scrape`,
`serper_organic`, `hunter`, `gdelt`, `derived_13f`, `derived_5500`. A derived claim
confirms against a document, not against the derivation. And note `serper_organic` is a
*search index*, not an authority: a Serper result confirms nothing on its own — the page
it points to does, once fetched.

**V1 needs credits that enrichment must not eat.** Source-supports-claim runs a
`fetch_page` per anchor claim — roughly 150 calls across the file — and it runs last in
the pipeline. Ring-fence ~200 Serper credits for it in the global budget counter
(`research_layer_plan.md` §4.7). If enrichment burns the pool, the check that catches
claims whose source URL doesn't support them silently doesn't run, and that is the single
most valuable check in the system. Free-path fetches (httpx+trafilatura) don't draw from
the ring-fence — prefer them here too.

**Release rule enforcement** happens here and is the seam to Layer D: any claim flipping
to `removed_failed_validation` gets written to `audit_rejected_values` **and** its value
blanked in the ledger. Layer D must never see a killed value in a shippable field.

---

## 6. LAYER D — Dataset assembly

### 6.1 Selection (this is where `quota.py` finally runs)

`quota.py` was specced during discovery but it belongs **here** — you can only enforce
final-file class concentration once you know who survived.

```python
def select_50(survivors):
    # ranked, then quota-bound
    ranked = sort(survivors, key=lambda r: (
        r.actionability_score,           # brief scores this explicitly
        r.type_final == "SFO",           # SFOs are the valued prize
        r.verified_cell_count,
        r.urgency_tier_rank,
        r.discovery_class_count,
    ), reverse=True)

    selected, per_class = [], Counter()
    for r in ranked:
        cls = r.discovery_class_primary
        if per_class[cls] >= MAX_PER_CLASS:   # 15 = 30% of 50
            continue
        selected.append(r); per_class[cls] += 1
        if len(selected) == 50: break
    return selected, per_class
```

If quota blocks a strong record, log it — "excluded to preserve source diversity" is a
defensible, documented judgment call, and it's exactly the kind of thing the methodology
should surface.

### 6.2 Assembly — six sheets, all from the ledger

| Sheet | Built from | Transform |
|---|---|---|
| `records` | claims where `field_name` is not null | pivot: one row per entity, one column per `field_name` |
| `provenance` | same claims, unpivoted | one row per (record, field) with source + verification columns |
| `audit_rejected_values` | claims where `status == removed_failed_validation` | direct |
| `rejected_records` | `rejections` table, all stages | direct, with stage column |
| `source_class_report` | `entity_sources` + metrics module | aggregate |
| `data_dictionary` | static + your inclusion standard | hand-written once |

The `records` sheet is literally a pivot of the ledger. That is the payoff for adding
`field_name` in §2.

### 6.3 Emission rules

- Every high-value cell: populated, or blank **with** `could_not_verify` in its status
  column. Never silently empty.
- Column order: identity → type → principal contact → why-now → mandates → signals →
  integrity.
- Emit `records.csv` alongside the XLSX.
- Write `run_manifest.json`: git SHA, run timestamp, connector versions, endpoint
  verification dates, model versions, budget spent. Makes the file reproducible and dates
  every claim in it.

---

## 7. Storage additions

```sql
claims(claim_id, entity_id, question_id, field_name, answer, status,
       source_url, source_class, extraction_method, retrieved_at, confidence,
       produced_by, wave,
       verification_method, confirming_url, confirming_class, verified_at)

enrichment_runs(entity_id, wave, calls_spent, usd_spent, started_at, ended_at, outcome)
field_status(entity_id, field, status, method, confirming_url, confirming_class, last_checked)
findings(entity_id, check_id, claim_id, field, severity, detail, evidence_url)
chain_steps(entity_id, step_no, claim, originating_class, originating_url,
            confirming_class, confirming_url, method, result, timestamp)
audit_rejected_values(entity_id, field_name, rejected_value, reason_code, evidence_url, rejected_at)
production_records(entity_id, selected_at, rank, primary_class, excluded_by_quota bool)
```

`claims` is now the durable spine — persist it, don't hold it only in graph state. Every
sheet, every metric, and the chain deliverable read from this one table.

`extraction_method` matters more than it looks: record `httpx_trafilatura` vs
`serper_scrape` vs `derived_13f` vs `jsonld`. It goes straight into the provenance sheet
as "how it was obtained," and it's also how you measure what share of fetches the free
path actually covered — which tells you whether the credit budget holds for a rerun.

---

## 8. Build order

| Step | Deliverable | Blocks |
|---|---|---|
| 1 | Add `field_name` + `wave` to `Claim`; persist `claims` table; backfill from layer-1 runs | everything |
| 2 | Add `thin_reason` + `dead_ends` to Verdict output | Seam A |
| 3 | Wave −1 derivation module; unit-test against 3 real entities | enrichment |
| 4 | Wave 0 gates (reuse V4/V5 functions) + reject path | enrichment |
| 5 | Wave 1 tiered contact resolution + completeness gate | enrichment |
| 6 | Wave 2 depth enrichment | enrichment |
| 7 | Layer V with skip-if-unchanged + release-rule enforcement | validation |
| 8 | `quota.py` + `select_50` | dataset |
| 9 | Sheet assembly (pivot + unpivot) + XLSX writer + manifest | dataset |
| 10 | Reserve-pool orchestration loop | integration |
| 11 | End-to-end run on 10 entities, inspect all six sheets by hand | — |

Step 11 before the full run, same reason as the earlier pilots. Sheet assembly bugs are
invisible until you open the file.

---

## 9. Integration tests (the seams, not the layers)

- **Provenance survival:** a claim created in wave −1 must appear in `provenance` with its
  original `source_url` intact after passing through validation and assembly.
- **Release rule:** inject an undeliverable email → assert it appears in
  `audit_rejected_values` and the `principal_email` cell in `records` is blank with
  `could_not_verify`.
- **Cross-class:** inject a claim whose only confirmation shares its originating class →
  assert status stays `single_source`, not `verified`.
- **Quota:** feed 60 survivors all from one class → assert selection caps at
  `MAX_PER_CLASS` and logs the exclusions.
- **Reserve loop:** force 45 survivors → assert exactly one draw of ~8 fixable-thin
  records, and that no structural-thin record is ever drawn.
- **Idempotency:** re-run enrichment on a completed entity → assert zero new API calls.

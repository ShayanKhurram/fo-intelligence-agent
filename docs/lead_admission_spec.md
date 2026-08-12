# input.md — Lead Admission Spec

**Authoritative definition of a *preferred lead*.** Every discovery connector, the
pre-triage filter, and the Layer-1 question battery answer to this file. If code and this
file disagree, this file is right and the code is a defect.

**Target:** 500 validated family offices — **250 SFO / 250 MFO** (balanced).

**Core principle:** *exclusion is mechanical and happens before the LLM.* The 2026-07-28 run
put 2,633 13F filers into triage at a 0.7% base rate and shipped `FIRST HAWAIIAN BANK`,
`Canada Post Corp Registered Pension Plan`, and `DAILY JOURNAL CORP` as `pursue_low`. That
was not an LLM failure — those leads should never have reached an LLM. Never spend a token
deciding something a regex or a numeric field already settles.

---

## 1. Definitions

Use these exactly. Do not widen them.

**SFO (single-family office)** — a private entity managing the capital of **one** family,
with no external clients and no offer of services to the public. Typically adviser-exempt
(Advisers Act rule 202(a)(11)(G)-1), so it is *absent* from Form ADV. Evidence of an SFO is
evidence of a private investment operation attached to one named family.

**MFO (multi-family office)** — a firm serving **multiple unrelated** families with an
integrated service model that goes beyond investment management: tax, estate, trust,
governance, concierge, or consolidated reporting. Almost always a registered investment
adviser.

**Not a family office** (regardless of what it calls itself):
- A wealth-management RIA whose service model is portfolio management alone, even if every
  client is high-net-worth. **Serving rich people is not a family office.** This is the single
  most common false positive.
- A bank, trust company, or bank trust department.
- A pension plan, retirement system, endowment, or sovereign fund.
- An asset manager, hedge fund, mutual fund, PE/VC firm, or fund GP.
- A broker-dealer.
- A private foundation. (A foundation *corroborates* an adjacent family office; it is not
  itself the lead. See §6.)

---

## 2. Hard exclusions — mechanical, pre-LLM, non-overridable

Applied to `entity_name_raw` at ingest, before any lead enters the triage queue. A hit here
is a terminal reject written to `rejections` with the reason code shown. **No LLM call, no
enrichment, no exceptions — an institutional disqualifier beats every positive signal,
including a family token in the name.**

| Reason code | Match (case-insensitive) |
|---|---|
| `X.bank` | `\bbank\b`, `bancorp`, `bancshares`, `banc\b`, `\bsavings\b`, `\bthrift\b`, `credit union`, `\bN\.?A\.?$`, `national association`, `trust compan(y\|ies)`, `trust division`, `trust services` |
| `X.pension` | `pension`, `retirement system`, `\bretrmt\b`, `employe\w* retirement`, `superannuation`, `\b401\(k\)\b`, `deferred compensation plan` |
| `X.insurance` | `insurance`, `assurance`, `\bcasualty\b`, `\blife co\b`, `reinsurance` |
| `X.fund` | `mutual fund`, `\bfund,? (inc\|llc\|lp\|ltd)\b`, `\bETF\b`, `index fund`, `closed.?end`, `diversified equity fund`, `\bfund gp\b`, `\bfeeder fund\b` |
| `X.brokerdealer` | `securities,? inc`, `\bbrokerage\b`, `broker.?dealer`, `\bclearing\b` |
| `X.public` | `\bcorp\b` or `\bincorporated\b` **and** entity has an SEC CIK with a 10-K/10-Q filing |
| `X.govt` | `sovereign`, `municipal`, `state of\b`, `commonwealth of\b`, `\bcity of\b`, `\bcounty of\b` |
| `X.foreign` | **Strip all `.` from the name first**, then match `\b(?:BV\|NV\|SA\|SPA\|AG\|AB\|OY\|PLC\|GMBH\|SARL\|PTY LTD\|A/S)\b` — **US-only target; drop if scope widens** |
| `X.university` | `university`, `college`, `endowment`, `school district` |

Structural disqualifiers, same terminal treatment:

| Reason code | Rule |
|---|---|
| `X.bd_affiliated` | Form ADV `6A(1) == 'Y'` (broker-dealer). Zero of the 123 confirmed family offices in the 2026-05-01 ADV snapshot had this set. |
| `X.retail_book` | Form ADV: reject when `5D(a)(1) > 5D(b)(1)` (more ordinary-individual clients than high-net-worth ones), **unless** `5F(2)(c) >= $1B`. Ratio, never absolute count — see below. |
| `X.13f_only` | Origin class is `13f_filing` **and** no corroborating family-office evidence from a second source. See §4. |
| `X.defunct` | ADV `SEC Current Status` is terminated/withdrawn, or 5500 plan year older than 5 years with no later filing. |

> **`X.retail_book` must be a ratio.** An absolute "more than 100 retail clients" cut was
> implemented, run, and reverted on 2026-08-13: it dropped `PATHSTONE` ($110B RAUM, 1,834 HNW
> clients, 145 retail), `STOKES FAMILY OFFICE` ($3.35B) and `FUSION FAMILY WEALTH` ($1.44B).
> Every large MFO carries some non-HNW individuals — family members below the threshold,
> childrens' trusts — so an absolute threshold penalises precisely the biggest and most certain
> offices. By ratio, Pathstone is 7% retail while `TEXAS FAMILY WEALTH` is 99% (1,060 vs 12),
> which is the distinction that matters. Scale is itself MFO evidence, hence the $1B override.

> **`multi-family` needs `office` after it.** A bare `multi.?family` alternative matches
> **multifamily real estate** — it admitted `BRIDGE INVESTMENT GROUP` ($49.3B), `GID
> MULTIFAMILY INVESTMENT MANAGEMENT` ($5.9B) and `LARAMAR MULTI-FAMILY VALUE MANAGER` on
> 2026-08-13. Applies to every source's name regex, not just ADV.

> **Regex trap:** `\bB\.V\.\b` and `\bN\.V\.\b` **never match** — `\b` after a trailing period
> requires a following word character, so a name ending in `B.V.` fails. `Achmea Investment
> Management B.V.` and `ASR Vermogensbeheer N.V.` both slipped through on 2026-07-28 for this
> reason. Always strip `.` before token-matching suffixes.

### Kill-list validation (measured 2026-08-12, not estimated)

| Check | Result |
|---|---|
| False positives against the 123 confirmed ADV family offices | **0** |
| False positives against the 97 distinct FEC leads | **0** |
| Of the 52 bad survivors from 2026-07-28, caught by name rules | 17 (33%) |
| Of those 52, origin class `edgar_13f` | **51 (98%)** |

Read that last row carefully: **`X.13f_only` is the load-bearing rule, not the name list.**
Removing 13F from discovery eliminates essentially the entire false-positive population on its
own. The name kill list is a safe second layer — it over-excludes nothing — but it catches only
a third of the noise by itself. Do not implement the name list and consider the job done.

The 35 that pass the name list are mostly bank *holding* companies whose names contain no bank
token (`FIRST HORIZON CORP`, `First Citizens Financial Corp`), asset managers (`AEGON USA
Investment Management`), and public companies (`DAILY JOURNAL CORP`). These are caught by
`X.13f_only` and `X.public`, both structural — which is the point.

> **Field trap:** do **not** use ADV `5C(1)` as a client count. It is `0` for 74% of filers
> (12,502 / 16,779) and its "if more than 100" overflow column is populated for exactly 2
> rows. Real per-category client counts are `5D(x)(1)`; per-category RAUM is `5D(x)(3)`;
> total RAUM is `5F(2)(c)`.

---

## 3. Positive admission — what earns a slot

A lead is admitted to triage only if it clears §2 **and** satisfies the rule for its type.

### SFO admission
Requires **both**:
1. **A family anchor** — a surname or family name bound to the entity (`Pritzker`, `Walton`,
   `Dell`), or an explicit `family office` / `family capital` / `family holdings` token.
   A generic string with no family attached (`"FAMILY OFFICE"`, seen verbatim in the current
   FEC pull) is **not** an anchor — reject as `X.no_anchor`.
2. **An operating signal** — at least one of: a 5500 filing with 2–50 participants under
   NAICS `523*` / `525*` / `5511*`; an FEC employer string naming the office; a
   13D/13G filing; a Form D; a website describing an investment operation.

**Absence from Form ADV is supporting evidence for an SFO, never a discovery signal on its
own.** Every dentist in America is also absent from ADV. It only carries weight *after* an
operating signal is established.

### MFO admission
Requires **all three**:
1. Registered investment adviser (present in the ADV bulk file with an active status).
2. Self-describes as a family office / multi-family office in its own words — legal name,
   primary business name, website, or ADV brochure.
3. Serves **multiple unrelated families**, and is not disqualified by `X.retail_book`.

> **Do not attempt a structural-only MFO filter.** It was tested against the 2026-05-01
> snapshot and it does not work. Profiling the 123 confirmed offices against all 16,779
> advisers: median total clients 124 vs 94, median AUM/client $4M vs $3M — no separation.
> Only HNW RAUM share separates (0.88 vs 0.66) and it is far too weak to gate on; tuned
> thresholds returned `NORTHSTAR FINANCIAL PLANNING` and `CENTER FOR FINANCIAL PLANNING INC`.
> Form ADV does not encode the service-model distinction that defines an MFO. MFO candidates
> must be **name- or directory-seeded, then ADV-verified** — never ADV-discovered.

---

## 4. Per-source admission rules

| Source | Role | Admission rule |
|---|---|---|
| **FEC bulk** (`indiv{YY}.zip`) | **Primary SFO engine** | Regex the `EMPLOYER` field (index 11 of 21, pipe-delimited, no header; `NAME`=7, `CITY`=8, `STATE`=9, `OCCUPATION`=12). Require a family anchor per §3. Normalize and cluster before counting — see §5. |
| **DOL 5500** | SFO backfill + identity | NAICS `523*`/`525*`/`5511*`, participants 2–50, family anchor in sponsor name. Supplies EIN + address + headcount, which FEC lacks. |
| **Form ADV bulk** | **Verifier, not discoverer** | `iaMMDDYY.zip` from `/files/investment/data/other/information-about-registered-investment-advisers-exempt-reporting-advisers/` (the `/files/data/frequently-requested-foia-...` path 404s for recent snapshots). 448 columns, latin-1, CSV. Use to confirm CRD, legal name, address, RAUM, status for MFO candidates sourced elsewhere. Its own name-regex yield is ~123–147 and may be admitted directly. |
| **MFO directories / rankings** | MFO seed | Admit as *candidates only*. Every one must clear §3 MFO rules against the ADV file before it counts toward the 500. |
| **13F** | **Enrichment only — removed from discovery** | Never admits a lead by itself (`X.13f_only`). Keep the existing 2,633 rows as a name-keyed AUM/holdings lookup for offices discovered elsewhere. Do not re-triage the 52 already scored. |
| PPP, 990-PF, 13D/G, conferences | Reserve | Not in the current build. Do not add without a decision — PPP is largely redundant with 5500. |

---

## 5. Entity resolution — apply before any count is reported

FEC employer strings are free text and will inflate the 500 with duplicates. Before a lead
counts:

1. Uppercase; strip punctuation; collapse whitespace.
2. Strip trailing entity suffixes: `LLC`, `L.L.C.`, `INC`, `LP`, `LLP`, `LTD`, `CO`, `CORP`,
   `TRUST`, `THE` (leading).
3. Strip generic tails: `FAMILY OFFICE`, `FAMILY OFFICES`, `CAPITAL`, `MANAGEMENT`,
   `PARTNERS`, `HOLDINGS`, `ADVISORS` — then cluster on the **remaining family token**.
4. `SMITH FAMILY OFFICE`, `Smith Family Office LLC`, and `The Smith Family Office` are **one**
   lead. Merge to the longest well-formed variant; keep the others as `aliases`.
5. Cross-source dedupe key, in priority order: EIN → ADV CRD# → SEC CIK → normalized name +
   state.

A count of distinct leads that has not been through this step is not a real count.

---

## 6. Evidence bar for a *validated* lead

To count toward the 500, a lead needs **two independent sources**, at least one **primary**
(a regulatory filing, or the entity's own website/document). Two aggregators repeating each
other is one source.

Required fields — a lead missing any of these is `pursue_low`, not shippable:

- Canonical legal name
- Type: `SFO` | `MFO` (`type_unconfirmed` is not shippable toward the 500)
- City + state
- **One named decision-maker with a current title** (principal, CIO, President, Managing
  Director, Executive Director)
- At least one source URL with a retrieval date

Optional but preferred: AUM with an as-of date, EIN, CRD#, website, staff headcount.

A private foundation (990-PF) may be used to corroborate an SFO — a family foundation's
officer/trustee address is frequently the family office's own address — but the foundation
is never the lead itself.

---

## 7. Worked examples

Drawn from actual pipeline output. These are the calibration set; when a judgment is close,
match it against this table.

| Entity | Verdict | Why |
|---|---|---|
| `DFO Management` | **ACCEPT — SFO** | Michael Dell's family office. Single family, no external clients. |
| `WE FAMILY OFFICES` | **ACCEPT — MFO** | Registered adviser, self-describes as MFO, multiple unrelated families. |
| `BOSTON FAMILY OFFICE LLC` | **ACCEPT — MFO** | In ADV snapshot, name anchor, multi-family service model. |
| `Colony Family Offices, LLC` | **ACCEPT — MFO** | Same pattern; arrived via 13F but corroborated independently. |
| `FIRST HAWAIIAN BANK` | **REJECT** `X.bank` | Shipped `pursue_low` on 2026-07-28. Mechanical kill — must never reach an LLM. |
| `Canada Post Corp Registered Pension Plan` | **REJECT** `X.pension` + `X.foreign` | Same run, same failure. |
| `COMMONWEALTH OF PENNSYLVANIA PUBLIC SCHOOL EMPLS RETRMT SYS` | **REJECT** `X.pension` + `X.govt` | Same. |
| `DAILY JOURNAL CORP` | **REJECT** `X.public` | Public company that files 13F. Pure 13F artifact. |
| `AEGON USA Investment Management, LLC` | **REJECT** `X.fund` | Institutional asset manager. Was scored `pursue`. |
| `IFM Investors Pty Ltd` | **REJECT** `X.foreign` | Australian infrastructure manager. Was scored `pursue`. |
| `ADAMS DIVERSIFIED EQUITY FUND, INC.` | **REJECT** `X.fund` | Closed-end fund. |
| `Fulton Bank, N.A.` | **REJECT** `X.bank` | Bank trust department. |
| `"FAMILY OFFICE"` (FEC, Sudbury MA) | **REJECT** `X.no_anchor` | No family attached. Real string in the current FEC pull. |
| `NORTHSTAR FINANCIAL PLANNING, LLC` | **REJECT** | Wealth-management RIA. Passed a tuned structural filter — proof that structural MFO filtering fails. |
| `MFO Capital Limited` | **REJECT** `X.fund` + `X.foreign` | Fund GP. "MFO" in the name means nothing. |
| `ICG Advisors, LLC` | **BORDERLINE → verify** | Plausible MFO; needs §3 MFO checks before it counts. |
| `PATHSTONE FAMILY OFFICE` | **BORDERLINE → verify** | Real MFO, but large enough to risk `X.retail_book`. Check `5D(a)(1)` before admitting. |

---

## 8. Gate wiring — defect to fix

`G1.Q5` ("is this a plain RIA-in-costume?") is `gate="HARD"` but `on_unknown="deprioritize"`,
so an unanswered check ships the lead as `pursue_low` instead of rejecting it. That is why
banks and pension plans appear in the survivor list rather than in `rejections`.

Two changes:
1. Everything in §2 is rejected **mechanically at ingest**, so `G1.Q5` never sees an
   institution in the first place. This is the real fix.
2. `evaluate_hard_gates` must test claim *polarity*, not just claim *status*. A settled
   negative answer currently returns `status="confirmed"` and passes the gate — the same
   defect that got `G3.Q3` deleted on 2026-08-12. `G1.Q5` still has it.

---

## 9. Reporting rule

Never report a count of "valid leads" that has not been through §5 dedupe and §6 evidence
checks. If the SFO and MFO halves are unbalanced, say so and give both numbers separately —
do not report a combined 500 that is 400 SFO and 100 MFO as if it met the target. Per §3, the
MFO half is the constrained one and is where a shortfall will appear first.

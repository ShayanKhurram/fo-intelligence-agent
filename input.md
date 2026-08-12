# input.md — Run Priority Guide

**When told to run, pull source classes in this order.** Do not pull them evenly. Do not
pull anything marked OFF.

**Target: 500 — 250 SFO / 250 MFO.**

---

## Source priority

Connector code lives in `C:/Users/HP/scraping_data_task_!/fo_discovery/connectors/`.
**All eight connectors are written.** Nothing below needs building from scratch — the
blockers are dead URLs, wrong column names, and one wrong role. Fix the wiring, don't rewrite.

| # | Source class | Yields | Target | Connector | Blocker |
|---|---|---|---|---|---|
| 1 | `fec_employer` | **SFO** | 250 | `c3_fec.py` | Running the API path. Bulk path exists but is gated behind env `FEC_BULK_PATH` and won't download on its own. |
| 2 | `adv_name` | **MFO** | **87 (delivered)** | `c2_form_adv.py` | ✅ **Fixed and run 2026-08-13.** |
| 3 | `mfo_directory` | **MFO** | ~163 | `c5_conferences.py`, `c6_serp.py` | Written, never run. Gap widened — see below. |
| 4 | `dol_5500` | **SFO** backfill | as needed | `c4_dol_5500.py` | No working live URL; `efast.dol.gov` 403s. Ran off `samples/dol_5500_2025.zip`, which is why it emitted `Acme Family Office LLC`. |
| — | `edgar_13f` | — | **0** | `c1_edgar_13f.py` | **OFF for discovery.** Works fine — that's the problem. |
| — | `ppp_loans` | — | **0** | `c8_ppp.py` | **OFF** — redundant with 5500. |

### `c2_form_adv.py` — fixed 2026-08-13, delivered 87 MFOs

Run it with:

```
cd "C:/Users/HP/scraping_data_task_!"
SCRAPER_CONTACT_EMAIL="you@example.com" python -m fo_discovery.run --connectors c2 --format jsonl
```

It now plays both roles: `AdvNameConnector` (discovery, `discovery_class="adv_name"`) and
`AdvFirmLookup` (corroboration, 16,747 firms indexed by name → CRD/RAUM). `DiscoveryClass`
gained `ADV_NAME`, and `"c2"` is registered in both `CONNECTORS` and `LOOKUPS`.

**Yield: 123 name matches → 87 emitted.** Exclusions: 31 `X.retail_book`, 2 `X.foreign`,
3 dropped by tightening the name regex.

Four defects were fixed. The first three made it impossible for the connector to ever return
a row; the last two were found *by running it*:

1. It scraped `https://adviserinfo.sec.gov/compilation` for an `IA_Firm*.zip` href. That page
   is a JS app with no such link, so URL resolution returned `None` and it silently no-opped.
   Now resolves the newest `iaMMDDYY.zip` off the SEC listing page, newest-by-date rather than
   by document order, with a verified fallback constant.
2. It read the CSV as utf-8. The file is **latin-1**.
3. It matched columns `CRD_NUMBER` / `LEGAL_NAME` / `AUM`. The real headers are
   **`Organization CRD#`**, **`Legal Name`**, **`Primary Business Name`**, RAUM at
   **`5F(2)(c)`** — so even given the right zip, every row hit the `if not crd` guard.
4. **`X.retail_book` as an absolute cut was wrong** — it dropped `PATHSTONE` ($110B, 1,834 HNW
   clients, 145 retail), `STOKES FAMILY OFFICE` ($3.35B) and `FUSION FAMILY WEALTH` ($1.44B).
   Any large MFO carries some non-HNW individuals, so an absolute threshold penalises the
   biggest and most certain offices. Now a **ratio**: reject when `retail > hnw`, with firms
   above $1B RAUM kept regardless. Pathstone is 7% retail; `TEXAS FAMILY WEALTH` is 99%
   (1,060 retail vs 12 HNW) and is correctly dropped.
5. **`multi.?family` matched multifamily real estate** — it pulled in `BRIDGE INVESTMENT GROUP`
   ($49.3B), `GID MULTIFAMILY INVESTMENT MANAGEMENT` ($5.9B), and `LARAMAR MULTI-FAMILY VALUE
   MANAGER`. Those 3 were the only firms matching that alternative without also matching the
   `family <noun>` branch, so the pattern now requires `multi-family office`.

---

## Why this order (measured 2026-08-12, not estimated)

- **FEC is the SFO engine.** 97 distinct offices from a partial pull that was 85% one cycle
  (2026), at 100% name precision. The bulk `indiv{YY}.zip` files across 2010–2026 are ~10–20x
  that volume. Highest yield per unit of work, and the bulk parser already exists — it just
  needs `FEC_BULK_PATH` pointed at downloaded cycle files.
- **ADV is the MFO source and is nearly free.** One 5MB download, one regex. **Delivered 87
  validated MFOs on 2026-08-13** — Pathstone, WE Family Offices, Callan, BMO Family Office,
  Colony. Zero institutions, zero real-estate firms in the output.
- **ADV cannot go past 87.** MFOs like Iconiq have no "family" token, and structural filtering
  does not find them — tested and failed (see `docs/lead_admission_spec.md` §3). The MFO gap is
  therefore **163, not 100** — bigger than first planned, and it all falls on directories.
  This is the constrained half of the 500 and the place the target is most likely to miss.
- **13F is off because it produced 98% of the garbage.** 0.7% base rate; of the 52 leads that
  survived triage on 2026-07-28, **51 came from 13F** — banks, pension plans, `DAILY JOURNAL
  CORP`. Keep the existing 2,633 rows as a name-keyed AUM lookup only. Do not re-triage them.

---

## Run order

1. **ADV first** — one download, deterministic, no LLM. Banks the MFO half immediately and
   gives you a local verification file for everything downstream.
   `https://www.sec.gov/files/investment/data/other/information-about-registered-investment-advisers-exempt-reporting-advisers/ia050126.zip`
   (448 cols, latin-1 CSV. Name fields: `Primary Business Name`, `Legal Name`.)
2. **FEC bulk** — the long pole. Sweep cycles 2026 → 2010, newest first, stopping when the
   SFO quota is met. Dedupe before counting (see below).
3. **Check the split.** If SFO < 250, run `dol_5500`. If MFO < 250, run `mfo_directory`.
4. **Triage only what survives** steps 1–3. Nothing from `edgar_13f` enters the queue.

---

## Stop conditions

- Stop a source when its target in the table above is met.
- **Dedupe before reporting any count.** FEC employer strings are free text — `SMITH FAMILY
  OFFICE`, `Smith Family Office LLC`, and `The Smith Family Office` are one lead. A count that
  has not been deduped is not a count.
- Report SFO and MFO separately, never a combined 500. If short, it will be short on MFO —
  that is the constrained half.

---

## Skip on sight

Reject before any LLM call, no exceptions:

- Anything with origin class `edgar_13f` and no second-source corroboration.
- Banks, pension plans, insurers, funds, broker-dealers, public companies, non-US entities.
- A family token with no family attached — the literal string `"FAMILY OFFICE"` appears in the
  current FEC pull and is not a lead.

Full rules, reason codes, and the validated regex list: `docs/lead_admission_spec.md` §2.

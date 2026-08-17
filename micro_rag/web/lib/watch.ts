// T46.1 — the baseline read for the Intent Watcher.
//
// The corpus already carries activity in `provenance` (field_name in the five signal
// fields), written there by the Python ingest. This module is the **read side**: it
// surfaces that baseline as `WatchSignal`s and stacks any `watch_signals` rows (produced
// by the research run) on top. It reads only — it never writes to `records`,
// `provenance`, `chunks` or `dataset_meta`, all of which the ingest owns and prunes.
import { getPool } from "./db.ts";
import { ensureWatchSchema } from "./watch-schema.ts";

// A signal surfaced on the watch board. `origin` distinguishes the standing corpus
// (baseline, from `provenance`) from what a research run discovered (research, from
// `watch_signals`). `field_name` is only set on baseline signals, where the kind was
// derived mechanically from it (see KIND_BY_FIELD).
export type WatchSignal = {
  id: string | number;
  origin: "baseline" | "research";
  kind: string;
  headline: string;
  meaning: string | null;
  source_url: string | null;
  source_domain: string | null;
  source_name: string | null;
  observed_at: string | null; // ISO date (DATE, no time) or null
  confidence: string | null;
  field_name?: string;
};

export type WatchBoardOrg = {
  record_id: string;
  entity_name: string;
  entity_type: string;
  hq_state: string | null;
  aum_usd: number | null;
  signals: WatchSignal[];
  newestAt: string | null;
  baselineCount: number;
  freshCount: number;
};

export type WatchBoard = {
  orgs: WatchBoardOrg[];
};

// The five activity signal fields the ingest writes, mapped mechanically to a display
// `kind`. The mapping is structural and deterministic — never derived by a model, so the
// UI can rely on it. `important_insight` is already an interpreted one-sentence trigger
// in the corpus (the closest thing to "meaning" that exists) and is surfaced as a
// `mandate_shift` signal whose `meaning` is its own value.
const SIGNAL_FIELDS = [
  "recent_investments",
  "recent_news",
  "recent_key_hires",
  "recent_fund_commitments",
  "important_insight",
] as const;

const KIND_BY_FIELD: Record<string, string> = {
  recent_investments: "capital_deployment",
  recent_fund_commitments: "capital_deployment",
  recent_key_hires: "personnel",
  important_insight: "mandate_shift",
  recent_news: "firm_news",
};

// Baseline rows that are clearly not a signal. The live corpus really does contain a
// `recent_news` row whose entire `value` is the string `META_TITLE_QUOTE` — a leak from
// upstream scraping. Without this guard it would render as a headline. Drop a row whose
// value is NULL, blank, under 12 characters, or is all-caps-with-underscores (a sentinel
// token, not prose).
const JUNK_RE = /^[A-Z_]+$/;

function isJunkValue(value: string | null): boolean {
  if (value === null) return true;
  const trimmed = value.trim();
  if (trimmed.length === 0) return true;
  if (trimmed.length < 12) return true;
  if (JUNK_RE.test(trimmed)) return true;
  return false;
}

const EXCLUDED_STATUSES = new Set([
  "could_not_verify",
  "removed_failed_validation",
  "contradicted",
]);

type BaselineRow = {
  record_id: string;
  field_name: string;
  value: string | null;
  source_url: string | null;
  // `provenance` has no `source_name` column; `source_class` is the provenance analogue
  // (13f_filing, gdelt, news_article, web_page) and is mapped to the signal's
  // `source_name` for display. D6.
  source_class: string | null;
  // node-postgres parses `timestamptz` into a JS `Date`, not a string — typing this as
  // `string` hid the `.slice is not a function` crash from `tsc`. D7.
  retrieved_at: Date | string | null;
  status: string;
  confidence: string | null;
};

type ResearchRow = {
  id: number;
  record_id: string;
  kind: string;
  headline: string;
  meaning: string | null;
  confidence: string | null;
  source_url: string | null;
  source_domain: string | null;
  source_name: string | null;
  // Postgres `DATE` also comes back as a JS `Date`. D8.
  observed_at: Date | string | null;
};

type OrgRow = {
  record_id: string;
  entity_name: string;
  entity_type: string;
  hq_state: string | null;
  aum_usd: number | null;
};

// Load the baseline (`provenance`) activity signals for every record in one query, apply
// the junk guard, and map each surviving row to a WatchSignal. Returns a map keyed by
// record_id.
async function loadBaselineSignals(): Promise<Map<string, WatchSignal[]>> {
  const pool = getPool();
  const { rows } = await pool.query<BaselineRow>(
    `SELECT record_id, field_name, value, source_url, source_class,
            retrieved_at, status, confidence
       FROM provenance
      WHERE field_name = ANY($1::text[])`,
    [[...SIGNAL_FIELDS]],
  );
  const byRecord = new Map<string, WatchSignal[]>();
  for (const r of rows) {
    if (EXCLUDED_STATUSES.has(r.status)) continue;
    if (isJunkValue(r.value)) continue;
    const kind = KIND_BY_FIELD[r.field_name] ?? "firm_news";
    // `important_insight` is already an interpreted one-sentence trigger in the corpus,
    // so it is its own meaning — the closest thing to "meaning" the corpus has. Other
    // baseline fields carry no interpretation; their meaning is an honest blank.
    const meaning = r.field_name === "important_insight" ? (r.value as string).trim() : null;
    const signal: WatchSignal = {
      id: `${r.record_id}:${r.field_name}:${r.value!.slice(0, 40)}`,
      origin: "baseline",
      kind,
      headline: (r.value as string).trim(),
      meaning,
      source_url: r.source_url,
      source_domain: r.source_url ? domainOf(r.source_url) : null,
      // `provenance` has no `source_name`; `source_class` is the provenance analogue.
      source_name: r.source_class,
      observed_at: toIsoDate(r.retrieved_at),
      confidence: r.confidence,
      field_name: r.field_name,
    };
    const list = byRecord.get(r.record_id);
    if (list) list.push(signal);
    else byRecord.set(r.record_id, [signal]);
  }
  return byRecord;
}

async function loadResearchSignals(recordIds: string[]): Promise<Map<string, WatchSignal[]>> {
  if (recordIds.length === 0) return new Map();
  const pool = getPool();
  const { rows } = await pool.query<ResearchRow>(
    `SELECT id, record_id, kind, headline, meaning, confidence,
            source_url, source_domain, source_name, observed_at
       FROM watch_signals
      WHERE record_id = ANY($1::text[])
      ORDER BY created_at DESC`,
    [recordIds],
  );
  const byRecord = new Map<string, WatchSignal[]>();
  for (const r of rows) {
    const signal: WatchSignal = {
      id: r.id,
      origin: "research",
      kind: r.kind,
      headline: r.headline,
      meaning: r.meaning,
      source_url: r.source_url,
      source_domain: r.source_domain,
      source_name: r.source_name,
      observed_at: toIsoDate(r.observed_at),
      confidence: r.confidence,
    };
    const list = byRecord.get(r.record_id);
    if (list) list.push(signal);
    else byRecord.set(r.record_id, [signal]);
  }
  return byRecord;
}

function domainOf(url: string): string | null {
  try {
    const host = new URL(url).hostname.replace(/^www\./, "");
    return host || null;
  } catch {
    return null;
  }
}

// Postgres returns both `timestamptz` and `date` as JS `Date` objects; ISO-slice them.
// `String(date)` yields "Fri Aug 14 2026 ..." — never use it for a date key, or the
// lexicographic sort in `newestSignalDate` silently corrupts (every malformed date sorts
// after every ISO one). D8: both loaders route through this helper so `observed_at` is
// always either a real `YYYY-MM-DD` or `null`.
export function toIsoDate(v: Date | string | null | undefined): string | null {
  if (v === null || v === undefined) return null;
  const d = v instanceof Date ? v : new Date(v);
  return isNaN(d.getTime()) ? null : d.toISOString().slice(0, 10);
}

function newestSignalDate(signals: WatchSignal[]): string | null {
  let best: string | null = null;
  for (const s of signals) {
    if (!s.observed_at) continue;
    if (best === null || s.observed_at > best) best = s.observed_at;
  }
  return best;
}

// The watch board — every org that carries at least one usable baseline signal OR at
// least one `watch_signals` row. Sorted **freshest first**: orgs with a dated signal by
// newest signal date DESC, then undated orgs by entity_name ASC. Undated is the normal
// case (805 of 861 signal rows have a NULL retrieved_at) and is handled, not filtered.
export async function loadWatchBoard(): Promise<WatchBoard> {
  const pool = getPool();
  await ensureWatchSchema(pool);

  const baseline = await loadBaselineSignals();

  // Every record that carries a baseline signal — and any record that has a research
  // signal even if it has no baseline (a research run can surface an org the ingest
  // never tagged). Union the two id sets before loading record metadata + research rows.
  const recordIds = new Set<string>(baseline.keys());

  const { rows: researchRows } = await pool.query<{ record_id: string }>(
    `SELECT DISTINCT record_id FROM watch_signals`,
  );
  for (const r of researchRows) recordIds.add(r.record_id);

  if (recordIds.size === 0) return { orgs: [] };

  const ids = [...recordIds];
  const [orgRows, research] = await Promise.all([
    pool.query<OrgRow>(
      `SELECT record_id, entity_name, entity_type, hq_state, aum_usd
         FROM records
        WHERE record_id = ANY($1::text[])`,
      [ids],
    ),
    loadResearchSignals(ids),
  ]);

  const orgById = new Map(orgRows.rows.map((o) => [o.record_id, o] as const));

  const orgs: WatchBoardOrg[] = [];
  for (const id of ids) {
    const org = orgById.get(id);
    if (!org) continue; // record pruned between the signal read and now — skip, don't crash
    const signals = [...(baseline.get(id) ?? []), ...(research.get(id) ?? [])];
    if (signals.length === 0) continue;
    const newestAt = newestSignalDate(signals);
    orgs.push({
      record_id: id,
      entity_name: org.entity_name,
      entity_type: org.entity_type,
      hq_state: org.hq_state,
      aum_usd: org.aum_usd,
      signals,
      newestAt,
      baselineCount: baseline.get(id)?.length ?? 0,
      freshCount: research.get(id)?.length ?? 0,
    });
  }

  // Freshest first: dated orgs by newest signal date DESC, then undated orgs by name.
  // A stable name tiebreak keeps the order deterministic across reloads.
  orgs.sort((a, b) => {
    const ad = a.newestAt;
    const bd = b.newestAt;
    if (ad !== null && bd !== null) {
      if (ad !== bd) return ad < bd ? 1 : -1; // DESC by date
      return a.entity_name < b.entity_name ? -1 : a.entity_name > b.entity_name ? 1 : 0;
    }
    if (ad !== null && bd === null) return -1; // dated before undated
    if (ad === null && bd !== null) return 1;
    return a.entity_name < b.entity_name ? -1 : a.entity_name > b.entity_name ? 1 : 0; // undated by name
  });

  return { orgs };
}

// The research queue — the **stalest-first** order, deliberately the opposite of the
// display order: never-researched orgs first, then longest-ago-researched, then orgs
// whose newest signal is oldest, then by name. Never shares a query with
// `loadWatchBoard` because the two orders are intentionally different.
export async function loadResearchQueue(
  limit: number,
): Promise<{ record_id: string; entity_name: string }[]> {
  if (limit <= 0) return [];
  const pool = getPool();
  await ensureWatchSchema(pool);

  // The candidate pool is the same as the board's: every record carrying a baseline
  // signal OR a watch_signals row. Recompute the union of signal-bearing record_ids, then
  // join to `records` for the name and to `watch_org_state` for recency, and to a
  // per-record newest-signal-date subquery so stalest-signal is a sort key too.
  const { rows } = await pool.query<{
    record_id: string;
    entity_name: string;
  }>(
    `WITH signal_records AS (
         SELECT record_id FROM provenance
          WHERE field_name = ANY($1::text[])
            AND status NOT IN ('could_not_verify','removed_failed_validation','contradicted')
            AND value IS NOT NULL
            AND btrim(value) <> ''
            AND char_length(btrim(value)) >= 12
            AND btrim(value) !~ '^[A-Z_]+$'
         UNION
         SELECT DISTINCT record_id FROM watch_signals
     ),
     newest_signal AS (
         -- newest observed_at across baseline provenance (date part of retrieved_at) and
         -- watch_signals. NULL means undated; in the stalest-first order NULLS come FIRST
         -- so an undated org is treated as maximally stale (worth researching soon).
         SELECT record_id, MAX(observed_at) AS newest_signal_at
           FROM (
             SELECT record_id, (retrieved_at)::date AS observed_at
               FROM provenance
              WHERE field_name = ANY($1::text[])
                AND status NOT IN ('could_not_verify','removed_failed_validation','contradicted')
                AND retrieved_at IS NOT NULL
             UNION ALL
             SELECT record_id, observed_at FROM watch_signals WHERE observed_at IS NOT NULL
           ) u
          GROUP BY record_id
     )
     SELECT r.record_id, r.entity_name
       FROM signal_records s
       JOIN records r ON r.record_id = s.record_id
       LEFT JOIN watch_org_state os ON os.record_id = s.record_id
       LEFT JOIN newest_signal ns ON ns.record_id = s.record_id
      ORDER BY os.last_researched_at NULLS FIRST,
               ns.newest_signal_at NULLS FIRST,
               r.entity_name ASC
      LIMIT $2`,
    [[...SIGNAL_FIELDS], limit],
  );

  // De-duplicate defensively (the UNION inside signal_records already dedupes, but a
  // second guard is cheap and protects against any future join fan-out).
  const seen = new Set<string>();
  const out: { record_id: string; entity_name: string }[] = [];
  for (const r of rows) {
    if (seen.has(r.record_id)) continue;
    seen.add(r.record_id);
    out.push({ record_id: r.record_id, entity_name: r.entity_name });
  }
  return out;
}
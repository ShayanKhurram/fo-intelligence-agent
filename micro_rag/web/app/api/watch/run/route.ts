import { NextResponse } from "next/server";
import { getPool } from "@/lib/db";
import { ensureWatchSchema } from "@/lib/watch-schema";
import { loadResearchQueue, toIsoDate, type WatchSignal } from "@/lib/watch";
import {
  searchActivity,
  acceptArticle,
  dedupeKey,
  normalizeEntityName,
  interpretBatch,
  type Article,
  type InterpretItem,
  type Interpretation,
  type SearchResult,
} from "@/lib/watch-research";
import { sseStream } from "@/lib/sse";
import type { WatchStreamEvent, WatchMeaningUpdate } from "@/lib/watch-types";

// T46.3 — the Intent Watcher run. POST starts a sweep, GET streams it as SSE, DELETE
// cancels it. The run is resumable across 45s slices: progress is persisted per-org in
// watch_run_targets, so a dropped connection loses at most one org's work and the next
// connection picks up the remaining `state='pending'` targets by position.

// 60s is the Hobby-tier ceiling (app/api/query/route.ts documents the same). The slice
// loop self-limits to a 45s wall-clock budget and emits `paused`, so a single request
// never approaches the hard limit.
export const maxDuration = 60;
export const dynamic = "force-dynamic";

const CONCURRENCY = 6;
const SLICE_BUDGET_MS = 45_000;
const INTERPRET_FLUSH_EVERY = 5;
const RATE_LIMIT_BACKOFF_MS = 2000;
const RATE_LIMIT_MAX_RETRIES = 2;

// ---------------------------------------------------------------------------
// POST — start (or reuse) a run
// ---------------------------------------------------------------------------

export async function POST(req: Request) {
  const body = await req.json().catch(() => ({} as Record<string, unknown>));
  const scopeRaw = Number(body?.scope ?? 60);
  const scope = Math.min(Math.max(Number.isFinite(scopeRaw) ? scopeRaw : 60, 1), 500);
  const lookbackRaw = Number(body?.lookbackDays ?? 90);
  const lookbackDays = Math.min(Math.max(Number.isFinite(lookbackRaw) ? lookbackRaw : 90, 1), 365);
  const backend: "serper" | "gdelt" = process.env.SERPER_API_KEY ? "serper" : "gdelt";

  try {
    const pool = getPool();
    await ensureWatchSchema(pool);

    // Two concurrent sweeps would double-spend Serper credits. If a run is already
    // running, hand back that run instead of creating a second one.
    const { rows: running } = await pool.query(
      `SELECT run_id, queued, backend FROM watch_runs WHERE status = 'running' ORDER BY started_at DESC LIMIT 1`,
    );
    if (running.length > 0) {
      return NextResponse.json({
        run_id: running[0].run_id,
        queued: running[0].queued,
        backend: running[0].backend,
        reused: true,
      });
    }

    const targets = await loadResearchQueue(scope);
    if (targets.length === 0) {
      return NextResponse.json({ run_id: null, queued: 0, backend, empty: true });
    }

    const runId = crypto.randomUUID();
    await pool.query(
      `INSERT INTO watch_runs (run_id, status, scope, lookback_days, backend, queued, completed, found, failed)
       VALUES ($1,'running',$2,$3,$4,$5,0,0,0)`,
      [runId, scope, lookbackDays, backend, targets.length],
    );

    // Bulk-insert the target list with its stalest-first position preserved, so a resumed
    // slice continues in the same order the queue produced.
    const placeholders: string[] = [];
    const vals: unknown[] = [runId];
    targets.forEach((t, i) => {
      const o = i * 3;
      placeholders.push(`($1,$${2 + o},$${3 + o},$${4 + o})`);
      vals.push(t.record_id, i, "pending");
    });
    await pool.query(
      `INSERT INTO watch_run_targets (run_id, record_id, position, state) VALUES ${placeholders.join(",")}`,
      vals,
    );

    return NextResponse.json({ run_id: runId, queued: targets.length, backend });
  } catch (e) {
    return NextResponse.json(
      { status: "db_unavailable", error: String(e) },
      { status: 503 },
    );
  }
}

// ---------------------------------------------------------------------------
// DELETE — cancel a running run
// ---------------------------------------------------------------------------

export async function DELETE(request: Request) {
  const { searchParams } = new URL(request.url);
  const runId = searchParams.get("run_id");
  if (!runId) {
    return NextResponse.json({ error: "missing run_id" }, { status: 400 });
  }
  try {
    const pool = getPool();
    await ensureWatchSchema(pool);
    // Only a running run can be cancelled. The slice loop checks status before each org
    // and stops within one org, emitting `{type:"done", cancelled:true}`.
    await pool.query(
      `UPDATE watch_runs SET status = 'cancelled', ended_at = now() WHERE run_id = $1 AND status = 'running'`,
      [runId],
    );
    return NextResponse.json({ ok: true, run_id: runId });
  } catch (e) {
    return NextResponse.json(
      { status: "db_unavailable", error: String(e) },
      { status: 503 },
    );
  }
}

// ---------------------------------------------------------------------------
// GET — the SSE stream
// ---------------------------------------------------------------------------

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const runId = searchParams.get("run_id");
  if (!runId) {
    return NextResponse.json({ error: "missing run_id" }, { status: 400 });
  }

  return sseStream<WatchStreamEvent>(
    (emit) => runSlice(runId, emit),
    { type: "done", completed: 0, found: 0, error: "stream_failed" },
  );
}

type Target = { record_id: string; entity_name: string; position: number };

async function runSlice(runId: string, emit: (e: WatchStreamEvent) => void): Promise<void> {
  const pool = getPool();

  // Load the run row and the target list. If the run is already terminal, emit a matching
  // done event and close — a reconnect to a finished run should not restart it.
  const { rows: runRows } = await pool.query<
    { status: string; lookback_days: number; backend: string }
  >(`SELECT status, lookback_days, backend FROM watch_runs WHERE run_id = $1`, [runId]);
  if (runRows.length === 0) {
    emit({ type: "done", completed: 0, found: 0, error: "unknown_run" });
    return;
  }
  const run = runRows[0];
  if (run.status === "done") {
    const c = await counts(pool, runId);
    emit({ type: "done", completed: c.completed, found: c.found });
    return;
  }
  if (run.status === "cancelled") {
    const c = await counts(pool, runId);
    emit({ type: "done", completed: c.completed, found: c.found, cancelled: true });
    return;
  }
  if (run.status === "failed") {
    const c = await counts(pool, runId);
    emit({ type: "done", completed: c.completed, found: c.found, error: "credits_exhausted" });
    return;
  }

  // Pending targets, in position order. Join `records` for the entity name — the target
  // table stores only record_id (the watcher owns no FK to records, by constraint). A
  // record pruned between POST and now simply drops out of the join and is skipped.
  const { rows: targetRows } = await pool.query<Target>(
    `SELECT t.record_id, t.position, r.entity_name
       FROM watch_run_targets t
       JOIN records r ON r.record_id = t.record_id
      WHERE t.run_id = $1 AND t.state = 'pending'
      ORDER BY t.position`,
    [runId],
  );

  const totals = await counts(pool, runId);
  const total = totals.queued;
  const completedStart = totals.completed;
  const foundStart = totals.found;
  const lookbackDays = run.lookback_days;
  const backend = run.backend as "serper" | "gdelt";
  const key = backend === "serper" ? process.env.SERPER_API_KEY : undefined;
  const now = new Date();

  emit({
    type: "run",
    runId,
    queued: total,
    completed: completedStart,
    backend,
  });

  if (targetRows.length === 0) {
    // Nothing pending — the run is effectively complete (e.g. reconnect after finish).
    await pool.query(`UPDATE watch_runs SET status = 'done', ended_at = now() WHERE run_id = $1 AND status = 'running'`, [runId]);
    emit({ type: "done", completed: completedStart, found: foundStart });
    return;
  }

  // Slice state shared across the worker pool.
  let sliceCompleted = 0;
  let sliceFound = 0;
  let stopReason: "credits_exhausted" | "cancelled" | null = null;
  const sliceStart = Date.now();
  const pendingInterpret: InterpretItem[] = [];
  let nextIdx = 0;

  // The interpret flush must be serialized even with concurrent workers — two workers
  // seeing >=5 accumulated orgs would otherwise flush overlapping sets. A simple
  // promise-mutex guards the splice + interpretBatch call.
  let flushChain: Promise<void> = Promise.resolve();
  async function guardedFlush(take: number): Promise<void> {
    const prev = flushChain;
    let release!: () => void;
    flushChain = new Promise<void>((r) => (release = r));
    await prev;
    try {
      await flushInterpret(take);
    } finally {
      release();
    }
  }

  async function flushInterpret(take: number): Promise<void> {
    if (pendingInterpret.length === 0) return;
    const batch = pendingInterpret.splice(0, take);
    if (batch.length === 0) return;
    // interpretBatch makes ONE LLM call per 5 orgs and degrades to meaning=null on any
    // failure — an honest blank, never a fabricated one.
    const map = await interpretBatch(batch);
    for (const item of batch) {
      const interp: Interpretation[] = map.get(item.recordId) ?? [];
      if (interp.length === 0) continue;
      // Persist the interpretation onto the rows inserted during the org phase.
      for (const it of interp) {
        await pool.query(
          `UPDATE watch_signals SET kind = $3, meaning = $4, confidence = $5
            WHERE record_id = $1 AND dedupe_key = $2`,
          [item.recordId, it.dedupeKey, it.kind, it.meaning, it.confidence],
        );
      }
      const updates: WatchMeaningUpdate[] = interp.map((it) => ({
        dedupeKey: it.dedupeKey,
        kind: it.kind,
        meaning: it.meaning,
        confidence: it.confidence,
      }));
      emit({ type: "meaning", recordId: item.recordId, updates });
    }
  }

  async function emitProgress(): Promise<void> {
    const completed = completedStart + sliceCompleted;
    const found = foundStart + sliceFound;
    // msPerOrg = median duration_ms over targets completed IN THIS RUN; null until at
    // least 3 have completed (gauged by the EMITTED completed counter, not the raw DB
    // duration count — under concurrency 6 the DB can lag ahead of `sliceCompleted`, so
    // guarding on the DB count would leak a measured ETA before `completed` reaches 3).
    // etaMs = msPerOrg * remaining / concurrency. Never seeded with a constant — a made-up
    // countdown is worse than none.
    const { rows: drows } = await pool.query<{ duration_ms: number }>(
      `SELECT duration_ms FROM watch_run_targets
        WHERE run_id = $1 AND state IN ('done','failed','skipped') AND duration_ms IS NOT NULL`,
      [runId],
    );
    const ds = drows.map((r) => r.duration_ms).sort((a, b) => a - b);
    const msPerOrg = completed >= 3 && ds.length > 0 ? ds[Math.floor(ds.length / 2)] : null;
    const remaining = Math.max(0, total - completed);
    const etaMs = msPerOrg !== null ? Math.round((msPerOrg * remaining) / CONCURRENCY) : null;
    emit({ type: "progress", completed, total, found, msPerOrg, etaMs });
  }

  async function getRunStatus(): Promise<string> {
    const { rows } = await pool.query<{ status: string }>(
      `SELECT status FROM watch_runs WHERE run_id = $1`,
      [runId],
    );
    return rows[0]?.status ?? "running";
  }

  // Retry on rate_limited (ordinary Serper throttling — back off and continue); propagate
  // credits_exhausted immediately (the whole run ends, never retried per-org).
  async function searchWithRetry(name: string): Promise<SearchResult> {
    for (let attempt = 0; ; attempt++) {
      const r = await searchActivity(name, { lookbackDays, backend, key });
      if (r.error === "rate_limited" && attempt < RATE_LIMIT_MAX_RETRIES) {
        await new Promise((res) => setTimeout(res, RATE_LIMIT_BACKOFF_MS));
        continue;
      }
      return r;
    }
  }

  async function processTarget(target: Target): Promise<void> {
    if (stopReason) return;

    // Check for a mid-stream cancel before each org — stops within one org.
    const status = await getRunStatus();
    if (status === "cancelled") {
      stopReason = "cancelled";
      return;
    }
    if (status === "failed") {
      stopReason = "credits_exhausted";
      return;
    }

    emit({
      type: "scanning",
      recordId: target.record_id,
      entityName: target.entity_name,
      index: target.position,
      total,
    });

    const t0 = Date.now();
    const normalized = normalizeEntityName(target.entity_name);
    const result = await searchWithRetry(target.entity_name);
    const durationMs = Date.now() - t0;

    // credits_exhausted ends the whole run. Do not mark this target or emit an org event;
    // the run-finalize block sets status='failed' and emits the terminal done.
    if (result.error === "credits_exhausted") {
      stopReason = "credits_exhausted";
      await markTarget(pool, runId, target.record_id, "skipped", durationMs, "credits_exhausted");
      return;
    }

    if (result.skipped) {
      // name_too_generic — the entity normalizes to fewer than 2 significant tokens.
      await markTarget(pool, runId, target.record_id, "skipped", durationMs, result.skipped);
      emit({
        type: "org",
        recordId: target.record_id,
        entityName: target.entity_name,
        signals: [],
        skipped: result.skipped,
        index: target.position,
        total,
      });
      await upsertOrgState(pool, target.record_id, 0);
      sliceCompleted++;
      await emitProgress();
      return;
    }

    if (result.error) {
      // rate_limited (after retries), http_<status>, or fetch_failed — fail this org, not
      // the run. The run continues with the next target.
      await markTarget(pool, runId, target.record_id, "failed", durationMs, result.error);
      emit({
        type: "org",
        recordId: target.record_id,
        entityName: target.entity_name,
        signals: [],
        error: result.error,
        index: target.position,
        total,
      });
      await upsertOrgState(pool, target.record_id, 0);
      sliceCompleted++;
      await emitProgress();
      return;
    }

    // Relevance gate: drop undated / stale / blocked-domain / name-not-matched articles.
    const accepted: Article[] = [];
    for (const a of result.articles) {
      const verdict = acceptArticle(a, normalized, { lookbackDays, now });
      if (verdict.ok) accepted.push(a);
    }

    // Dedupe: drop anything already in watch_signals for this record OR already carried in
    // provenance.source_url — a re-run surfaces what changed, not what was already known.
    const newArticles = await filterNew(pool, target.record_id, accepted);

    if (newArticles.length > 0) {
      await insertSignals(pool, runId, target.record_id, newArticles);
    }

    // Two-phase reveal, phase 1: emit the org the moment the search returns, with every
    // signal's meaning null and a provisional kind. The meaning event fills them in.
    const signals: WatchSignal[] = newArticles.map((a) => ({
      id: dedupeKey(a.url),
      origin: "research",
      kind: "firm_news",
      headline: a.title,
      meaning: null,
      source_url: a.url,
      source_domain: domainOf(a.url),
      source_name: a.source || null,
      observed_at: toIsoDate(a.observedAt),
      confidence: null,
    }));
    emit({
      type: "org",
      recordId: target.record_id,
      entityName: target.entity_name,
      signals,
      index: target.position,
      total,
    });

    await markTarget(
      pool,
      runId,
      target.record_id,
      "done",
      durationMs,
      newArticles.length > 0 ? null : "no_new",
    );
    await upsertOrgState(pool, target.record_id, newArticles.length);
    sliceCompleted++;
    sliceFound += newArticles.length;

    // Accumulate orgs with surviving articles; flush interpretBatch every 5.
    if (newArticles.length > 0) {
      pendingInterpret.push({
        recordId: target.record_id,
        entityName: target.entity_name,
        articles: newArticles,
      });
      if (pendingInterpret.length >= INTERPRET_FLUSH_EVERY) {
        await guardedFlush(INTERPRET_FLUSH_EVERY);
      }
    }

    await emitProgress();
  }

  async function worker(): Promise<void> {
    while (!stopReason) {
      // 45s wall-clock budget: stop issuing new searches and let the slice pause, so a
      // single request never approaches the 60s hard limit.
      if (Date.now() - sliceStart > SLICE_BUDGET_MS) break;
      const i = nextIdx++;
      if (i >= targetRows.length) break;
      await processTarget(targetRows[i]);
    }
  }

  const workers = Array.from(
    { length: Math.min(CONCURRENCY, targetRows.length) },
    () => worker(),
  );
  await Promise.all(workers);

  // Flush any remaining accumulated orgs (the last partial batch).
  await guardedFlush(Number.MAX_SAFE_INTEGER);

  // Finalize.
  const completed = completedStart + sliceCompleted;
  const found = foundStart + sliceFound;

  // Persist the derived counters onto the run row at every slice end and terminal
  // transition. `counts()` remains the single source of truth for the stream (it derives
  // from watch_run_targets + watch_signals, which is why resume works); these three
  // columns are a persisted SUMMARY and are never read back. Without this they stayed 0
  // forever while the targets table showed the real progress — a row that would quietly
  // lie to anyone reading the table months from now.
  const persistTotals = async () => {
    const failedRows = await pool.query<{ c: number }>(
      `SELECT count(*)::int AS c FROM watch_run_targets WHERE run_id = $1 AND state = 'failed'`,
      [runId],
    );
    await pool.query(
      `UPDATE watch_runs SET completed = $2, found = $3, failed = $4, last_call_at = now() WHERE run_id = $1`,
      [runId, completed, found, failedRows.rows[0].c],
    );
  };
  await persistTotals();

  if (stopReason === "credits_exhausted") {
    await pool.query(
      `UPDATE watch_runs SET status = 'failed', error = 'credits_exhausted', ended_at = now() WHERE run_id = $1`,
      [runId],
    );
    emit({ type: "done", completed, found, error: "credits_exhausted" });
    return;
  }
  if (stopReason === "cancelled") {
    // DELETE already set status='cancelled'; just emit the terminal event.
    emit({ type: "done", completed, found, cancelled: true });
    return;
  }

  // No stop reason — either the budget ran out (paused, resumable) or all targets done.
  const { rows: pendingRows } = await pool.query<{ c: number }>(
    `SELECT count(*)::int AS c FROM watch_run_targets WHERE run_id = $1 AND state = 'pending'`,
    [runId],
  );
  const remaining = pendingRows[0].c;
  if (remaining > 0) {
    emit({ type: "paused", remaining });
    return;
  }
  await pool.query(
    `UPDATE watch_runs SET status = 'done', ended_at = now() WHERE run_id = $1 AND status = 'running'`,
    [runId],
  );
  emit({ type: "done", completed, found });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function counts(
  pool: ReturnType<typeof getPool>,
  runId: string,
): Promise<{ queued: number; completed: number; found: number }> {
  const { rows } = await pool.query<{ c: number; d: number }>(
    `SELECT
       count(*)::int AS c,
       count(*) FILTER (WHERE state IN ('done','failed','skipped'))::int AS d
       FROM watch_run_targets WHERE run_id = $1`,
    [runId],
  );
  const { rows: fr } = await pool.query<{ c: number }>(
    `SELECT count(*)::int AS c FROM watch_signals WHERE run_id = $1`,
    [runId],
  );
  return { queued: rows[0].c, completed: rows[0].d, found: fr[0].c };
}

async function markTarget(
  pool: ReturnType<typeof getPool>,
  runId: string,
  recordId: string,
  state: string,
  durationMs: number,
  note: string | null,
): Promise<void> {
  await pool.query(
    `UPDATE watch_run_targets SET state = $3, duration_ms = $4, note = $5
      WHERE run_id = $1 AND record_id = $2`,
    [runId, recordId, state, durationMs, note],
  );
}

// Drop articles already known for this record — by dedupe_key in watch_signals, OR by
// exact source_url in provenance (the corpus's own evidence chain).
async function filterNew(
  pool: ReturnType<typeof getPool>,
  recordId: string,
  articles: Article[],
): Promise<Article[]> {
  if (articles.length === 0) return [];
  const keys = articles.map((a) => dedupeKey(a.url));
  const urls = articles.map((a) => a.url);
  const [sig, prov] = await Promise.all([
    pool.query<{ dedupe_key: string }>(
      `SELECT dedupe_key FROM watch_signals WHERE record_id = $1 AND dedupe_key = ANY($2::text[])`,
      [recordId, keys],
    ),
    pool.query<{ source_url: string }>(
      `SELECT source_url FROM provenance WHERE record_id = $1 AND source_url = ANY($2::text[])`,
      [recordId, urls],
    ),
  ]);
  const usedKeys = new Set(sig.rows.map((r) => r.dedupe_key));
  const usedUrls = new Set(prov.rows.map((r) => r.source_url));
  return articles.filter((a) => !usedKeys.has(dedupeKey(a.url)) && !usedUrls.has(a.url));
}

// Insert surviving articles as signals with provisional kind='firm_news', meaning=null.
// ON CONFLICT DO NOTHING — a concurrent slice or a race cannot duplicate a signal.
async function insertSignals(
  pool: ReturnType<typeof getPool>,
  runId: string,
  recordId: string,
  articles: Article[],
): Promise<void> {
  const cols = "(run_id, record_id, dedupe_key, kind, headline, source_url, source_domain, source_name, observed_at)";
  const placeholders: string[] = [];
  const vals: unknown[] = [];
  articles.forEach((a, i) => {
    // 8 article values per row; $1 is the shared run_id. Offset by i*8 so row k's
    // placeholders are $1,$(2+8k)..$(9+8k) with no gaps. (A previous i*9 offset skipped
    // $10 for 2+ articles, which pg's varparam analysis could not type — 42P18.)
    const o = i * 8;
    placeholders.push(`($1,$${2 + o},$${3 + o},$${4 + o},$${5 + o},$${6 + o},$${7 + o},$${8 + o},$${9 + o})`);
    vals.push(
      recordId,
      dedupeKey(a.url),
      "firm_news",
      a.title,
      a.url,
      domainOf(a.url),
      a.source || null,
      toIsoDate(a.observedAt),
    );
  });
  await pool.query(
    `INSERT INTO watch_signals ${cols} VALUES ${placeholders.join(",")}
     ON CONFLICT (record_id, dedupe_key) DO NOTHING`,
    [runId, ...vals],
  );
}

// Upsert the per-record research state. last_signal_count is the total watch_signals
// count for the record after this run (not just the new ones).
async function upsertOrgState(
  pool: ReturnType<typeof getPool>,
  recordId: string,
  foundNew: number,
): Promise<void> {
  const { rows } = await pool.query<{ c: number }>(
    `SELECT count(*)::int AS c FROM watch_signals WHERE record_id = $1`,
    [recordId],
  );
  const totalCount = rows[0].c;
  await pool.query(
    `INSERT INTO watch_org_state (record_id, last_researched_at, last_found_at, last_signal_count)
     VALUES ($1, now(), CASE WHEN $2 > 0 THEN now() ELSE NULL END, $3)
     ON CONFLICT (record_id) DO UPDATE SET
       last_researched_at = now(),
       last_found_at = CASE WHEN $2 > 0 THEN now() ELSE watch_org_state.last_found_at END,
       last_signal_count = $3`,
    [recordId, foundNew, totalCount],
  );
}

function domainOf(url: string): string | null {
  try {
    return new URL(url).hostname.replace(/^www\./, "") || null;
  } catch {
    return null;
  }
}
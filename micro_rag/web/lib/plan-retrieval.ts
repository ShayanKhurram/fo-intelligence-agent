// Plan retrieval — micro_rag_plan.md T42.3. Two DB steps that together are the "more
// than one retrieval" the whole feature rests on: a wide structured sweep of `records`
// (every survivor, not a top-k chunk fetch), then ONE embedding round-trip that pulls
// the best evidence chunk per surviving record against the plan thesis.
//
// The sweep applies ONLY the core filters (entity_type / hq_state / aum_min / aum_max)
// in SQL. `mandates` and `check_size_*` are deliberately NOT filtered on: at 501 records
// `mandates` is non-empty on 55 and only 9 of those are retrievable, and
// `check_size_min` is set on 1 — filtering on either returns a near-empty set. That is
// the mistake the first draft of the spec made; it is not reintroduced here. Secondary
// predicates (no named principal / no contact path / non-shipped outcome) are classified
// in TypeScript, not SQL, so the rejected-with-reason appendix is free rather than N
// extra queries — and the rejected rows come back in `excluded` rather than being
// silently dropped.

import { getPool } from "./db.ts";
import { embedQuery } from "./embeddings.ts";
import type { PlanSpec } from "./plan-spec.ts";
import type { RecordRow } from "./types.ts";

export type Excluded = { record_id: string; entity_name: string; reason: string };

// A row must be a shipped lead to stay in the candidate set; everything else is honest
// context, not something to approach. 501-row measure: 1 `ship`, 338-ish
// `ship_with_caveats`, the rest rejected — so this drops the rejected tail, not the bulk.
const SHIPPED_OUTCOMES = new Set(["ship", "ship_with_caveats"]);

// A runaway guard, not a working limit. The corpus is 501 rows behind indexes on
// entity_type / hq_state / aum_usd, so the structured sweep is one cheap query and the
// cap no longer binds (T42.6: the previous 200-row cap ordered by record_id ranked an
// alphabet-first prefix and silently ignored the rest). Kept high so a future corpus
// 10× larger still has a bound; when it does bind, `truncated` reports it.
const SWEEP_CAP = 1000;

// The three facets that carry the material an approach note rests on. `identity` and
// `people` are structural; `summary` is a projection of the others. Best-evidence keeps
// to the facets where the thesis actually lives.
const EVIDENCE_FACETS = ["mandate", "activity", "why_now"];

/** Builds the core-filter WHERE clause the way `retrieval.ts`'s `buildWhereClause`
 * does: every value bound as a parameter, never interpolated. Only the four core
 * filters — entity_type / hq_state / aum_min / aum_max — appear here. */
function buildCoreWhere(spec: PlanSpec): { clause: string; params: unknown[] } {
  const conditions: string[] = [];
  const params: unknown[] = [];
  if (spec.entity_type) {
    params.push(spec.entity_type);
    conditions.push(`entity_type = $${params.length}`);
  }
  if (spec.hq_state) {
    params.push(spec.hq_state);
    conditions.push(`hq_state = $${params.length}`);
  }
  if (spec.aum_min != null) {
    params.push(spec.aum_min);
    conditions.push(`aum_usd >= $${params.length}`);
  }
  if (spec.aum_max != null) {
    params.push(spec.aum_max);
    conditions.push(`aum_usd <= $${params.length}`);
  }
  return { clause: conditions.length ? `WHERE ${conditions.join(" AND ")}` : "", params };
}

/** Classifies a swept row against the secondary predicates, returning the exclusion
 * reason(s) or null when the row survives into the candidate set. Pure, no DB. */
function exclusionReasons(row: Record<string, unknown>): string[] | null {
  const reasons: string[] = [];
  const outcome = (row.outcome as string | null | undefined) ?? null;
  if (!SHIPPED_OUTCOMES.has(outcome ?? "")) {
    reasons.push(`outcome is ${outcome ?? "unset"} (not a shipped lead)`);
  }
  if (!row.principal_name) {
    reasons.push("no named principal");
  }
  // "Contact path of any kind" — the firm's own inbox counts too. `principal_linkedin`
  // is provenance-sourced, not a `records` column, so it is not visible at the sweep.
  const hasContactPath = !!(row.principal_email || row.principal_phone || row.firm_email);
  if (!hasContactPath) {
    reasons.push("no contact path of any kind");
  }
  return reasons.length > 0 ? reasons : null;
}

export async function sweepCandidates(
  spec: PlanSpec
): Promise<{
  candidates: RecordRow[];
  excluded: Excluded[];
  sweptTotal: number;
  sweptConsidered: number;
  truncated: boolean;
}> {
  const pool = getPool();
  const { clause, params } = buildCoreWhere(spec);
  // The COUNT and the sweep share the exact same WHERE clause (built once), so the two
  // can never disagree about what was filtered — `sweptTotal` is the truth about how many
  // records matched, `sweptConsidered` is how many made it past the cap into ranking.
  const countSql = `SELECT COUNT(*) AS n FROM records ${clause}`;
  const sweepSql = `SELECT * FROM records ${clause} ORDER BY record_id ASC LIMIT ${SWEEP_CAP}`;
  const [countRes, sweepRes] = await Promise.all([
    pool.query(countSql, params),
    pool.query(sweepSql, params),
  ]);
  const sweptTotal = Number(countRes.rows[0]?.n ?? 0);

  const candidates: RecordRow[] = [];
  const excluded: Excluded[] = [];
  for (const row of sweepRes.rows) {
    const reasons = exclusionReasons(row);
    if (reasons) {
      excluded.push({
        record_id: row.record_id,
        entity_name: row.entity_name,
        reason: reasons.join("; "),
      });
    } else {
      candidates.push(row as RecordRow);
    }
  }
  const sweptConsidered = candidates.length + excluded.length;
  return {
    candidates,
    excluded,
    sweptTotal,
    sweptConsidered,
    truncated: sweptTotal > sweptConsidered,
  };
}

export type BestEvidence = {
  chunk_id: string;
  facet: string;
  content: string;
  distance: number;
};

/** Embeds the thesis ONCE, then one round-trip pulls the lowest-distance evidence chunk
 * per record across the mandate/activity/why_now facets. `DISTINCT ON (record_id)` with
 * `ORDER BY record_id, distance` keeps exactly one chunk per record — the best one.
 * Empty `recordIds` returns an empty Map without touching the pool, so a declined sweep
 * costs no embedding call. */
export async function bestEvidencePerRecord(
  recordIds: string[],
  thesis: string
): Promise<Map<string, BestEvidence>> {
  const out = new Map<string, BestEvidence>();
  if (recordIds.length === 0) return out;

  const pool = getPool();
  const queryVec = await embedQuery(thesis);
  // pg/psycopg will not adapt a raw JS array to pgvector's `vector` type — format the
  // vector as a bracketed literal the way `retrieval.ts`'s `semanticRank` does.
  const vecLiteral = `[${queryVec.join(",")}]`;
  const { rows } = await pool.query(
    `SELECT DISTINCT ON (record_id) record_id, chunk_id, facet, content,
            embedding <=> $2::vector AS distance
     FROM chunks
     WHERE record_id = ANY($1::text[]) AND facet = ANY($3::text[]) AND embedding IS NOT NULL
     ORDER BY record_id, embedding <=> $2::vector`,
    [recordIds, vecLiteral, EVIDENCE_FACETS]
  );
  for (const row of rows) {
    out.set(row.record_id, {
      chunk_id: row.chunk_id,
      facet: row.facet,
      content: row.content,
      distance: Number(row.distance),
    });
  }
  return out;
}
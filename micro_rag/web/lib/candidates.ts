// T44.2 — the unified retrieval core. One module that replaces what `hybridRetrieve`
// (lib/retrieval.ts) and the plan sweep (lib/plan-retrieval.ts) each did halfway: a wide
// structural sweep over `records`, then BOTH signals — semantic (best-evidence `DISTINCT
// ON`) and lexical (`ts_rank` over `content_tsv`) — fused by reciprocal rank over that
// candidate set, never a top-k fetch.
//
// The lexical half is load-bearing, not a nicety. A named-entity query ("Kapor", "QVT")
// finds its record through `lexicalRank`'s tsvector match today; a core that scored by
// embedding distance alone would silently lose exact-name retrieval — the most obviously
// correct behavior this product has. So the two signals are fused, and the fused rank
// (not a single distance) feeds plan-rank's `fit` sub-score via `PlanCandidate.fitRank`.

import { getPool } from "./db.ts";
import { embedQuery } from "./embeddings.ts";
import {
  sweepCandidates,
  bestEvidencePerRecord,
  type Excluded,
  type BestEvidence,
} from "./plan-retrieval.ts";
import { rankCandidates, type PlanCandidate, type RankedCandidate } from "./plan-rank.ts";
import { RRF_K } from "./retrieval.ts";
import type { QueryUnderstanding } from "./query-understanding.ts";
import type { PlanSpec } from "./plan-spec.ts";
import type { RecordRow } from "./types.ts";

function toSpec(u: QueryUnderstanding): PlanSpec {
  return {
    ...u.filters,
    thesis: u.semantic,
    top_n: u.top_n,
    asOf: u.asOf ?? "",
  };
}

/** Relaxes ONE core filter at a time, in the same fixed priority order as the plan route
 * (aum_min → aum_max → hq_state → entity_type), returning the relaxed spec plus a
 * human-readable description of what was dropped. `mandates_any` is not in the sweep's
 * WHERE clause, so it is not relaxable here. */
function relaxOneFilter(spec: PlanSpec): { next: PlanSpec; description: string } | null {
  const order: (keyof PlanSpec)[] = ["aum_min", "aum_max", "hq_state", "entity_type"];
  for (const key of order) {
    if (spec[key] !== undefined) {
      const next = { ...spec };
      const dropped = next[key];
      delete next[key];
      const label: Record<string, string> = {
        aum_min: "minimum AUM",
        aum_max: "maximum AUM",
        hq_state: "state (" + dropped + ")",
        entity_type: "office type (" + dropped + ")",
      };
      return { next, description: label[key] ?? String(key) };
    }
  }
  return null;
}

/** Semantic record ranks over the candidate set: best-evidence `DISTINCT ON (record_id)`
 * (one embedding of the thesis), then rank the records that have an evidence chunk by
 * ascending distance. Returns a map record_id → rank (1-based). */
async function semanticRecordRanks(
  recordIds: string[],
  thesis: string
): Promise<{ ranks: Map<string, number>; evidence: Map<string, BestEvidence> }> {
  const evidence = await bestEvidencePerRecord(recordIds, thesis);
  const ranked = [...evidence.entries()].sort((a, b) => a[1].distance - b[1].distance);
  const ranks = new Map<string, number>();
  for (let i = 0; i < ranked.length; i++) {
    ranks.set(ranked[i][0], i + 1);
  }
  return { ranks, evidence };
}

/** Lexical record ranks over the candidate set: `ts_rank(content_tsv, plainto_tsquery)`
 * exactly as `lib/retrieval.ts`'s `lexicalRank` does it, but reduced to ONE row per record
 * (the max ts_rank across that record's chunks) so it fuses with the per-record semantic
 * rank. No facet filter — name lookups match the identity/people/summary chunks. */
async function lexicalRecordRanks(
  recordIds: string[],
  query: string
): Promise<Map<string, number>> {
  const ranks = new Map<string, number>();
  if (recordIds.length === 0) return ranks;
  const pool = getPool();
  const { rows } = await pool.query(
    `SELECT record_id, MAX(ts_rank(content_tsv, plainto_tsquery('english', $1))) AS score
     FROM chunks
     WHERE record_id = ANY($2::text[]) AND content_tsv @@ plainto_tsquery('english', $1)
     GROUP BY record_id
     ORDER BY score DESC`,
    [query, recordIds]
  );
  rows.forEach((r, i) => ranks.set(r.record_id, i + 1));
  return ranks;
}

/** Reciprocal rank fusion over the two record-rank lists, reusing `RRF_K = 60` from
 * `lib/retrieval.ts` (tuned for this corpus; re-guessing would silently re-tune retrieval).
 * Returns a per-record fused score, then normalized to [0,1] so the top record is 1.0 —
 * the value that feeds `PlanCandidate.fitRank`. Records present in neither list score 0. */
function fuseAndNormalize(
  recordIds: string[],
  semRanks: Map<string, number>,
  lexRanks: Map<string, number>
): Map<string, number> {
  const fused = new Map<string, number>();
  let max = 0;
  for (const id of recordIds) {
    let s = 0;
    const sr = semRanks.get(id);
    if (sr != null) s += 1 / (RRF_K + sr);
    const lr = lexRanks.get(id);
    if (lr != null) s += 1 / (RRF_K + lr);
    fused.set(id, s);
    if (s > max) max = s;
  }
  if (max <= 0) return fused; // no record had any hit — leave all at 0
  const norm = new Map<string, number>();
  for (const [id, s] of fused) norm.set(id, s / max);
  return norm;
}

export async function selectCandidates(
  understanding: QueryUnderstanding
): Promise<{
  ranked: RankedCandidate[];
  excluded: Excluded[];
  sweptTotal: number;
  sweptConsidered: number;
  truncated: boolean;
  relaxedFilters: string[];
}> {
  const relaxedFilters: string[] = [];
  let spec = toSpec(understanding);

  // Structural sweep — reuse `sweepCandidates` from plan-retrieval exactly (cap 1000,
  // ORDER BY record_id, secondary predicates classified in TypeScript, sweptTotal from
  // the shared buildCoreWhere). Relax ONE core filter at a time, reported, ONLY when the
  // sweep matches nothing (the T42.4 rule — never a quiet widening into an unrelated list).
  let sweep = await sweepCandidates(spec);
  let totalRows = sweep.candidates.length + sweep.excluded.length;
  while (totalRows === 0) {
    const relaxed = relaxOneFilter(spec);
    if (!relaxed) break;
    spec = relaxed.next;
    relaxedFilters.push(relaxed.description);
    sweep = await sweepCandidates(spec);
    totalRows = sweep.candidates.length + sweep.excluded.length;
  }

  // No matches at all (even after full relaxation) → empty result; the route declines.
  if (totalRows === 0) {
    return {
      ranked: [],
      excluded: [],
      sweptTotal: sweep.sweptTotal,
      sweptConsidered: sweep.sweptConsidered,
      truncated: sweep.truncated,
      relaxedFilters,
    };
  }

  const candidateIds = sweep.candidates.map((r) => r.record_id);

  // Both signals over the candidate set, in parallel.
  const semPromise = semanticRecordRanks(candidateIds, understanding.semantic);
  const lexPromise = lexicalRecordRanks(candidateIds, understanding.semantic);
  const sem = await semPromise;
  const lex = await lexPromise;
  const fitRank = fuseAndNormalize(candidateIds, sem.ranks, lex);

  // Build the candidates the ranker consumes: each record's best evidence chunk (for the
  // approach note + the "why"), its raw semantic distance (kept for legacy callers), and
  // the fused fitRank that the `fit` sub-score now uses in place of that distance.
  const candidates: PlanCandidate[] = sweep.candidates.map((r) => {
    const ev = sem.evidence.get(r.record_id) ?? null;
    return {
      ...(r as RecordRow),
      evidenceDistance: ev ? ev.distance : null,
      evidenceChunk: ev ? { facet: ev.facet, content: ev.content } : null,
      fitRank: fitRank.get(r.record_id) ?? 0,
    };
  });

  const ranked = rankCandidates(candidates, spec).slice(0, spec.top_n);

  return {
    ranked,
    excluded: sweep.excluded,
    sweptTotal: sweep.sweptTotal,
    sweptConsidered: sweep.sweptConsidered,
    truncated: sweep.truncated,
    relaxedFilters,
  };
}
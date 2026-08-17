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
import { rankCandidates, isNonStatement, type PlanCandidate, type RankedCandidate } from "./plan-rank.ts";
import { RRF_K } from "./retrieval.ts";
import { expandTopic } from "./topic-expansion.ts";
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

/** Lexical record ranks over the candidate set: `ts_rank(content_tsv, ...)` as
 * `lib/retrieval.ts`'s `lexicalRank` does it, but reduced to ONE row per record (the max
 * ts_rank across that record's chunks) so it fuses with the per-record semantic rank. No facet
 * filter — name lookups match the identity/people/summary chunks.
 *
 * T49.2 — takes a LIST of terms, OR-ed together, not one string. `plainto_tsquery` ANDs its
 * words, so a single multi-word thesis demanded every word at once and matched almost nothing.
 * OR-ing the expanded topic vocabulary is what lets "climate" reach the record that says
 * "Energy transition wealth creates a distinctive investment thesis" — the tsquery `||`
 * operator composes the parameterised sub-queries, so terms stay bound, never interpolated. */
async function lexicalRecordRanks(
  recordIds: string[],
  terms: string[]
): Promise<Map<string, number>> {
  const ranks = new Map<string, number>();
  const usable = terms.map((t) => t.trim()).filter(Boolean);
  if (recordIds.length === 0 || usable.length === 0) return ranks;
  const pool = getPool();
  // $1 is the record id array; terms bind from $2 upward.
  const tsq = usable.map((_, i) => `plainto_tsquery('english', $${i + 2})`).join(" || ");
  const { rows } = await pool.query(
    `SELECT record_id, MAX(ts_rank(content_tsv, (${tsq}))) AS score
     FROM chunks
     WHERE record_id = ANY($1::text[]) AND content_tsv @@ (${tsq})
     GROUP BY record_id
     ORDER BY score DESC`,
    [recordIds, ...usable]
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

  // T49.2 — BOTH signals rank on the topical residual, not the raw question. Measured on the
  // live corpus: ranking "Which family offices invest in real estate?" put the same hub record
  // (BOSTON FAMILY OFFICE) first for real estate, climate and biotech alike, while ranking
  // "real estate" surfaces GONZALEZ FAMILY OFFICE — an actual real-estate record — and lifts
  // lexical matches from 6 records to 13 (plainto_tsquery ANDs its terms, so the full question
  // demanded 'famili & offic & invest & estate'). An empty residual means a purely structural
  // question ("based in Texas?"), which has no topic to rank on; there the full question is the
  // right fallback and the hq_state filter is what establishes relevance.
  const thesis = understanding.thesis || understanding.semantic;
  // Topic expansion runs concurrently with the embedding round-trip, so it adds vocabulary
  // without adding a serial hop. It only widens the LEXICAL half — the embedding still uses the
  // topic itself, and expansion failure degrades to the bare topic.
  const termsPromise = understanding.thesis ? expandTopic(thesis) : Promise.resolve([thesis]);
  const semPromise = semanticRecordRanks(candidateIds, thesis);
  const terms = await termsPromise;
  const lexPromise = lexicalRecordRanks(candidateIds, terms);
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

  // T49 — relevance gate. Drop a candidate iff it carries NO relevance signal at all:
  //   (1) fitRank === 0 — the record appeared in NEITHER the semantic nor the lexical
  //       rank list, so nothing in the corpus matched the question for it; or
  //   (2) its best evidence chunk is an ingest non-statement marker (e.g. "No investing
  //       thesis or mandate details have been confirmed"), so the closest thing on file
  //       is an assertion of absence — not evidence of fit.
  //   (3) T49.1 — its best evidence sits farther than MAX_EVIDENCE_DISTANCE from the
  //       thesis, i.e. the corpus holds nothing this close to what was asked. (1) and (2)
  //       alone left thesis queries broken: "invest in climate" returned 10 rows, none
  //       about climate, because every candidate with a real chunk gets SOME rank.
  // This is the ONE place "grade, never gate" is overridden, and only for relevance.
  // reach / recency / trust stay scores, never predicates. The gate runs BEFORE
  // rankCandidates so the doomed records do not set effectiveWeights for the records that
  // survive (effectiveWeights renormalizes over whatever set it is handed). When the gate
  // empties the set the route declines through the path it already has — returning nothing
  // is the honest answer to a question this corpus cannot support, and beats 10 wrong rows.
  const { kept, gated } = partitionByRelevance(candidates, { applyDistanceCeiling: !understanding.thesis });
  // T49.2 — for a TOPICAL question, relevance decides who is in the shortlist; the composite
  // still decides the order they are shown in.
  //
  // `rankCandidates` blends fit (0.35) with reach/recency/trust, which is the right ordering for
  // "who should I approach" — but it is the wrong SELECTOR for "who invests in climate". Measured:
  // the 6 genuine climate records reached fit 0.46-0.58 after topic expansion, yet still sat at
  // positions 25-71 because two dozen better-contactable records outscored them on the blend.
  // Taking the top_n by fit and then restoring composite order keeps the prospecting ranking the
  // product is built around, while ensuring the rows are about the thing that was asked.
  // Purely structural questions keep the old behaviour: there is no topic to be relevant TO, so
  // the composite selects, exactly as before.
  const ordered = rankCandidates(kept, spec);
  let ranked: RankedCandidate[];
  if (understanding.thesis) {
    const byFit = [...ordered].sort((a, b) =>
      b.scores.fit !== a.scores.fit
        ? b.scores.fit - a.scores.fit
        : a.record_id < b.record_id ? -1 : a.record_id > b.record_id ? 1 : 0
    );
    const admitted = new Set(byFit.slice(0, spec.top_n).map((r) => r.record_id));
    ranked = ordered.filter((r) => admitted.has(r.record_id));
  } else {
    ranked = ordered.slice(0, spec.top_n);
  }

  // Gated records are reported in the excluded appendix — never silently vanished. The
  // route's candidateCount = sweptConsidered - excluded.length stays correct because the
  // gated rows move out of `ranked` and into `excluded` together.
  const excluded = [...sweep.excluded, ...gated];

  return {
    ranked,
    excluded,
    sweptTotal: sweep.sweptTotal,
    sweptConsidered: sweep.sweptConsidered,
    truncated: sweep.truncated,
    relaxedFilters,
  };
}

// T49.1 — the absolute ceiling on how far a record's best evidence may sit from the thesis
// and still count as an answer. Cosine distance; lower is closer.
//
// BE PRECISE ABOUT WHAT THIS DOES AND DOES NOT FIX. It rejects queries the corpus cannot
// answer at all. It does NOT make topical queries topically precise — see the limitation
// below before assuming it does.
//
// Measured with scripts/calibrate-relevance.ts against the live 508-row corpus on
// 2026-08-17, Xenova/all-MiniLM-L6-v2, using the ACTUAL thesis the pipeline embeds —
// `understanding.semantic`, which is the whole raw question (query-understanding.ts:209).
// Calibrating on a bare topic word instead would be measuring an input this code never sees:
//
//   thesis (as the pipeline embeds it)              median   BEST distance   verdict
//   "Who is Kapor Family Office?" (name lookup)        —         ~0.22       answerable
//   "Which family offices are based in Texas?"         —         ~0.44       answerable
//   "Which family offices invest in real estate?"     0.644        0.262      answerable
//   "Which family offices invest in climate?"         0.681        0.390      answerable*
//   "Which family offices invest in biotechnology?"   0.665        0.446      answerable*
//   "what is the capital of France"                   0.937        0.765      UNANSWERABLE
//
// Real questions bottom out at <=0.45; a question with no purchase on this corpus never gets
// below 0.765. 0.65 sits in that gap with wide margin on both sides, so this constant is not
// finely tuned — anything in ~0.5-0.7 behaves identically on the measured set.
//
// Three cheaper-looking ideas were measured and REJECTED. Do not re-attempt them without new
// measurements:
//   - A *relative* floor (z-score against the query's own distribution). A nonsense query
//     still produces outliers: "underwater basket weaving" has records at z=-2.98 and
//     "capital of France" at z=-3.06 — more extreme than most genuine topical matches.
//     Relative position measures "closest of this set", never "on topic": the same flaw as
//     the positional rank term it would be replacing.
//   - Requiring a LEXICAL hit on the thesis. Too strict: only 1 of 293 candidates matches
//     plainto_tsquery('climate'), discarding the "clean energy"/"decarbonization" records a
//     reader would call relevant.
//   - Tightening this ceiling to separate topics. Impossible as the input stands: the three
//     topical queries above bottom out at 0.262/0.390/0.446, interleaved with each other, so
//     no cutoff splits "climate" from "real estate".
//
// *THE OPEN LIMITATION (PLAN.md T49.2). Within an answerable query this gate does not order
// by topic, because the embedded thesis is the entire question. "Which family offices invest
// in ..." is scaffolding shared with every chunk ("<name>'s investing mandate. ..."), so the
// topic word is roughly one content word in six and barely moves the vector. The measured
// consequence: "Boston Family Office activity. Recent investments: ..." is the SINGLE closest
// record for climate, real estate AND biotechnology alike — one long, centrally-located chunk
// that wins every topical query (embedding hubness). Fixing that means changing the
// representation — embedding a topical residual rather than the raw question, or re-chunking
// so subject matter is not swamped by scaffolding — NOT moving this number.
export const MAX_EVIDENCE_DISTANCE = 0.65;

/** Partitions candidates into kept vs gated on relevance alone. Pure (no DB, no
 * embeddings) so it can be unit-tested directly. A candidate is gated iff:
 *   - fitRank is 0 (no semantic or lexical hit), OR
 *   - its best evidence chunk is a non-statement marker, OR
 *   - its best evidence sits farther than MAX_EVIDENCE_DISTANCE from the thesis.
 * Everything else is graded, not gated. */
export function partitionByRelevance(
  candidates: PlanCandidate[],
  opts: { applyDistanceCeiling?: boolean } = {}
): {
  kept: PlanCandidate[];
  gated: Excluded[];
} {
  // T49.2 — the ceiling applies ONLY on the structural/fallback path, where it was calibrated
  // against full-question distances. On the topical path the thesis is a bare term and the
  // distances shift wholesale: "climate"'s genuinely relevant records sit at 0.758-0.911, which
  // OVERLAPS the junk a no-answer query returns (France bottoms out at 0.793). No cutoff can
  // separate "few relevant records" from "none" there, so applying one would delete the very
  // records this query is supposed to surface. On that path relevance is the LLM judge's job
  // (T49.3): retrieval widens, the judge narrows.
  const applyCeiling = opts.applyDistanceCeiling ?? true;
  const kept: PlanCandidate[] = [];
  const gated: Excluded[] = [];
  for (const c of candidates) {
    const fitRank = c.fitRank ?? 0;
    if (fitRank === 0) {
      gated.push({
        record_id: c.record_id,
        entity_name: c.entity_name,
        reason: "no semantic or keyword match for the question",
      });
      continue;
    }
    if (isNonStatement(c.evidenceChunk?.content)) {
      gated.push({
        record_id: c.record_id,
        entity_name: c.entity_name,
        reason: "no investing thesis on file",
      });
      continue;
    }
    // A missing distance is NOT treated as far away: `fitRank > 0` already proves the record
    // matched at least one signal, and a lexical-only match (an exact name lookup with no
    // embedding row) legitimately has no distance. Gating on null here would break exact-name
    // retrieval — the behavior lib/candidates.ts's header calls the most obviously correct
    // thing this product does.
    if (applyCeiling && c.evidenceDistance != null && c.evidenceDistance > MAX_EVIDENCE_DISTANCE) {
      gated.push({
        record_id: c.record_id,
        entity_name: c.entity_name,
        reason: `evidence too far from the question (${c.evidenceDistance.toFixed(2)} > ${MAX_EVIDENCE_DISTANCE})`,
      });
      continue;
    }
    kept.push(c);
  }
  return { kept, gated };
}

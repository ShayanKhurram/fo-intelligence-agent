// PlanSpec — the input type the ranker (`plan-rank.ts`'s `rankCandidates`) and the
// unified retrieval core (`candidates.ts`'s `selectCandidates`) consume. T44.3 deleted
// the `parsePlanRequest` adapter and the `ParsedPlan` type that lived here — the parser
// is now `understandQuery` in `query-understanding.ts`, and there is no separate plan
// mode. What remains is the type, which `plan-rank.ts` / `candidates.ts` /
// `plan-retrieval.ts` import. It belongs here (it is the plan spec), so it stays.

import type { ParsedFilters } from "./query-understanding.ts";

export type PlanSpec = ParsedFilters & {
  // The semantic residual — the ask's prose, embedded by the retrieval core's
  // best-evidence step. Kept as the full query text so the embedding sees the whole intent.
  thesis: string;
  // Default 10, clamped to [1, 25]. How many offices the shortlist renders.
  top_n: number;
  // ISO date injected by the caller (the route). Recency is computed against this in
  // plan-rank.ts — never against the wall clock, or the ranker is non-deterministic.
  asOf: string;
};
// Retrieval — slimmed in T44.3. `hybridRetrieve` and its private helpers are gone: the
// unified retrieval core in `lib/candidates.ts` replaced them (sweep + semantic +
// lexical fused by reciprocal rank, over the full candidate set, not a top-k fetch).
//
// Two exports remain because live code still imports them:
//   - `RetrievedChunk` — the chunk shape `lib/grounding.ts`'s generation prompt builds on.
//     grounding.ts is untouchable this round, so the type stays here.
//   - `RRF_K` — the reciprocal-rank-fusion constant tuned for this corpus; `candidates.ts`
//     reuses it so the tuning carries over rather than being re-guessed.

export type RetrievedChunk = {
  chunk_id: string;
  record_id: string;
  facet: string;
  content: string;
  entity_name: string;
};

export const RRF_K = 60;
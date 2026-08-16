// Plan-spec parser — micro_rag_plan.md T42.1. Decomposes a "first approach" ask into a
// structured PlanSpec the ranking step (plan-rank.ts) and the retrieval sweep (T42.3)
// consume. Pure: no imports from ./db, ./embeddings, or ./ollama. No LLM call here —
// the 60s route budget has room for exactly one generation pass, and T42.4 spends it on
// the approach notes. This is the same trade-off query-understanding.ts documents at its
// head: a deterministic parser covers the small, regular filter surface at ~0ms.
//
// Filter parsing (entity_type / state / AUM / mandate keywords) is delegated to
// `understandQuery` so there is exactly one parser for those — reusing it rather than
// reimplementing state or AUM parsing is what keeps "family offices in California" and
// "SFOs in Texas with >$500M AUM" producing the same filters here and on the search path.

import {
  understandQuery,
  type ParsedFilters,
  type QueryUnderstanding,
} from "./query-understanding.ts";

export type PlanSpec = ParsedFilters & {
  // The semantic residual — the ask's prose, embedded later by T42.3's best-evidence
  // sweep. Kept as the full query text so the embedding sees the whole intent, the same
  // way `understandQuery` keeps `semantic = query`.
  thesis: string;
  // Default 10, clamped to [1, 25]. How many offices the shortlist renders.
  top_n: number;
  // ISO date injected by the caller (the route). Recency is computed against this in
  // plan-rank.ts — never against the wall clock, or the ranker is non-deterministic.
  asOf: string;
};

// The route needs the intent to decide whether to run the plan pipeline at all, so the
// parsed plan carries it alongside the spec. PlanSpec itself stays filter-only because
// plan-rank.ts's `rankCandidates` consumes a spec and is intent-agnostic.
export type ParsedPlan = PlanSpec & {
  intent: QueryUnderstanding["intent"];
};

export const DEFAULT_TOP_N = 10;
export const MIN_TOP_N = 1;
export const MAX_TOP_N = 25;

/** Parses a "top N" / "first N" / "best N" request out of the query, or returns
 * `undefined` when no count is named. Clamping to [1, 25] happens in `clampTopN`. */
function parseTopN(query: string): number | undefined {
  const m = query.match(/\b(?:top|first|best|leading)\s+(\d{1,3})\b/i);
  return m ? parseInt(m[1], 10) : undefined;
}

function clampTopN(n: number | undefined): number {
  if (n == null || Number.isNaN(n)) return DEFAULT_TOP_N;
  return Math.max(MIN_TOP_N, Math.min(MAX_TOP_N, n));
}

/** A plan request names a raise, asks who/which offices to approach or contact first, or
 * asks for a prioritized/ranked/ordered list. Deliberately narrow: "how many SFOs are in
 * Texas" stays an `aggregate` and "family offices in California" stays a `search` — the
 * plan detector is an addition, not a reclassification of existing intents. */
function hasRaiseAmount(q: string): boolean {
  // A currency figure: "$12M", "$5,000,000", "$1.5B", "$800k", "$12". The `\$` is NOT
  // wrapped in `\b` — a word boundary cannot sit between a space/start and `$` (both are
  // non-word chars), so `\b\$\b` can never match. Anchor on the digit run instead.
  if (/\$\s*\d[\d,.]*(?:\s*[mbk])?\b/i.test(q)) return true;
  // A spelled-out amount: "12 million", "5 billion", "800 thousand".
  if (/\b\d[\d,.]*\s*(?:million|billion|thousand)\b/i.test(q)) return true;
  return false;
}

function hasRaiseWord(q: string): boolean {
  // "raising a round", "raising a seed round", "Series A", "raising capital". "raise"
  // itself is omitted: it is a stem of "raising", which the outer `\braising\b` conjunct
  // already requires, and `\braise\b` cannot match "raising" anyway (no boundary after it).
  return /\b(series|round|seed|capital)\b/i.test(q);
}

function isPlanRequest(query: string): boolean {
  const q = query.toLowerCase();
  // Names a raise: "raising" AND (a money amount OR a raise word like "round"/"seed").
  // The `\braising\b` conjunct is what stops an unrelated sentence that merely mentions a
  // dollar figure ("...that invested $12M last year") from being reclassified as a plan.
  if (/\braising\b/.test(q) && (hasRaiseAmount(q) || hasRaiseWord(q))) {
    return true;
  }
  // Asks which offices to approach / contact / pitch.
  if (/\b(?:which|what)\s+(?:family\s+offices|offices|firms|investors|funds)\s+(?:should\s+i\s+)?(?:to\s+)?(?:approach|contact|reach|pitch)/.test(q)) {
    return true;
  }
  // Asks who to approach / contact / pitch.
  if (/\bwho\s+(?:to|should\s+i)\s+(?:approach|contact|reach|pitch)/.test(q)) {
    return true;
  }
  // "... approach/contact/pitch first/next".
  if (/\b(?:approach|contact|reach\s+out\s+to|pitch)\s+(?:first|next)\b/.test(q)) {
    return true;
  }
  // Prioritized / ranked / ordered list, or "in what order".
  if (/\b(?:prioritized|prioritised|ranked|ordered|sorted)\s+list\b/.test(q)) {
    return true;
  }
  if (/\bin\s+what\s+order\b/.test(q)) {
    return true;
  }
  return false;
}

export function parsePlanRequest(query: string, asOf: string): Promise<ParsedPlan> {
  // `understandQuery` is async (declared so for parity with a possible future LLM-backed
  // implementation), so this function is async too — the route awaits it.
  return understandQuery(query).then((understood) => {
    const intent: QueryUnderstanding["intent"] = isPlanRequest(query)
      ? "plan"
      : understood.intent;
    const spec: PlanSpec = {
      ...understood.filters,
      thesis: query,
      top_n: clampTopN(parseTopN(query)),
      asOf,
    };
    return { ...spec, intent };
  });
}
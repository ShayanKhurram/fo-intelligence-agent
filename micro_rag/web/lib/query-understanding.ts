// Query understanding — micro_rag_plan.md §4.1, heuristic edition. Converts a
// natural-language question into a structured filter + semantic residual + answer shape
// WITHOUT an LLM call. The LLM version cost ~10-15s on Ollama Cloud (its per-call floor);
// since the dataset's filter surface is small and regular (entity_type, US state, an AUM
// range, a set of mandate keywords), a deterministic parser covers the vast majority of
// real queries at ~0ms and leaves generation as the only LLM round-trip.
//
// T44.1 — this is now the ONE parser. `top_n` / `asOf` / `shape` were folded in from the
// deleted `lib/plan-spec.ts` parser. `shape` is derived from question FORM only (a count,
// a single-entity lookup, or a ranked list), never from how the ask is WORDED — the prior
// `isPlanRequest` trigger-phrase list classified 1 of 14 natural phrasings and is gone.
// When the form is ambiguous the answer is `many`, because the wide path is a superset: it
// costs a little latency and never costs correctness. `intent` is retained as a
// shape-derived alias (`count` → `aggregate`, else `search`) so the existing query route
// keeps working unchanged.

export const DEFAULT_TOP_N = 10;
export const MIN_TOP_N = 1;
export const MAX_TOP_N = 25;

export type ParsedFilters = {
  entity_type?: "SFO" | "MFO";
  hq_state?: string;
  aum_min?: number;
  aum_max?: number;
  mandates_any?: string[];
};

// The answer shape — what the route builds from the retrieval result. Form-only:
//   count — a counting question (the existing aggregate regex).
//   one   — names a single entity, or "who is / what is / tell me about X".
//   many  — everything else. THE DEFAULT. Ambiguous → many, because escalating to the
//           wide path costs a little latency and never costs correctness.
export type QueryShape = "count" | "one" | "many";

export type QueryUnderstanding = {
  filters: ParsedFilters;
  semantic: string;
  // T49.2 — the query's TOPIC, with scaffolding and filter-captured terms removed. Empty
  // means the question is purely structural and carries no topic of its own. Retrieval ranks
  // on this when present; `semantic` (the full question) remains the fallback.
  thesis: string;
  top_n: number;
  asOf: string | null;
  shape: QueryShape;
  // Shape-derived alias kept so app/api/query/route.ts switches unchanged:
  // `count` → "aggregate", `one`/`many` → "search". The `"plan"` member is gone (T44.1).
  intent: "search" | "lookup" | "aggregate" | "out_of_scope";
};

/** Turns a parsed filter set into removable UI chips (ui_plan.md §6 "Filter chips" —
 * "parsed from the query, removable, mono labels. Removing one re-runs the search").
 * `key` matches the ParsedFilters property name so the client can delete exactly that
 * key and resubmit as an `overrideFilters` override, skipping re-parsing from text. */
export function filtersToChips(filters: ParsedFilters): { key: string; label: string }[] {
  const chips: { key: string; label: string }[] = [];
  if (filters.entity_type) chips.push({ key: "entity_type", label: filters.entity_type });
  if (filters.hq_state) chips.push({ key: "hq_state", label: filters.hq_state });
  if (filters.aum_min != null && filters.aum_max != null) {
    chips.push({ key: "aum_range", label: `$${fmtUsd(filters.aum_min)}–$${fmtUsd(filters.aum_max)}` });
  } else {
    if (filters.aum_min != null) chips.push({ key: "aum_min", label: `≥ $${fmtUsd(filters.aum_min)}` });
    if (filters.aum_max != null) chips.push({ key: "aum_max", label: `≤ $${fmtUsd(filters.aum_max)}` });
  }
  if (filters.mandates_any && filters.mandates_any.length > 0) {
    chips.push({ key: "mandates_any", label: filters.mandates_any.join(", ") });
  }
  return chips;
}

/** Inverse of a chip's `key` — deletes exactly what that chip represents. `aum_range`
 * is a synthetic key covering both aum_min/aum_max together (see filtersToChips). */
export function removeFilterKey(filters: ParsedFilters, key: string): ParsedFilters {
  const next = { ...filters };
  if (key === "aum_range") {
    delete next.aum_min;
    delete next.aum_max;
  } else {
    delete next[key as keyof ParsedFilters];
  }
  return next;
}

function fmtUsd(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(n % 1e9 === 0 ? 0 : 1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(n % 1e6 === 0 ? 0 : 1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)}K`;
  return String(n);
}

const STATE_CODES = new Set([
  "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA",
  "ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK",
  "OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC",
]);

const STATE_NAMES: Record<string, string> = {
  alabama:"AL",alaska:"AK",arizona:"AZ",arkansas:"AR",california:"CA",colorado:"CO",
  connecticut:"CT",delaware:"DE",florida:"FL",georgia:"GA",hawaii:"HI",idaho:"ID",
  illinois:"IL",indiana:"IN",iowa:"IA",kansas:"KS",kentucky:"KY",louisiana:"LA",maine:"ME",
  maryland:"MD",massachusetts:"MA",michigan:"MI",minnesota:"MN",mississippi:"MS",missouri:"MO",
  montana:"MT",nebraska:"NE",nevada:"NV","new hampshire":"NH","new jersey":"NJ","new mexico":"NM",
  "new york":"NY","north carolina":"NC","north dakota":"ND",ohio:"OH",oklahoma:"OK",oregon:"OR",
  pennsylvania:"PA","rhode island":"RI","south carolina":"SC","south dakota":"SD",tennessee:"TN",
  texas:"TX",utah:"UT",vermont:"VT",virginia:"VA",washington:"WA","west virginia":"WV",
  wisconsin:"WI",wyoming:"WY","washington dc":"DC","district of columbia":"DC",
};

// T49.2 — words stripped from a query to leave its TOPIC, for the embedding + lexical halves
// of retrieval.
//
// Why: `semantic` is the whole raw question, and the measurement in PROJECT_LOG.md shows that
// wrecks both signals. Embedding "Which family offices invest in climate?" puts the topic at
// roughly one content word in six, and the rest ("family offices invest") is scaffolding shared
// with every chunk ("<name>'s investing mandate. ..."), so all topical queries collapse into one
// band — the same hub record was closest for climate, real estate AND biotech alike. The lexical
// half fares worse: plainto_tsquery ANDs its terms, so that question becomes
// 'famili & offic & invest & climat' and matches almost nothing.
//
// Two classes are removed: question scaffolding (interrogatives, verbs, articles) and DOMAIN
// scaffolding — words present in nearly every record and therefore carrying no discriminating
// signal here ("family", "office", "capital", "wealth", "investment"). Entity-specific words
// survive, which is what keeps name lookups working: "Who is Kapor Family Office?" -> "kapor".
const THESIS_SCAFFOLDING = new Set([
  // interrogatives / verbs / function words
  "which", "what", "who", "whom", "whose", "where", "when", "why", "how",
  "is", "are", "was", "were", "be", "been", "am", "do", "does", "did", "has", "have", "had",
  "can", "could", "should", "would", "will", "shall", "may", "might",
  "the", "a", "an", "of", "in", "on", "for", "to", "with", "at", "by", "from", "as", "and", "or",
  "that", "this", "those", "these", "there", "any", "all", "some", "me", "my", "i", "us", "our",
  "you", "your", "it", "its", "about", "into", "out", "up", "down", "please",
  "tell", "show", "find", "list", "give", "get", "want", "need", "looking", "look", "search",
  "help", "know", "see", "name", "names",
  // domain scaffolding — in nearly every record, so it discriminates nothing
  "family", "families", "office", "offices", "firm", "firms", "group", "groups",
  "invest", "invests", "invested", "investing", "investment", "investments",
  "investor", "investors", "fund", "funds", "capital", "wealth", "advisor", "advisors",
  "advisory", "management", "managers", "manager", "partners", "holdings", "llc", "inc",
  "ltd", "lp", "co", "companies", "company",
  // structural phrasing — the SQL filters already handle these
  "based", "located", "location", "headquartered", "hq", "headquarters", "over", "under",
  "above", "below", "more", "less", "than", "top", "best", "biggest", "largest", "smallest",
  "having", "aum", "assets", "under", "record", "records", "dataset",
]);

/** The topical remainder of a query: the question with scaffolding and any term the structured
 * filters already captured removed. Empty means the question carries NO topic of its own — it is
 * purely structural ("which family offices are based in Texas?"), and relevance is established by
 * the SQL filter rather than by similarity. Callers must treat empty as "no topical requirement"
 * rather than as a thesis, or a structural query would be judged against an empty string. */
export function topicalResidual(query: string, filters: ParsedFilters): string {
  let q = ` ${query.toLowerCase().replace(/[^\p{L}\p{N}\s%$.-]/gu, " ")} `;

  // Drop the state the hq_state filter already captured — multi-word names ("west virginia")
  // before single tokens, and the two-letter code too.
  if (filters.hq_state) {
    for (const [name, code] of Object.entries(STATE_NAMES)) {
      if (code === filters.hq_state) q = q.replace(new RegExp(`\\b${name}\\b`, "g"), " ");
    }
    q = q.replace(new RegExp(`\\b${filters.hq_state.toLowerCase()}\\b`, "g"), " ");
  }
  // Entity-type words the entity_type filter already captured.
  if (filters.entity_type) {
    q = q.replace(/\b(single|multi)[\s-]?family\b/g, " ").replace(/\b(sfo|mfo)\b/g, " ");
  }

  return q
    .split(/\s+/)
    .filter(Boolean)
    // Bare numbers / money amounts belong to the AUM filter, not the topic.
    .filter((t) => !/^\$?[\d.,]+(m|b|k|mm|bn|million|billion)?$/.test(t))
    .filter((t) => !THESIS_SCAFFOLDING.has(t))
    .join(" ")
    .trim();
}

// Mandate / sector keywords the dataset actually uses. A hit adds a mandate filter term;
// matching is done against the record's `mandates` text[] with the `&&` overlap operator,
// so partial/keyword terms still match longer mandate strings via lexical retrieval anyway.
const MANDATE_KEYWORDS = [
  "real estate","technology","tech","healthcare","biotech","life sciences","venture",
  "venture capital","private equity","growth equity","buyout","crypto","digital assets",
  "energy","infrastructure","consumer","fintech","financial","industrial","hedge fund",
  "public equities","fixed income","credit","impact","esg","direct","fund of funds",
];

function parseAum(q: string): { aum_min?: number; aum_max?: number } {
  const out: { aum_min?: number; aum_max?: number } = {};
  const unit = (u: string | undefined): number =>
    !u ? 1 : /^b/.test(u) ? 1e9 : /^m/.test(u) ? 1e6 : /^k|^t/.test(u) ? 1e3 : 1;
  const num = (n: string) => parseFloat(n.replace(/,/g, ""));

  // "between X and Y [unit]" / "X to Y [unit]"
  const range = q.match(/(?:between\s+)?\$?\s*(\d[\d,.]*)\s*(billion|million|thousand|b|m|k)?\s*(?:and|to|-|–)\s*\$?\s*(\d[\d,.]*)\s*(billion|million|thousand|b|m|k)?/i);
  if (range) {
    const u2 = unit(range[4] || range[2]);
    out.aum_min = num(range[1]) * unit(range[2] || range[4]);
    out.aum_max = num(range[3]) * u2;
    if (out.aum_min > out.aum_max) [out.aum_min, out.aum_max] = [out.aum_max, out.aum_min];
    return out;
  }

  // directional: "over/at least/more than 100m", "under/below/less than 1 billion"
  for (const m of q.matchAll(/(over|above|more than|greater than|at least|>=?|under|below|less than|fewer than|at most|<=?|around|about|~)\s*\$?\s*(\d[\d,.]*)\s*(billion|million|thousand|b|m|k)?/gi)) {
    const val = num(m[2]) * unit(m[3]);
    const w = m[1].toLowerCase();
    if (/over|above|more|greater|at least|>/.test(w)) out.aum_min = val;
    else if (/under|below|less|fewer|at most|</.test(w)) out.aum_max = val;
    else { out.aum_min = val * 0.75; out.aum_max = val * 1.25; } // "around"
  }
  return out;
}

function detectState(q: string): string | undefined {
  for (const [name, code] of Object.entries(STATE_NAMES)) {
    if (new RegExp(`\\b${name}\\b`, "i").test(q)) return code;
  }
  const codeMatch = q.match(/\b([A-Z]{2})\b/);
  if (codeMatch && STATE_CODES.has(codeMatch[1])) return codeMatch[1];
  return undefined;
}

/** Parses a "top N" / "first N" / "best N" / "leading N" request out of the query, or
 * returns `undefined` when no count is named. Clamping to [1, 25] happens in `clampTopN`. */
function parseTopN(query: string): number | undefined {
  const m = query.match(/\b(?:top|first|best|leading)\s+(\d{1,3})\b/i);
  return m ? parseInt(m[1], 10) : undefined;
}

function clampTopN(n: number | undefined): number {
  if (n == null || Number.isNaN(n)) return DEFAULT_TOP_N;
  return Math.max(MIN_TOP_N, Math.min(MAX_TOP_N, n));
}

// A counting question. This is the existing aggregate regex, unchanged — it was never the
// defect. (The defect was classifying *phrasing* for the plan intent; "how many" is form.)
const COUNT_RE = /\b(how many|how much|what (?:fraction|percentage|share|proportion)|number of|count of|total (?:number|count))\b/;

// A single-entity question form: "who is X", "what is X", "tell me about X". Note "who are"
// / "what are" do NOT match — those name a plural set and stay `many`. Form-only, not
// phrasing: there is no list of entity names or raise words here.
const ONE_RE = /\b(?:who is|what is|tell me about)\b/i;

function detectShape(q: string): QueryShape {
  if (COUNT_RE.test(q)) return "count";
  if (ONE_RE.test(q)) return "one";
  return "many";
}

export async function understandQuery(query: string, asOf?: string): Promise<QueryUnderstanding> {
  const q = query.toLowerCase();
  const filters: ParsedFilters = {};

  // entity type. The trailing `s?` (and `offices?`) matches the plurals "SFOs" / "MFOs" /
  // "single family offices" — the old `\b(sfo|...)\b` needed a word boundary after `sfo`,
  // which "sfos" has none of, so every plural type query parsed with no entity_type filter
  // at all. Bare "family office" is deliberately NOT matched: the phrase appears in nearly
  // every query in this domain and would set entity_type on almost everything. The SFO /
  // MFO distinction stays explicit, and SFO-before-MFO precedence is unchanged.
  if (/\b(sfos?|single[- ]family offices?)\b/i.test(q)) filters.entity_type = "SFO";
  else if (/\b(mfos?|multi[- ]family offices?)\b/i.test(q)) filters.entity_type = "MFO";

  // state
  const state = detectState(query);
  if (state) filters.hq_state = state;

  // AUM
  const aum = parseAum(q);
  if (aum.aum_min != null) filters.aum_min = aum.aum_min;
  if (aum.aum_max != null) filters.aum_max = aum.aum_max;

  // mandates
  const mandates = MANDATE_KEYWORDS.filter((k) => new RegExp(`\\b${k.replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&")}\\b`, "i").test(q));
  if (mandates.length) filters.mandates_any = [...new Set(mandates)];

  // shape — question form only (count / one / many). `many` is the default.
  const shape: QueryShape = detectShape(q);
  // intent — shape-derived alias so the existing query route switches unchanged.
  const intent: QueryUnderstanding["intent"] = shape === "count" ? "aggregate" : "search";

  // `semantic` stays the whole query — it is what the UI echoes and what the LLM judge reads.
  // `thesis` is the topical residual retrieval actually ranks on (T49.2): embedding the full
  // question buried the topic under scaffolding shared with every chunk, and ANDing the full
  // question in plainto_tsquery matched almost nothing.
  return {
    filters,
    semantic: query,
    thesis: topicalResidual(query, filters),
    top_n: clampTopN(parseTopN(query)),
    asOf: asOf ?? null,
    shape,
    intent,
  };
}
// Query understanding — micro_rag_plan.md §4.1, heuristic edition. Converts a
// natural-language question into a structured filter + semantic residual + intent WITHOUT
// an LLM call. The LLM version cost ~10-15s on Ollama Cloud (its per-call floor); since the
// dataset's filter surface is small and regular (entity_type, US state, an AUM range, a set
// of mandate keywords), a deterministic parser covers the vast majority of real queries at
// ~0ms and leaves generation as the only LLM round-trip. Trade-off: subtle out_of_scope /
// lookup classification is lost — those fall back to "search", where the retrieval floor
// (gate1) and no-match messaging already handle irrelevant queries gracefully.

export type ParsedFilters = {
  entity_type?: "SFO" | "MFO";
  hq_state?: string;
  aum_min?: number;
  aum_max?: number;
  mandates_any?: string[];
};

export type QueryUnderstanding = {
  filters: ParsedFilters;
  semantic: string;
  intent: "search" | "lookup" | "aggregate" | "out_of_scope" | "plan";
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

export async function understandQuery(query: string): Promise<QueryUnderstanding> {
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

  // intent: aggregate if it's a counting/comparison question
  const intent: QueryUnderstanding["intent"] =
    /\b(how many|how much|what (?:fraction|percentage|share|proportion)|number of|count of|total (?:number|count))\b/.test(q)
      ? "aggregate"
      : "search";

  // semantic residual = the whole query (embedding the full text is fine; filter words add
  // little noise at this corpus size, and keeping them preserves recall for lexical rank).
  return { filters, semantic: query, intent };
}

// T49.3 — the LLM relevance judge. The last stage of retrieval, and the only one that can
// actually READ a record.
//
// Why this exists: embedding distance cannot tell "about climate" from "not about climate" on
// this corpus. Measured (see MAX_EVIDENCE_DISTANCE in lib/candidates.ts), the single closest
// record for climate, real estate AND biotechnology alike is the same one — "BOSTON FAMILY
// OFFICE activity. Recent investments: ..." — a long, centrally-located chunk that wins every
// topical query. No threshold fixes that, because the ordering itself carries no topical
// signal. A model that reads the chunk can simply see that "Geography focus: Subscribers
// only." does not answer "which family offices invest in climate?".
//
// This runs over the ~10 already-ranked rows, not the corpus, so it is one small call.
//
// FAIL-OPEN, ALWAYS. Every failure path — network error, timeout, unparseable reply, a reply
// naming ids that were never offered — keeps ALL rows. A judge that silently empties the
// shortlist would turn a working answer into "no matches" and look identical to a corpus gap.
// The judge may only ever REMOVE rows it positively identified as irrelevant.

import { ollamaChat, type ChatMessage } from "./ollama.ts";
import type { RankedCandidate } from "./plan-rank.ts";

export type JudgeResult = {
  keep: Set<string>;
  /** Rows the judge positively rejected, with its stated reason. */
  dropped: { record_id: string; entity_name: string }[];
  /** True when the judge ran and was understood. False means fail-open — nothing was dropped. */
  applied: boolean;
};

// Evidence is truncated per record: the judge needs enough to recognise the subject, not the
// whole chunk. Keeps the prompt to roughly one screen for 10 records, which is what holds the
// call near the ~10s floor this backend has.
const EVIDENCE_CHARS = 320;

const SYSTEM = `You decide which retrieved records actually answer a user's question about a
family-office dataset. You are a filter, not a writer.

DEFAULT TO KEEPING. Drop a record only when you can say what makes it wrong for this question.
Removing a correct record is a worse error than keeping a borderline one.

First decide what KIND of question it is:

1. STRUCTURAL — it asks by location, entity type, or size ("based in Texas", "multi-family
   offices", "AUM over $500M"). These filters were ALREADY applied to the data before you saw it,
   so every record shown to you already satisfies them. KEEP ALL OF THEM. Do not drop a record
   because its evidence text fails to restate the filter — evidence that talks about investments,
   news, or strategy instead of the state it is in is still a correct answer.

2. NAMED ORGANISATION — it asks about one specific firm ("Who is Kapor Family Office?"). Keep only
   records that ARE that organisation. Drop other firms even if they look similar.

3. TOPICAL — it asks about a subject or sector ("invest in climate", "back biotech"). Keep a
   record only if its evidence actually concerns that subject. A question about climate is
   answered by evidence mentioning climate, clean energy, decarbonization, sustainability or
   renewables. It is NOT answered by generic filler like "Geography focus: United States",
   "Subscribers only", "thesis-based investments", or by an unrelated sector such as fintech.

Reply with ONLY a JSON array of the numbers you are keeping, e.g. [1,4,7]. Reply [] only when the
question is topical and no record concerns that subject. No prose, no explanation, no code
fences.`;

function buildPrompt(query: string, rows: RankedCandidate[]): string {
  const lines = rows.map((r, i) => {
    const ev = (r.evidenceChunk?.content ?? "").replace(/\s+/g, " ").slice(0, EVIDENCE_CHARS);
    return `${i + 1}. ${r.entity_name}\n   evidence: ${ev || "(none on file)"}`;
  });
  return `Question: ${query}\n\nRecords:\n${lines.join("\n")}\n\nJSON array of the numbers to keep:`;
}

/** Extracts the first JSON array of numbers from a model reply. Tolerates code fences, stray
 * prose, and a bare comma list — anything else is treated as unparseable (fail-open). */
export function parseKeepList(raw: string, count: number): number[] | null {
  if (!raw) return null;
  const match = /\[[^\]]*\]/.exec(raw);
  const body = match ? match[0].slice(1, -1) : raw;
  const nums = body
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
    .map((s) => Number(s));
  // An empty array is a MEANINGFUL answer ("none qualify"), so only a bracketed empty match may
  // produce []. A bare unparseable string must not be read as "keep nothing".
  if (nums.length === 0) return match && match[0].replace(/\s/g, "") === "[]" ? [] : null;
  if (nums.some((n) => !Number.isInteger(n) || n < 1 || n > count)) return null;
  return [...new Set(nums)];
}

export async function judgeRelevance(query: string, rows: RankedCandidate[]): Promise<JudgeResult> {
  const keepAll = (): JudgeResult => ({
    keep: new Set(rows.map((r) => r.record_id)),
    dropped: [],
    applied: false,
  });

  // Nothing to filter, or nothing worth a model call.
  if (rows.length <= 1) return keepAll();

  const messages: ChatMessage[] = [
    { role: "system", content: SYSTEM },
    { role: "user", content: buildPrompt(query, rows) },
  ];

  let raw: string;
  try {
    // "cheapest" (gemma4:31b) on purpose, NOT the "strongest" generation tier. This is
    // classification over text handed to the model verbatim — no reasoning to do — and it sits
    // in the critical path of a route capped at maxDuration = 60 that already spends ~25-30s on
    // generation. A reasoning model here would add its whole chain-of-thought to that budget.
    raw = await ollamaChat(messages, "cheapest");
  } catch {
    return keepAll(); // network / throttle / timeout — never drop rows on an error
  }

  const keepNums = parseKeepList(raw, rows.length);
  if (keepNums === null) return keepAll(); // unparseable — fail open

  const keptRows = keepNums.map((n) => rows[n - 1]);
  return {
    keep: new Set(keptRows.map((r) => r.record_id)),
    dropped: rows
      .filter((r) => !keptRows.includes(r))
      .map((r) => ({ record_id: r.record_id, entity_name: r.entity_name })),
    applied: true,
  };
}

// The grounding control — micro_rag_plan.md §5. Three mechanical gates, none of which
// is a prompt. "The brief is explicit that prompt instructions do not count."
import { getPool } from "./db";
import { ollamaChat, ollamaChatStream } from "./ollama";
import type { RetrievedChunk } from "./retrieval";

const RETRIEVAL_FLOOR = 0.01; // RRF scores are small (1/(60+rank)); tuned empirically at this corpus size

export type FieldFact = { value: string; status: string };
export type RecordFacts = Record<string, Record<string, FieldFact>>; // record_id -> field_name -> fact

const _UNRELIABLE = new Set(["could_not_verify", "removed_failed_validation", "contradicted"]);

/** Gate 3 — status propagation. Builds the fact sheet the model is allowed to see:
 * a field with status could_not_verify/removed_failed_validation/contradicted is
 * simply NOT INCLUDED here, which means the model physically cannot cite it —
 * this is a data-flow control, not an instruction. */
export async function loadRecordFacts(recordIds: string[]): Promise<RecordFacts> {
  if (recordIds.length === 0) return {};
  const pool = getPool();
  const { rows } = await pool.query(
    `SELECT record_id, field_name, value, status FROM provenance WHERE record_id = ANY($1::text[])`,
    [recordIds]
  );
  const facts: RecordFacts = {};
  for (const row of rows) {
    if (_UNRELIABLE.has(row.status)) continue; // Gate 3: never sent to the model
    facts[row.record_id] ??= {};
    facts[row.record_id][row.field_name] = { value: row.value, status: row.status };
  }
  return facts;
}

export type Gate1Decision = "proceed" | "decline_low_score" | "decline_zero_results" | "decline_out_of_scope";

export function gate1(topScore: number, chunkCount: number, intentOutOfScope: boolean): Gate1Decision {
  if (intentOutOfScope) return "decline_out_of_scope";
  if (chunkCount === 0) return "decline_zero_results";
  if (topScore < RETRIEVAL_FLOOR) return "decline_low_score";
  return "proceed";
}

function buildGenerationPrompt(query: string, chunks: RetrievedChunk[], facts: RecordFacts): string {
  const chunkText = chunks
    .map((c, i) => `[${i + 1}] record_id=${c.record_id} entity=${c.entity_name} facet=${c.facet}\n${c.content}`)
    .join("\n\n");

  const factSheets = Object.entries(facts)
    .map(([recordId, fields]) => {
      const lines = Object.entries(fields)
        .map(([field, fact]) => `  ${field} = ${fact.value} (status: ${fact.status})`)
        .join("\n");
      return `record_id=${recordId} confirmed fields:\n${lines}`;
    })
    .join("\n\n");

  return `Retrieved context:\n${chunkText}\n\nConfirmed fields available to cite:\n${factSheets}\n\nQuestion: ${query}`;
}

const GENERATION_SYSTEM = `You answer questions about a family-office dataset using ONLY the retrieved context and
confirmed fields given to you. Every sentence that states a fact MUST end with a tag naming the record and field
it rests on, in the exact format [record_id:field_name] — e.g. "Acme Capital committed $40M in March 2026. [disc_abc123:recent_fund_commitments]"
Only use field names that appear in the "confirmed fields" list for that record_id. Never state a fact you cannot
tag this way. If a field is not in the confirmed list, say it hasn't been confirmed rather than guessing. Do not
invent record_ids or field names.`;

export async function generateAnswer(query: string, chunks: RetrievedChunk[], facts: RecordFacts): Promise<string> {
  const userPrompt = buildGenerationPrompt(query, chunks, facts);
  // Generation runs on the "strongest" tier (glm-5.2) at the user's direction — see T48.
  // glm-5.2 is a reasoning model, but its chain-of-thought is emitted in a sibling
  // `reasoning` field, never in `content` (verified by probing the live endpoint;
  // see lib/ollama.test.ts), so reading only `content` excludes it by construction. The
  // /api/query route caps the whole request at maxDuration = 60 (Vercel Hobby ceiling —
  // one embedding + one generation pass); glm-5.2's reasoning phase still has to fit
  // inside that. Rollback: set OLLAMA_MODEL_STRONGEST (or point this call at "cheapest"/
  // "mid", both still on gemma4:31b) to drop back to a non-reasoning model.
  return ollamaChat(
    [
      { role: "system", content: GENERATION_SYSTEM },
      { role: "user", content: userPrompt },
    ],
    "strongest"
  );
}

/** Streaming twin of `generateAnswer` — same prompt, same tier, but yields content
 * deltas as they arrive so the SSE route can reveal the answer sentence-by-sentence
 * instead of only after the full completion lands. */
export function generateAnswerStream(query: string, chunks: RetrievedChunk[], facts: RecordFacts) {
  const userPrompt = buildGenerationPrompt(query, chunks, facts);
  return ollamaChatStream(
    [
      { role: "system", content: GENERATION_SYSTEM },
      { role: "user", content: userPrompt },
    ],
    "strongest"
  );
}

const TAG_RE = /\[([^\]:]+):([^\]]+)\]/g;

// Abbreviations whose trailing period is NOT a sentence boundary. Entity names in this
// dataset routinely end in these ("Accredited Investors Inc.", "6th Street Advisors L.P."),
// and a naive split on "." would sever the name from its own sentence, producing a bogus
// untagged fragment that then gets stripped. Guarded before splitting, restored after.
const ABBREVIATIONS = ["Inc", "Ltd", "Corp", "Co", "LLC", "L.P", "LP", "L.L.C", "N.A", "S.A", "plc", "Pte", "Cos", "Bros", "St", "U.S", "No"];

// A sentence that explicitly declines to assert an unknown fact ("... has not been
// confirmed", "no email is available") is GOOD grounding, not a fabrication. It carries no
// tag because it makes no citable claim — so it should neither be kept as a fact nor counted
// against the strip threshold. Excluded from the denominator entirely.
const NON_CLAIM_RE = /\b(not|n't|no|none|unconfirmed|unavailable|undisclosed|unknown)\b.*\b(confirmed|available|disclosed|reported|verified|provided|known|found|listed|on file|specified|identified)\b|\b(has not been|hasn't been|could not be|couldn't be|was not|were not|isn't|aren't)\b/i;

export type EntailmentResult = {
  finalAnswer: string;
  strippedSentences: { sentence: string; reason: string }[];
  strippedFraction: number;
  decision: "proceed" | "discard_over_threshold";
};

// A genuine control character (U+0000) built at runtime rather than embedded as a
// literal in this source file, which was previously the actual space character " " —
// meaning `s.split(DOT_SENTINEL).join(".")` below replaced EVERY space in EVERY
// sentence with a period, not just the abbreviation-guarded ones, silently corrupting
// every generated answer into "word.word.word" prose. Found live-testing the new SSE
// stream; the bug predates this refactor (identical value in the pre-ui_plan.md file) —
// the old non-streaming UI rendered the same corrupted text.
const DOT_SENTINEL = String.fromCharCode(0);

/** The generation prompt tells the model to place each sentence's tag AFTER its ending
 * punctuation ("...March 2026. [disc_x:field]"). Splitting naively on sentence-ending
 * punctuation would then orphan the tag onto the FOLLOWING sentence, making the real
 * tagged sentence look untagged (stripped) and mis-attributing the tag to the next one.
 * Pulls a tag that trails sentence-ending punctuation back INSIDE the sentence (before
 * the punctuation), so "FACT. [tag]" becomes "FACT [tag]." and the tag stays with the
 * sentence it annotates. Exported so the SSE route can normalize a streaming buffer the
 * same way before probing it for a finished sentence. */
export function normalizeTagPosition(text: string): string {
  return text.replace(/([.!?])(\s*)(\[[^\]]+\])/g, " $3$1");
}

/** Exported so the SSE route can split a live streaming buffer using the exact same
 * rules as the final, authoritative check — the incremental and final decisions must
 * never disagree about where a sentence boundary falls. */
export function splitSentences(text: string): string[] {
  // Guard abbreviation periods with a sentinel so the sentence splitter can't break on
  // them, split, then restore the periods.
  let guarded = text;
  for (const abbr of ABBREVIATIONS) {
    guarded = guarded.replace(new RegExp(`\\b${abbr.replace(/\./g, "\\.")}\\.`, "g"), `${abbr}${DOT_SENTINEL}`);
  }
  return guarded
    .split(/(?<=[.!?])\s+/)
    .map((s) => s.split(DOT_SENTINEL).join("."))
    .filter((s) => s.trim().length > 0);
}

export type SentenceCheck =
  | { kind: "claim"; display: string; recordId: string; field: string; value: string; status: string }
  | { kind: "neutral"; display: string }
  | { kind: "invalid"; reason: string };

/** The per-sentence half of Gate 2, factored out so both the final whole-answer
 * `verifyEntailment` (below) and the SSE route's incremental per-sentence streaming
 * check run the identical rule — one codepath, two callers, so they can never drift
 * apart on what counts as a validly-grounded sentence. `display` strips the
 * [record_id:field] tag out of the sentence text (the tag becomes a claim pill in the
 * UI, it's never shown as raw bracket syntax). */
export function checkSentence(sentence: string, facts: RecordFacts): SentenceCheck {
  const tags = [...sentence.matchAll(TAG_RE)];
  const display = sentence.replace(TAG_RE, "").replace(/\s+/g, " ").trim();

  if (tags.length === 0) {
    // An explicit "we don't know this" is an honest non-claim — kept in the answer but
    // carries no pill and isn't counted toward the strip threshold; anything else
    // untagged is a fabrication risk and gets stripped.
    if (NON_CLAIM_RE.test(sentence)) return { kind: "neutral", display };
    return { kind: "invalid", reason: "untagged" };
  }

  for (const [, recordId, field] of tags) {
    const recordFacts = facts[recordId.trim()];
    if (!recordFacts || !(field.trim() in recordFacts)) {
      return { kind: "invalid", reason: "tag references an unconfirmed or nonexistent field" };
    }
  }

  // One claim pill per sentence: the FIRST tag drives the pill (a sentence citing
  // multiple fields is rare in practice; the display text carries all the tagged
  // content regardless — only the pill's target record/field is singular).
  const [, recordId, field] = tags[0];
  const fact = facts[recordId.trim()][field.trim()];
  return { kind: "claim", display, recordId: recordId.trim(), field: field.trim(), value: fact.value, status: fact.status };
}

/** Gate 2 — post-generation claim entailment, run in code. Splits the answer into
 * sentences, requires every factual sentence to carry a valid [record_id:field] tag
 * that resolves to a field genuinely present (and settled) in `facts`. Untagged or
 * unresolvable sentences are stripped. Explicit non-claims ("... not confirmed") are
 * neutral — neither kept as fact nor counted against the threshold. If more than 30% of
 * the *claim-bearing* sentences were stripped, the whole answer is discarded (a
 * heavily-patched answer isn't trustworthy just because the survivors passed). */
export function verifyEntailment(rawAnswer: string, facts: RecordFacts): EntailmentResult {
  const normalized = normalizeTagPosition(rawAnswer);
  const sentences = splitSentences(normalized);
  const kept: string[] = [];
  const stripped: { sentence: string; reason: string }[] = [];
  let neutralCount = 0;

  for (const sentence of sentences) {
    const check = checkSentence(sentence, facts);
    if (check.kind === "invalid") {
      stripped.push({ sentence, reason: check.reason });
      continue;
    }
    if (check.kind === "neutral") neutralCount++;
    kept.push(sentence);
  }

  // Denominator is claim-bearing sentences only (total minus honest non-claims) — a summary
  // that's mostly "field X not confirmed" shouldn't trip the discard threshold on its honesty.
  const claimSentences = sentences.length - neutralCount;
  const strippedFraction = claimSentences > 0 ? stripped.length / claimSentences : 0;
  const decision: "proceed" | "discard_over_threshold" = strippedFraction > 0.3 ? "discard_over_threshold" : "proceed";

  return {
    finalAnswer: decision === "proceed" ? kept.join(" ") : "",
    strippedSentences: stripped,
    strippedFraction,
    decision,
  };
}

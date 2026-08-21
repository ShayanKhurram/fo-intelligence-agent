// The grounding control — micro_rag_plan.md §5. Three mechanical gates, none of which
// is a prompt. "The brief is explicit that prompt instructions do not count."
import { getPool } from "./db.ts";
import { ollamaChat, ollamaChatStream } from "./ollama.ts";
import type { RetrievedChunk } from "./retrieval.ts";

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

// T51.4 — the chunk header used to print `facet=<name>`, which reads exactly like the
// `field = value` lines in the confirmed-fields block below it. The model duly cited the
// facet as if it were a field (`[disc_x:why_now]`, `:identity`, `:activity`) — none of
// which are provenance fields, so every such sentence failed Gate 2 and three production
// answers were discarded at strippedFraction 1.0. The facet is a section label, not a
// citable field, so it is rendered as one: `section: <name>`, and the confirmed-fields
// block is the ONLY place in this prompt where a `name = value` pair appears. Deliberately
// NOT fixed by accepting facet names as valid tags — a facet has no provenance status, so
// making it citable would put unverified text behind a claim pill.
export function buildGenerationPrompt(query: string, chunks: RetrievedChunk[], facts: RecordFacts): string {
  const chunkText = chunks
    .map((c, i) => `[${i + 1}] record_id=${c.record_id} entity=${c.entity_name} (section: ${c.facet})\n${c.content}`)
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
Only use field names that appear in the "confirmed fields" list for that record_id. The "section:" label on a
retrieved context block is NOT a field name and must never appear in a tag. Never state a fact you cannot
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

// Any bracket whose body contains a colon is an attempted citation tag. The colon is what
// distinguishes a tag from an ordinary bracket the model might write (`[1]`, `[see below]`),
// which must keep passing through untouched.
//
// T51.5 — this used to be /\[([^\]:]+):([^\]]+)\]/g, which captured the ENTIRE body after
// the first colon as one field name. A model citing two fields in one bracket —
// `[disc_x:check_size_range, disc_x:investing_thesis]`, logged in production — therefore
// resolved to a field literally named "check_size_range, disc_x:investing_thesis", matched
// nothing, and a correctly-grounded sentence was rejected. The body is now parsed into its
// constituent pairs and each one validated.
const TAG_RE = /\[([^\]]*:[^\]]*)\]/g;

type TagPair = { recordId: string; field: string };

/** Parses a tag body into `record_id:field` pairs. `a:b` -> one pair; `a:b, a:c` -> two;
 * `a:b, c` -> two, the bare second part inheriting the preceding record_id (models write
 * both forms). Returns null when the body is not a citation at all, which the caller
 * treats as a malformed tag rather than as an untagged sentence — the model reached for a
 * citation and produced something unusable, and that is not the same as making no claim. */
function parseTagBody(body: string): TagPair[] | null {
  const pairs: TagPair[] = [];
  for (const part of body.split(/\s*[,;]\s*/)) {
    const trimmed = part.trim();
    if (!trimmed) continue;
    const idx = trimmed.indexOf(":");
    if (idx === -1) {
      // A bare field name continues the previous pair's record; with no previous pair
      // there is nothing to attach it to.
      const prev = pairs[pairs.length - 1];
      if (!prev) return null;
      pairs.push({ recordId: prev.recordId, field: trimmed });
      continue;
    }
    const recordId = trimmed.slice(0, idx).trim();
    const field = trimmed.slice(idx + 1).trim();
    if (!recordId || !field || field.includes(":")) return null;
    pairs.push({ recordId, field });
  }
  return pairs.length > 0 ? pairs : null;
}

// Abbreviations whose trailing period is NOT a sentence boundary. Entity names in this
// dataset routinely end in these ("Accredited Investors Inc.", "6th Street Advisors L.P."),
// and a naive split on "." would sever the name from its own sentence, producing a bogus
// untagged fragment that then gets stripped. Guarded before splitting, restored after.
// "Ltda" (Brazilian) added after production split "Specifically, Turim 21 Investimentos
// Ltda." off its own sentence — `\bLtd\.` cannot match it, since the "a" sits between.
// Longer forms must precede their own prefixes for the same reason.
const ABBREVIATIONS = ["Inc", "Ltda", "Ltd", "Corp", "Co", "LLC", "L.P", "LP", "L.L.C", "N.A", "S.A", "plc", "Pte", "Cos", "Bros", "St", "U.S", "No"];

// A sentence that explicitly declines to assert an unknown fact ("... has not been
// confirmed", "no email is available") is GOOD grounding, not a fabrication. It carries no
// tag because it makes no citable claim — so it should neither be kept as a fact nor counted
// against the strip threshold. Excluded from the denominator entirely.
//
// T51.3 — the original single pattern required a negation followed by a verb from a closed,
// fully-inflected list, and production kept producing honest sentences just outside it:
// "no record explicitly lists a New York location" missed because the list held *listed*
// but not *lists*, and "there is no information regarding industrial decarbonization"
// matched nothing at all. Both were stripped as fabrications and their answers discarded.
// The alternatives below cover the absence-of-data forms the model actually writes, and
// match verb STEMS so inflection stops mattering. Kept deliberately negative-anchored: a
// positive assertion ("Alpha Capital invests in real estate") still requires a tag.
const NON_CLAIM_RE = new RegExp(
  [
    // "no record ...", "no information on ...", "no data available"
    "\\bno\\s+(record|records|information|data|entry|entries|field|fields|mention|detail|details|evidence)\\b",
    // "does not contain information", "do not list an address"
    "\\b(does|do|did)\\s+not\\s+(contain|include|list|show|specify|mention|provide|report|indicate|state)\\b",
    // "has not been confirmed", "could not be verified", "cannot be determined"
    "\\b(has|have|had)\\s+not\\s+been\\b",
    "\\b(hasn't|haven't|hadn't)\\s+been\\b",
    "\\b(could|can|would)\\s+not\\s+be\\b",
    "\\b(couldn't|cannot|can't)\\s+be\\b",
    // "was not disclosed", "are not available"
    "\\b(was|were|is|are)\\s+not\\s+(confirmed|available|disclosed|reported|verified|provided|known|found|listed|specified|identified|on file)\\b",
    // negation anywhere, followed by a settled/known STEM in any inflection. `no` belongs
    // in this set: "No other family office has a confirmed real estate investment" is the
    // honest close of a list, and dropping it here quietly made that sentence a fabrication.
    "\\b(not|n't|no|none|unconfirmed|unavailable|undisclosed|unknown)\\b.*(confirm|disclos|verif|report|specif|identif|provid|list|avail|known|found|on file)",
  ].join("|"),
  "i"
);

// T51.2 — a list header ("The following family offices have confirmed AUM over $500
// million:") states no citable fact of its own; it introduces the sentences that do. It
// carries no tag for exactly the same reason an honest non-claim carries none, so it gets
// the same treatment: kept in the prose, no claim pill, excluded from the threshold
// denominator. Production discarded a correct 10-item list partly on this one sentence.
// Anchored on the trailing colon and nothing else — a sentence that ASSERTS something
// ("Canopy Partners has AUM of $640,000,000.") ends in a period and still needs its tag.
const LIST_HEADER_RE = /:\s*$/;

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
  // T51.1 — the same guard, for the period in an ordinal list marker. Splitting on
  // "(?<=[.!?])\s+" alone treats the "1." of "1. Alpha Capital has AUM of ... [tag]" as a
  // complete sentence, so every item of a markdown numbered list contributed a phantom
  // untagged "sentence" that Gate 2 then stripped. A fully-grounded 10-item list scored
  // strippedFraction 0.5 and was discarded in production for having formatting. Anchored
  // to the start of a line so a sentence that merely ends in a number ("...raised $500.
  // 3 offices matched.") is untouched.
  // The `**` allowance is not decoration: the model routinely bolds its list markers
  // ("**1.** Nolet Wealth Management"), and without it the marker is not at line start,
  // the guard misses, and the phantom-sentence bug comes straight back for that answer.
  guarded = guarded.replace(/(^|\n)([ \t]*(?:\*\*|__|\*)?\s*\d+)\.(?=\s|\*)/g, `$1$2${DOT_SENTINEL}`);
  return guarded
    // Two kinds of boundary: sentence-ending punctuation, and the newline before a list
    // marker. The second is what keeps a header and its first item apart now that the
    // marker's own period no longer splits — and it means a list item with no terminal
    // punctuation is still its own sentence rather than being glued to the next one.
    // `\d+[.)\0]` matches a marker whose period is already sentinel-guarded above.
    .split(/(?<=[.!?])\s+|\n+(?=[ \t]*(?:(?:\*\*|__|\*)?\s*\d+[.)\0]|[-*•]\s))/)
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
  // Removing the tag leaves the space that separated it from the sentence. Because
  // `normalizeTagPosition` deliberately parks the tag just BEFORE the closing punctuation
  // ("FACT [tag]."), that orphaned space always lands right in front of it — so every
  // claim sentence rendered as "…$1,200,000,000 ." in the UI. Close the gap.
  const display = sentence
    .replace(TAG_RE, "")
    .replace(/\s+/g, " ")
    .replace(/\s+([.,;:!?])/g, "$1")
    .trim();

  if (tags.length === 0) {
    // An explicit "we don't know this", and a list header that only introduces the
    // sentences below it, are honest non-claims — kept in the answer but they carry no
    // pill and aren't counted toward the strip threshold; anything else untagged is a
    // fabrication risk and gets stripped.
    if (NON_CLAIM_RE.test(sentence) || LIST_HEADER_RE.test(sentence.trim())) {
      return { kind: "neutral", display };
    }
    return { kind: "invalid", reason: "untagged" };
  }

  // Every pair in every bracket must resolve — a sentence citing two fields is only as
  // grounded as its weaker citation.
  const pairs: TagPair[] = [];
  for (const [, body] of tags) {
    const parsed = parseTagBody(body);
    if (!parsed) return { kind: "invalid", reason: "malformed citation tag" };
    pairs.push(...parsed);
  }

  for (const { recordId, field } of pairs) {
    const recordFacts = facts[recordId];
    if (!recordFacts || !(field in recordFacts)) {
      return { kind: "invalid", reason: "tag references an unconfirmed or nonexistent field" };
    }
  }

  // One claim pill per sentence: the FIRST tag drives the pill (a sentence citing
  // multiple fields is rare in practice; the display text carries all the tagged
  // content regardless — only the pill's target record/field is singular).
  const { recordId, field } = pairs[0];
  const fact = facts[recordId][field];
  return { kind: "claim", display, recordId, field, value: fact.value, status: fact.status };
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

  // T51.6 — `finalAnswer` is now always the sentences that survived, whatever the verdict.
  // It used to be blanked on discard, back when discarding meant the user saw none of them.
  // The streaming path shows each sentence the moment it passes, and a discard no longer
  // retracts what is on screen, so blanking this would make `query_log.final_answer`
  // disagree with what was actually delivered. `decision` still carries the verdict.
  return {
    finalAnswer: kept.join(" "),
    strippedSentences: stripped,
    strippedFraction,
    decision,
  };
}

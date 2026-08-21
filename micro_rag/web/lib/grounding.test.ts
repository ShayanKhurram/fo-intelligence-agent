// T51.7 — grounding.ts acceptance. Run with `npm test` (node --test, zero deps, no
// network and no DATABASE_URL: every function under test is pure and is given literal
// facts).
//
// This module carries all three grounding gates and was the only major lib with no test
// file, which is how T51.1–T51.5 reached production: Gate 2 was discarding correct,
// fully-grounded answers because of FORMATTING (list numbering, a header sentence, an
// honest "no data" line), and the user watched a right answer get replaced by a fallback
// string. Each block below names the subtask it pins down.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  splitSentences,
  normalizeTagPosition,
  checkSentence,
  verifyEntailment,
  buildGenerationPrompt,
  type RecordFacts,
} from "./grounding.ts";
import type { RetrievedChunk } from "./retrieval.ts";

const FACTS: RecordFacts = {
  rec_a: {
    aum_usd: { value: "1200000000", status: "confirmed" },
    check_size_range: { value: "up to $35M", status: "confirmed" },
    investing_thesis: { value: "generalist", status: "confirmed" },
  },
  rec_b: { aum_usd: { value: "890000000", status: "confirmed" } },
  rec_c: { aum_usd: { value: "640000000", status: "confirmed" } },
};

function kinds(answer: string, facts: RecordFacts = FACTS) {
  return splitSentences(normalizeTagPosition(answer)).map((s) => checkSentence(s, facts).kind);
}

// ---------------------------------------------------------------- T51.1 list numbering

test("T51.1: a numbered list keeps each item whole instead of splitting its marker off", () => {
  const answer = `The following family offices have confirmed AUM over $500 million:

1. Alpha Capital has AUM of $1,200,000,000. [rec_a:aum_usd]
2. Beta Holdings has AUM of $890,000,000. [rec_b:aum_usd]
3. Gamma Office has AUM of $640,000,000. [rec_c:aum_usd]`;

  const sentences = splitSentences(normalizeTagPosition(answer));
  // One header + three items. Before T51.1 this produced six segments, three of them the
  // bare strings "1.", "2.", "3.".
  assert.equal(sentences.length, 4);
  assert.ok(!sentences.some((s) => /^\d+\.$/.test(s.trim())), "no bare ordinal segments");
  assert.ok(sentences[1].startsWith("1. Alpha Capital"), "the marker stays with its item");

  const result = verifyEntailment(answer, FACTS);
  assert.equal(result.strippedFraction, 0);
  assert.equal(result.decision, "proceed");
  assert.deepEqual(result.strippedSentences, []);
});

test("T51.1: a ten-item grounded list proceeds (this exact shape was discarded at 0.5)", () => {
  const facts: RecordFacts = {};
  const items: string[] = [];
  for (let i = 0; i < 10; i++) {
    const id = `rec_${i}`;
    facts[id] = { aum_usd: { value: `${i}`, status: "confirmed" } };
    items.push(`${i + 1}. Office ${i} has AUM of $${i}00,000,000. [${id}:aum_usd]`);
  }
  const answer = `The following family offices qualify:\n\n${items.join("\n")}`;
  const result = verifyEntailment(answer, facts);
  assert.equal(result.decision, "proceed");
  assert.equal(result.strippedFraction, 0);
});

test("T51.1: list items with no terminal punctuation still split on the marker", () => {
  const answer = "Matching offices:\n1. Alpha Capital [rec_a:aum_usd]\n2. Beta Holdings [rec_b:aum_usd]";
  assert.deepEqual(kinds(answer), ["neutral", "claim", "claim"]);
});

test("T51.1: bullet lists are not regressed", () => {
  const answer = "Matching offices:\n*   Alpha Capital [rec_a:aum_usd]\n*   Beta Holdings [rec_b:aum_usd]";
  assert.deepEqual(kinds(answer), ["neutral", "claim", "claim"]);
});

test("T51.1: a sentence merely ENDING in a number is not treated as a list marker", () => {
  // The ordinal guard is anchored to the start of a line precisely so this still splits.
  const answer = "Alpha raised $500 [rec_a:aum_usd]. Beta followed [rec_b:aum_usd].";
  assert.equal(splitSentences(normalizeTagPosition(answer)).length, 2);
});

// ------------------------------------------------------------------- T51.2 list headers

test("T51.2: a list header is neutral, not a failed claim", () => {
  assert.equal(checkSentence("The following family offices have confirmed AUM over $500 million:", FACTS).kind, "neutral");
  assert.equal(checkSentence("Based on the retrieved context, here are the matching offices:", FACTS).kind, "neutral");
});

test("T51.2: an untagged factual assertion is still invalid", () => {
  assert.equal(checkSentence("Canopy Partners has AUM of $640,000,000.", FACTS).kind, "invalid");
  assert.equal(checkSentence("Alpha Capital invests in real estate.", FACTS).kind, "invalid");
});

test("T51.2: header + one tagged claim proceeds rather than discarding at 0.5", () => {
  const result = verifyEntailment("Matching offices:\nAlpha Capital has AUM of $1.2B. [rec_a:aum_usd]", FACTS);
  assert.equal(result.decision, "proceed");
  assert.equal(result.strippedFraction, 0);
});

// --------------------------------------------------------------- T51.3 honest non-claims

test("T51.3: absence-of-data sentences are neutral regardless of verb inflection", () => {
  const honest = [
    "Based on the available confirmed fields, no record explicitly lists a New York location.",
    "Based on the provided records, there is no information regarding industrial decarbonization.",
    "The provided context does not contain information about the capital of France.",
    "The email address has not been confirmed.",
    "The AUM figure could not be verified.",
    "Its investing thesis was not disclosed.",
  ];
  for (const s of honest) {
    assert.equal(checkSentence(s, FACTS).kind, "neutral", `should be neutral: ${s}`);
  }
});

test("T51.3: widening the non-claim rule did not make positive assertions neutral", () => {
  const claims = [
    "Alpha Capital invests in real estate.",
    "Beta Holdings is headquartered in Texas.",
    "Gamma Office committed $40M in March 2026.",
  ];
  for (const s of claims) {
    assert.equal(checkSentence(s, FACTS).kind, "invalid", `should be invalid untagged: ${s}`);
  }
});

test("T51.3: an all-neutral answer does not trip the discard threshold", () => {
  const result = verifyEntailment("No record explicitly lists a New York location.", FACTS);
  assert.equal(result.decision, "proceed");
  assert.equal(result.strippedFraction, 0);
});

// ------------------------------------------------------------------ T51.4 facet leakage

const CHUNK: RetrievedChunk = {
  chunk_id: "rec_a::why_now",
  record_id: "rec_a",
  facet: "why_now",
  content: "Recently promoted two senior partners.",
  entity_name: "Alpha Capital",
};

test("T51.4: the generation prompt never renders a facet as `facet=<name>`", () => {
  const prompt = buildGenerationPrompt("who is active?", [CHUNK], FACTS);
  assert.ok(!/facet\s*=/.test(prompt), "no facet= token, which the model was citing as a field");
  assert.match(prompt, /\(section: why_now\)/);
});

test("T51.4: the confirmed-fields block is the only `name = value` surface", () => {
  const prompt = buildGenerationPrompt("who is active?", [CHUNK], FACTS);
  assert.match(prompt, /Confirmed fields available to cite:/);
  assert.match(prompt, /aum_usd = 1200000000 \(status: confirmed\)/);
});

test("T51.4: a facet name is still not citable as a field", () => {
  // The fix is in the prompt, not the validator — if the model cites a facet anyway, the
  // sentence must still fail. Making facets citable would put unverified text behind a pill.
  assert.equal(checkSentence("Alpha is active. [rec_a:why_now]", FACTS).kind, "invalid");
});

// -------------------------------------------------------------- T51.5 multi-field tags

test("T51.5: a comma-joined tag resolves each pair and keeps one pill", () => {
  const check = checkSentence("Alpha writes checks up to $35 million [rec_a:check_size_range, rec_a:investing_thesis].", FACTS);
  assert.equal(check.kind, "claim");
  assert.equal(check.kind === "claim" && check.recordId, "rec_a");
  assert.equal(check.kind === "claim" && check.field, "check_size_range");
  assert.equal(check.kind === "claim" && check.display, "Alpha writes checks up to $35 million.");
});

test("T51.5: a comma-joined tag is invalid when any one pair does not resolve", () => {
  assert.equal(checkSentence("Alpha writes big checks [rec_a:check_size_range, rec_a:nope].", FACTS).kind, "invalid");
});

test("T51.5: a bare field after a comma inherits the preceding record id", () => {
  const check = checkSentence("Alpha writes big checks [rec_a:check_size_range, investing_thesis].", FACTS);
  assert.equal(check.kind, "claim");
});

test("T51.5: single tags and multiple brackets behave as before", () => {
  assert.equal(checkSentence("Alpha has AUM of $1.2B [rec_a:aum_usd].", FACTS).kind, "claim");
  assert.equal(checkSentence("Alpha and Beta both qualify [rec_a:aum_usd][rec_b:aum_usd].", FACTS).kind, "claim");
  assert.equal(checkSentence("Alpha and Beta both qualify [rec_a:aum_usd][rec_b:nope].", FACTS).kind, "invalid");
});

test("T51.5: a bracket with no colon is not a citation tag", () => {
  // "[1]" is ordinary prose, not an attempted citation — it must not make the sentence
  // look tagged, and it must not be stripped out of the display text.
  const check = checkSentence("See the note [1] for detail. [rec_a:aum_usd]", FACTS);
  assert.equal(check.kind, "claim");
  assert.ok(check.kind === "claim" && check.display.includes("[1]"));
  assert.equal(checkSentence("Alpha qualifies [1].", FACTS).kind, "invalid");
});

// -------------------------------------------------------- T51.6 discard keeps survivors

test("T51.6: a discarded answer still reports the sentences that survived", () => {
  // Three untagged assertions against one valid claim: 3/4 = 0.75 > 0.3.
  const answer =
    "Alpha Capital has AUM of $1,200,000,000. [rec_a:aum_usd] Beta is in Texas. Gamma is in Ohio. Delta is in Maine.";
  const result = verifyEntailment(answer, FACTS);
  assert.equal(result.decision, "discard_over_threshold");
  assert.ok(result.strippedFraction > 0.3);
  // finalAnswer used to be blanked here, which made query_log disagree with what the
  // streaming path had already put on screen.
  assert.match(result.finalAnswer, /Alpha Capital has AUM/);
});

test("T51.6: a generation with nothing valid leaves no survivors to keep", () => {
  const result = verifyEntailment("<pad><pad><pad><pad>", FACTS);
  assert.equal(result.decision, "discard_over_threshold");
  assert.equal(result.finalAnswer, "");
});

// ------------------------------------------------- pre-existing behaviour, not regressed

test("abbreviations do not split a sentence away from its own entity name", () => {
  const answer = "Accredited Investors Inc. has AUM of $1.2B [rec_a:aum_usd].";
  const sentences = splitSentences(normalizeTagPosition(answer));
  assert.equal(sentences.length, 1);
  assert.ok(sentences[0].startsWith("Accredited Investors Inc."));
});

test("normalizeTagPosition pulls a trailing tag back across its period", () => {
  assert.equal(
    normalizeTagPosition("Alpha committed $40M in March 2026. [rec_a:aum_usd]"),
    "Alpha committed $40M in March 2026 [rec_a:aum_usd]."
  );
  // ...so the tag is checked against the sentence it annotates, not the following one.
  assert.equal(kinds("Alpha committed $40M. [rec_a:aum_usd] Beta followed. [rec_b:aum_usd]").join(","), "claim,claim");
});

test("DOT_SENTINEL is a control character, not a space", () => {
  // It was once a literal " ", so restoring it turned every space into a period and
  // corrupted every answer into "word.word.word". Any space-shaped sentinel fails here.
  const s = splitSentences("Alpha Capital has a large mandate.");
  assert.equal(s[0], "Alpha Capital has a large mandate.");
});

test("the threshold denominator excludes neutrals", () => {
  // 1 claim + 1 stripped + 3 neutrals -> 1/2, not 1/5.
  const answer = [
    "Alpha has AUM of $1.2B. [rec_a:aum_usd]",
    "Beta is in Texas.",
    "The email has not been confirmed.",
    "No record lists a phone number.",
    "Matching offices:",
  ].join("\n");
  const result = verifyEntailment(answer, FACTS);
  assert.equal(result.strippedFraction, 0.5);
});

// T42.7 — query-understanding plural SFO/MFO matching. Run with `npm test`.
// `understandQuery` is async; imports use the explicit .ts extension (no @/ alias).

import { test } from "node:test";
import assert from "node:assert/strict";

import { understandQuery } from "./query-understanding.ts";

test("SFOs (plural, upper) parses entity_type SFO", async () => {
  const u = await understandQuery("which SFOs should I approach first");
  assert.equal(u.filters.entity_type, "SFO");
});

test("sfos (plural, lower) parses entity_type SFO", async () => {
  const u = await understandQuery("sfos with over $500M AUM");
  assert.equal(u.filters.entity_type, "SFO");
});

test('"single family offices" (plural phrase) parses entity_type SFO', async () => {
  const u = await understandQuery("single family offices in New York");
  assert.equal(u.filters.entity_type, "SFO");
});

test('"single-family offices" (hyphenated plural) parses entity_type SFO', async () => {
  const u = await understandQuery("single-family offices in Texas");
  assert.equal(u.filters.entity_type, "SFO");
});

test("MFOs (plural) parses entity_type MFO", async () => {
  const u = await understandQuery("MFOs in California");
  assert.equal(u.filters.entity_type, "MFO");
});

test('"multi-family offices" (hyphenated plural) parses entity_type MFO', async () => {
  const u = await understandQuery("multi-family offices with over $1B AUM");
  assert.equal(u.filters.entity_type, "MFO");
});

test('"multi family offices" (spaced plural) parses entity_type MFO', async () => {
  const u = await understandQuery("multi family offices in Florida");
  assert.equal(u.filters.entity_type, "MFO");
});

test("singular SFO still parses (no regression)", async () => {
  const u = await understandQuery("an SFO in Wyoming");
  assert.equal(u.filters.entity_type, "SFO");
});

test("singular MFO still parses (no regression)", async () => {
  const u = await understandQuery("an MFO in Wyoming");
  assert.equal(u.filters.entity_type, "MFO");
});

test('"single family office" (singular phrase) still parses SFO', async () => {
  const u = await understandQuery("a single family office in Oregon");
  assert.equal(u.filters.entity_type, "SFO");
});

test('bare "family offices in California" does NOT set entity_type', async () => {
  // The phrase appears in nearly every query in this domain; matching it would set
  // entity_type on almost everything. The SFO/MFO distinction must stay explicit.
  const u = await understandQuery("family offices in California");
  assert.equal(u.filters.entity_type, undefined);
  assert.equal(u.filters.hq_state, "CA");
});

test('"how many SFOs are in Texas" parses aggregate + entity_type SFO + state TX', async () => {
  const u = await understandQuery("how many SFOs are in Texas");
  assert.equal(u.intent, "aggregate");
  assert.equal(u.filters.entity_type, "SFO");
  assert.equal(u.filters.hq_state, "TX");
});
// --- T44.1: one parser, shape from question form, no phrasing test survives. ----------
// The 14 natural phrasings from the 2026-08-17 recall measurement (scripts/query_baseline.mjs)
// all yield shape "many" — the measurement that failed at 1/14 and is the point of T44.1.
const FOURTEEN_PHRASINGS = [
  "I am raising a $12M Series A for a US industrial-decarbonization company — which family offices should I approach first?",
  "I need to raise $12M for a climate startup. Where do I start?",
  "Who are the best family offices for my clean energy round?",
  "Help me build an outreach list for my seed round",
  "Rank family offices by fit for industrial decarbonization",
  "I am fundraising for a hardware company — best offices to target?",
  "Give me a target list of family offices for a $5M raise",
  "Which offices are most likely to take a meeting about my Series A?",
  "shortlist family offices for my raise",
  "Top 10 family offices to pitch for climate tech",
  "I want to talk to family offices about investing in my company",
  "who should I go to for a $3M round",
  "build me a pipeline of family offices for fintech",
  "best family offices to approach for a real estate fund",
];

test("all 14 natural plan phrasings yield shape many", async () => {
  for (const q of FOURTEEN_PHRASINGS) {
    const u = await understandQuery(q);
    assert.equal(u.shape, "many", `expected many: ${q}`);
  }
});

test('"how many SFOs are in Texas" yields shape count', async () => {
  const u = await understandQuery("how many SFOs are in Texas");
  assert.equal(u.shape, "count");
  assert.equal(u.filters.entity_type, "SFO");
  assert.equal(u.filters.hq_state, "TX");
});

test('"who is Kapor Family Office" yields shape one', async () => {
  const u = await understandQuery("who is Kapor Family Office");
  assert.equal(u.shape, "one");
});

test('"family offices in California" yields shape many', async () => {
  const u = await understandQuery("family offices in California");
  assert.equal(u.shape, "many");
  assert.equal(u.filters.hq_state, "CA");
});

test('"who are the best family offices" stays many (who are, not who is)', async () => {
  // The plural "who are" must NOT trigger shape one — a plural set is `many`.
  const u = await understandQuery("Who are the best family offices for my clean energy round?");
  assert.equal(u.shape, "many");
});

test("top_n clamps to [1,25] with default 10", async () => {
  assert.equal((await understandQuery("top 100 family offices")).top_n, 25);
  assert.equal((await understandQuery("top 0 family offices")).top_n, 1);
  assert.equal((await understandQuery("family offices in California")).top_n, 10);
});

test("asOf is echoed when supplied and null when not (never new Date in here)", async () => {
  assert.equal((await understandQuery("family offices", "2026-08-17")).asOf, "2026-08-17");
  assert.equal((await understandQuery("family offices")).asOf, null);
});

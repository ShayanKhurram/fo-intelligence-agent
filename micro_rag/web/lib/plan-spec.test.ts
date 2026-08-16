// T42.1 — plan-spec.ts acceptance. Run with `npm test` (node --test, zero deps).
// Node 24.18 strips TypeScript types natively; imports use the explicit .ts extension
// because the @/ path alias does not resolve under bare node.

import { test } from "node:test";
import assert from "node:assert/strict";

import { parsePlanRequest, DEFAULT_TOP_N, MIN_TOP_N, MAX_TOP_N } from "./plan-spec.ts";
import { understandQuery } from "./query-understanding.ts";

const AS_OF = "2026-08-16";

const PLAN_QUERY =
  "I'm raising a $12M Series A for a US industrial-decarbonization company — which family offices should I approach first?";

test("a raise + approach-first query parses to a plan intent with the thesis preserved", async () => {
  const parsed = await parsePlanRequest(PLAN_QUERY, AS_OF);
  assert.equal(parsed.intent, "plan");
  // The thesis text is preserved (verbatim query) so T42.3 can embed it.
  assert.equal(parsed.thesis, PLAN_QUERY);
  assert.equal(parsed.asOf, AS_OF);
});

test("a plain dollar amount with raising language is a plan (raise detector regression)", async () => {
  // No "Series", no "approach first" — the canonical query only passed on those
  // crutches. A bare "$12M" must be recognised as a raise amount.
  const parsed = await parsePlanRequest("I am raising $12M and need family offices", AS_OF);
  assert.equal(parsed.intent, "plan");
});

test("a comma-form dollar amount with raising language is a plan", async () => {
  const parsed = await parsePlanRequest("raising $5,000,000 for a climate company", AS_OF);
  assert.equal(parsed.intent, "plan");
});

test('"raising a round" with no amount is still a plan, via the raise word', async () => {
  const parsed = await parsePlanRequest("raising a round", AS_OF);
  assert.equal(parsed.intent, "plan");
});

test("a dollar figure with no raise language is NOT a plan (negative)", async () => {
  // A dollar figure alone must not reclassify a search as a plan — the `\braising\b`
  // conjunct is what keeps this from over-firing.
  const parsed = await parsePlanRequest(
    "family offices in California that invested $12M last year",
    AS_OF,
  );
  assert.notEqual(parsed.intent, "plan");
});

test('"how many SFOs are in Texas" stays aggregate, not plan', async () => {
  const parsed = await parsePlanRequest("how many SFOs are in Texas", AS_OF);
  assert.equal(parsed.intent, "aggregate");
});

test('"family offices in California" stays search, not plan', async () => {
  const parsed = await parsePlanRequest("family offices in California", AS_OF);
  assert.equal(parsed.intent, "search");
});

test("top_n clamps: 100 -> 25, 0 -> 1, absent -> 10", async () => {
  const hi = await parsePlanRequest("top 100 family offices to approach first", AS_OF);
  assert.equal(hi.intent, "plan");
  assert.equal(hi.top_n, MAX_TOP_N);

  const lo = await parsePlanRequest("top 0 family offices to approach first", AS_OF);
  assert.equal(lo.top_n, MIN_TOP_N);

  const absent = await parsePlanRequest(PLAN_QUERY, AS_OF);
  assert.equal(absent.top_n, DEFAULT_TOP_N);
});

test("filters on the PlanSpec match understandQuery for a state + AUM text", async () => {
  const text = "family offices in California with over $500M AUM";
  const parsed = await parsePlanRequest(text, AS_OF);
  const understood = await understandQuery(text);
  assert.equal(parsed.hq_state, understood.filters.hq_state);
  assert.equal(parsed.aum_min, understood.filters.aum_min);
  assert.equal(parsed.aum_max, understood.filters.aum_max);
  // And the state/AUM actually parsed to something, so this is not a vacuous equality.
  assert.equal(parsed.hq_state, "CA");
  assert.equal(parsed.aum_min, 500_000_000);
});
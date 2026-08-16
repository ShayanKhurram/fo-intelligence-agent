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
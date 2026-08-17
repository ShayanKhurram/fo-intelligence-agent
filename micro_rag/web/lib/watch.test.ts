// T46.1 D8 — toIsoDate acceptance. Run with `npm test` (node --test, zero deps).
// Postgres returns both `timestamptz` and `date` as JS `Date` objects; `toIsoDate` is the
// single helper that turns either into a `YYYY-MM-DD` string (or null), so the
// lexicographic freshest-first sort in `newestSignalDate` never sees "Fri Aug 14"-style
// garbage. Importing `watch.ts` is safe — it has no top-level DB calls.

import { test } from "node:test";
import assert from "node:assert/strict";

import { toIsoDate } from "./watch.ts";

test("toIsoDate: a JS Date → YYYY-MM-DD", () => {
  const d = new Date("2026-08-16T12:34:56.000Z");
  assert.equal(toIsoDate(d), "2026-08-16");
});

test("toIsoDate: an ISO date string → YYYY-MM-DD", () => {
  assert.equal(toIsoDate("2026-08-16"), "2026-08-16");
  assert.equal(toIsoDate("2026-08-16T12:00:00Z"), "2026-08-16");
});

test("toIsoDate: null → null", () => {
  assert.equal(toIsoDate(null), null);
});

test("toIsoDate: undefined → null", () => {
  assert.equal(toIsoDate(undefined), null);
});

test("toIsoDate: an unparseable string → null (never a fabricated date)", () => {
  assert.equal(toIsoDate("garbage"), null);
  assert.equal(toIsoDate("Fri Aug 14 2026 00:00:00 GMT+0500"), "2026-08-13");
});

test("toIsoDate: never returns the String(date) form 'Fri Aug 14'", () => {
  // A Postgres DATE comes back as a Date at midnight local; the result must always be
  // \d{4}-\d{2}-\d{2}, never the junk that `String(date).slice(0,10)` produced.
  const d = new Date(2026, 7, 14); // local midnight
  const out = toIsoDate(d);
  assert.ok(out && /^\d{4}-\d{2}-\d{2}$/.test(out), `bad shape: ${out}`);
});
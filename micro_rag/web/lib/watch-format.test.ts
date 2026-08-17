// T46.4 — tests for the Intent Watcher's presentation helpers. The rule these enforce is
// the same one that governs the whole feature: never render information the data does not
// carry. A missing date must not become today; an unknown ETA must not become a number.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  boardSummary,
  formatAum,
  formatEta,
  formatRelative,
  formatShortDate,
  kindMeta,
  metaLine,
  KIND_ORDER,
} from "./watch-format.ts";

const NOW = new Date("2026-08-17T12:00:00Z");

test("formatRelative: null/blank/garbage all read 'date unknown', never a guessed date", () => {
  assert.equal(formatRelative(null, NOW), "date unknown");
  assert.equal(formatRelative(undefined, NOW), "date unknown");
  assert.equal(formatRelative("", NOW), "date unknown");
  assert.equal(formatRelative("Fri Aug 14", NOW), "date unknown"); // the D8 corruption shape
});

test("formatRelative: real spans", () => {
  assert.equal(formatRelative("2026-08-17", NOW), "today");
  assert.equal(formatRelative("2026-08-16", NOW), "1d");
  assert.equal(formatRelative("2026-08-15", NOW), "2d");
  assert.equal(formatRelative("2026-08-01", NOW), "2w");
  assert.equal(formatRelative("2026-05-17", NOW), "3mo");
});

test("formatRelative: a future-dated source never renders a negative age", () => {
  assert.equal(formatRelative("2026-09-01", NOW), "today");
});

test("formatShortDate: returns null when undated so the caller drops the segment", () => {
  assert.equal(formatShortDate("2026-08-15"), "Aug 15");
  assert.equal(formatShortDate(null), null);
  assert.equal(formatShortDate("nonsense"), null);
});

test("formatEta: null means 'estimating…', never a fabricated countdown", () => {
  assert.equal(formatEta(null), "estimating…");
  assert.equal(formatEta(undefined), "estimating…");
  assert.equal(formatEta(NaN), "estimating…");
  assert.equal(formatEta(-5), "estimating…");
});

test("formatEta: seconds under a minute, whole minutes above", () => {
  assert.equal(formatEta(45_000), "≈ 45s");
  assert.equal(formatEta(90_000), "≈ 2 min");
  assert.equal(formatEta(200_000), "≈ 4 min");
  assert.equal(formatEta(500), "almost done");
});

test("formatAum: null for missing/zero so no '$0' or dash is ever shown", () => {
  assert.equal(formatAum(null), null);
  assert.equal(formatAum(0), null);
  assert.equal(formatAum(undefined), null);
  assert.equal(formatAum(412_000_000), "$412M");
  assert.equal(formatAum(2_030_000_000), "$2.0B");
  assert.equal(formatAum(12_000_000_000), "$12B");
});

test("metaLine: drops missing parts and the type_unconfirmed placeholder", () => {
  assert.equal(metaLine(["MFO", "TX", "$412M"]), "MFO · TX · $412M");
  assert.equal(metaLine(["MFO", null, null]), "MFO");
  assert.equal(metaLine(["type_unconfirmed", "NY", null]), "NY");
  assert.equal(metaLine([null, null, null]), "");
});

test("boardSummary: omits the freshness clause entirely when nothing is dated", () => {
  assert.equal(boardSummary(478, 845, null), "478 organizations · 845 signals");
  assert.match(boardSummary(478, 845, "2026-08-15"), /freshest/);
  assert.equal(boardSummary(1, 1, null), "1 organization · 1 signal");
});

test("every kind has a glyph AND a word — colour never carries meaning alone", () => {
  for (const k of KIND_ORDER) {
    const m = kindMeta(k);
    assert.ok(m.glyph.length > 0, `${k} needs a glyph`);
    assert.ok(m.label.length > 0, `${k} needs a word`);
    assert.match(m.color, /^var\(--/, `${k} must use a design token, not a raw colour`);
  }
});

test("kindMeta falls back to firm_news for an unknown kind rather than crashing", () => {
  assert.equal(kindMeta("something_new").label, "news");
});

// T47.2 acceptance. Run with `npm test` (node --test, zero deps).
//
// These tests exist for one property above all others: submitting a question APPENDS a
// turn and never touches the turns before it. The pre-T47 reducer returned
// `...INITIAL_STATE` on submit, so a second question destroyed the first answer, its
// records and its evidence. Every "two turns" test below would have failed against it.

import { test } from "node:test";
import assert from "node:assert/strict";

import { threadReducer, INITIAL_THREAD, viewCounts, answerText, type ThreadState } from "./thread.ts";
import type { ClaimVerifiedPayload, RecordRow } from "./types.ts";

function rec(id: string, name: string): RecordRow {
  return { record_id: id, entity_name: name, entity_type: "SFO", mandates: [], fit_tags: [] } as unknown as RecordRow;
}

function claim(recordId: string, field: string): ClaimVerifiedPayload {
  return { sentence: "s", recordId, field, value: "v", status: "verified" };
}

/** Submit two questions and stream a distinct result into each. */
function twoTurns(): ThreadState {
  let s = threadReducer(INITIAL_THREAD, { type: "submit", id: "t1", query: "SFOs over $500M", filters: {} });
  s = threadReducer(s, { type: "event", id: "t1", event: { type: "records", records: [rec("r1", "KAPOR")], candidateCount: 12 } });
  s = threadReducer(s, { type: "event", id: "t1", event: { type: "token", text: "First answer.", kind: "claim" } });
  s = threadReducer(s, { type: "event", id: "t1", event: { type: "claim_verified", claim: claim("r1", "aum_usd") } });
  s = threadReducer(s, { type: "event", id: "t1", event: { type: "done", records: [], relaxedFilters: [] } });

  s = threadReducer(s, { type: "submit", id: "t2", query: "MFOs in Texas", filters: {} });
  s = threadReducer(s, { type: "event", id: "t2", event: { type: "records", records: [rec("r2", "QVT"), rec("r3", "BOSTON")], candidateCount: 30 } });
  s = threadReducer(s, { type: "event", id: "t2", event: { type: "token", text: "Second answer.", kind: "claim" } });
  s = threadReducer(s, { type: "event", id: "t2", event: { type: "claim_verified", claim: claim("r2", "hq_state") } });
  return s;
}

test("submit appends a turn and does not disturb the previous one", () => {
  const s = twoTurns();
  assert.equal(s.turns.length, 2);
  assert.equal(s.turns[0].query, "SFOs over $500M");
  assert.equal(s.turns[1].query, "MFOs in Texas");
});

test("the first turn keeps its answer, records and claims after a second question", () => {
  const s = twoTurns();
  const [first] = s.turns;
  assert.equal(answerText(first), "First answer.");
  assert.deepEqual(first.records.map((r) => r.entity_name), ["KAPOR"]);
  assert.equal(first.claims.length, 1);
  assert.equal(first.claims[0].field, "aum_usd");
  assert.equal(first.status, "done");
});

test("two turns hold independent record sets and candidate counts", () => {
  const s = twoTurns();
  assert.equal(s.turns[0].records.length, 1);
  assert.equal(s.turns[1].records.length, 2);
  assert.equal(s.turns[0].candidateCount, 12);
  assert.equal(s.turns[1].candidateCount, 30);
});

test("an event is routed to its own turn and leaves the others alone", () => {
  let s = twoTurns();
  s = threadReducer(s, { type: "event", id: "t2", event: { type: "token", text: "More.", kind: "neutral" } });
  assert.equal(answerText(s.turns[0]), "First answer.");
  assert.equal(answerText(s.turns[1]), "Second answer. More.");
});

test("an event for an unknown turn id is a no-op, not a crash", () => {
  const s = twoTurns();
  const after = threadReducer(s, { type: "event", id: "nope", event: { type: "token", text: "x", kind: "neutral" } });
  assert.deepEqual(after, s);
});

test("claim_verified attaches to the most recent unclaimed claim-kind segment", () => {
  let s = threadReducer(INITIAL_THREAD, { type: "submit", id: "a", query: "q", filters: {} });
  s = threadReducer(s, { type: "event", id: "a", event: { type: "token", text: "One.", kind: "claim" } });
  s = threadReducer(s, { type: "event", id: "a", event: { type: "claim_verified", claim: claim("r1", "f1") } });
  s = threadReducer(s, { type: "event", id: "a", event: { type: "token", text: "Two.", kind: "neutral" } });
  s = threadReducer(s, { type: "event", id: "a", event: { type: "token", text: "Three.", kind: "claim" } });
  s = threadReducer(s, { type: "event", id: "a", event: { type: "claim_verified", claim: claim("r2", "f2") } });

  const segs = s.turns[0].segments;
  assert.equal(segs[0].claim?.field, "f1");
  assert.equal(segs[1].claim, undefined, "a neutral segment never takes a claim");
  assert.equal(segs[2].claim?.field, "f2");
});

test("two turns can sit on different views at the same time (T47.5)", () => {
  let s = twoTurns();
  s = threadReducer(s, { type: "set_view", id: "t1", view: "ranked" });
  s = threadReducer(s, { type: "set_view", id: "t2", view: "evidence" });
  assert.equal(s.turns[0].view, "ranked");
  assert.equal(s.turns[1].view, "evidence");
});

test("rerun resets one turn in place, keeping its id, position and question", () => {
  let s = twoTurns();
  s = threadReducer(s, { type: "rerun", id: "t1", filters: { hq_state: "TX" } as never });

  assert.equal(s.turns.length, 2, "a rerun must not append a near-duplicate turn");
  assert.equal(s.turns[0].id, "t1");
  assert.equal(s.turns[0].query, "SFOs over $500M", "the question survives the rerun");
  assert.equal(s.turns[0].records.length, 0, "the stale result is cleared");
  assert.equal(s.turns[0].status, "streaming");
  assert.equal(s.turns[1].query, "MFOs in Texas", "the other turn is untouched");
  assert.equal(s.turns[1].records.length, 2);
});

test("stopping keeps the partial answer rather than discarding it (T47.3)", () => {
  let s = twoTurns();
  assert.equal(s.turns[1].status, "streaming");
  s = threadReducer(s, { type: "stopped", id: "t2" });

  assert.equal(s.turns[1].status, "stopped");
  assert.equal(answerText(s.turns[1]), "Second answer.", "streamed text survives the abort");
  assert.equal(s.turns[1].claims.length, 1, "verified claims survive the abort");
  assert.equal(s.turns[1].recordsLoading, false, "no skeleton is left spinning");
});

test("a late abort never demotes a turn that already finished", () => {
  let s = twoTurns();
  assert.equal(s.turns[0].status, "done");
  s = threadReducer(s, { type: "stopped", id: "t1" });
  assert.equal(s.turns[0].status, "done");
});

test("done with error sets the error status, not done", () => {
  let s = threadReducer(INITIAL_THREAD, { type: "submit", id: "e", query: "q", filters: {} });
  s = threadReducer(s, { type: "event", id: "e", event: { type: "done", records: [], relaxedFilters: [], error: true, finalAnswerFallback: "Couldn't complete that search." } });
  assert.equal(s.turns[0].status, "error");
  assert.equal(s.turns[0].fallbackMessage, "Couldn't complete that search.");
});

test("toggle_stages affects only its own turn", () => {
  let s = twoTurns();
  // `done` collapses t1's strip; t2 is still streaming so its strip is open.
  assert.equal(s.turns[0].stagesCollapsed, true);
  assert.equal(s.turns[1].stagesCollapsed, false);

  s = threadReducer(s, { type: "toggle_stages", id: "t1" });
  assert.equal(s.turns[0].stagesCollapsed, false, "the toggled strip flipped");
  assert.equal(s.turns[1].stagesCollapsed, false, "the other turn's strip did not move");
});

test("viewCounts reports what each view actually holds", () => {
  const s = twoTurns();
  const c = viewCounts(s.turns[1]);
  assert.equal(c.records, 2);
  assert.equal(c.evidence, 1);
  assert.equal(c.ranked, 0, "a view with nothing in it counts 0 and is not offered");
  assert.equal(c.excluded, 0);
});

test("clear empties the thread", () => {
  const s = threadReducer(twoTurns(), { type: "clear" });
  assert.deepEqual(s, INITIAL_THREAD);
});

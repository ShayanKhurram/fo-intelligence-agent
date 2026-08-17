// T49.3 — the LLM relevance judge. The behaviour that matters most here is not "does it
// filter" but "does it ever filter by ACCIDENT": a judge that empties the shortlist on a
// network blip or a malformed reply would turn a working answer into "no matches" and be
// indistinguishable from a genuine corpus gap. Every failure path must fail OPEN.

import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

import { judgeRelevance, parseKeepList } from "./relevance-judge.ts";
import type { RankedCandidate } from "./plan-rank.ts";

function row(id: string, name: string, evidence: string): RankedCandidate {
  return {
    record_id: id,
    entity_name: name,
    score: 0.5,
    scores: { fit: 0.5, reach: 0.5, recency: 0.5, trust: 0.5 },
    why: [],
    gaps: [],
    evidenceChunk: { facet: "mandate", content: evidence },
  };
}

const ROWS = [
  row("r1", "Climate FO", "Backs decarbonization and clean energy."),
  row("r2", "Generic FO", "Geography focus: Subscribers only."),
  row("r3", "Other FO", "FinTech, Hardware, Non Profit."),
];

let origFetch: typeof globalThis.fetch;
let origKey: string | undefined;

beforeEach(() => {
  origFetch = globalThis.fetch;
  origKey = process.env.OLLAMA_API_KEY;
  process.env.OLLAMA_API_KEY = "test-key";
});

afterEach(() => {
  globalThis.fetch = origFetch;
  if (origKey === undefined) delete process.env.OLLAMA_API_KEY;
  else process.env.OLLAMA_API_KEY = origKey;
});

function reply(content: string): Response {
  return {
    ok: true,
    status: 200,
    json: async () => ({ choices: [{ message: { role: "assistant", content } }] }),
    text: async () => content,
  } as unknown as Response;
}

// --- parseKeepList ------------------------------------------------------------------------

test("parseKeepList reads a plain JSON array", () => {
  assert.deepEqual(parseKeepList("[1,3]", 3), [1, 3]);
});

test("parseKeepList tolerates code fences and surrounding prose", () => {
  assert.deepEqual(parseKeepList("Sure!\n```json\n[2]\n```", 3), [2]);
});

test("parseKeepList reads an explicit empty array as a real 'none qualify' answer", () => {
  assert.deepEqual(parseKeepList("[]", 3), []);
});

test("parseKeepList returns null for unparseable prose — NOT an empty keep list", () => {
  // The distinction this whole module rests on. "I could not determine relevance" must fail
  // open (null -> keep everything), never collapse to [] (drop everything).
  assert.equal(parseKeepList("I could not determine relevance.", 3), null);
  assert.equal(parseKeepList("", 3), null);
});

test("parseKeepList rejects out-of-range and non-integer indices", () => {
  assert.equal(parseKeepList("[0]", 3), null, "1-based: 0 is out of range");
  assert.equal(parseKeepList("[4]", 3), null, "beyond the offered rows");
  assert.equal(parseKeepList("[1.5]", 3), null);
  assert.equal(parseKeepList("[abc]", 3), null);
});

test("parseKeepList de-duplicates repeated indices", () => {
  assert.deepEqual(parseKeepList("[2,2,1]", 3), [2, 1]);
});

// --- judgeRelevance -----------------------------------------------------------------------

test("judgeRelevance keeps only the rows the model names", async () => {
  globalThis.fetch = (async () => reply("[1]")) as typeof globalThis.fetch;
  const res = await judgeRelevance("Which family offices invest in climate?", ROWS);
  assert.equal(res.applied, true);
  assert.deepEqual([...res.keep], ["r1"]);
  assert.deepEqual(res.dropped.map((d) => d.record_id), ["r2", "r3"]);
});

test("judgeRelevance fails OPEN when the request throws — no row is dropped", async () => {
  globalThis.fetch = (async () => {
    throw new Error("network down");
  }) as typeof globalThis.fetch;
  const res = await judgeRelevance("anything", ROWS);
  assert.equal(res.applied, false, "a failed judge must not claim to have been applied");
  assert.equal(res.keep.size, 3, "every row survives a judge failure");
  assert.equal(res.dropped.length, 0);
});

test("judgeRelevance fails OPEN on an unparseable reply", async () => {
  globalThis.fetch = (async () => reply("I'm not sure, they all look relevant to me.")) as typeof globalThis.fetch;
  const res = await judgeRelevance("anything", ROWS);
  assert.equal(res.applied, false);
  assert.equal(res.keep.size, 3);
});

test("judgeRelevance fails OPEN when the model names rows it was never offered", async () => {
  // A hallucinated index means the reply cannot be trusted as a whole — keep everything
  // rather than partially applying a reply we know is wrong.
  globalThis.fetch = (async () => reply("[1,9]")) as typeof globalThis.fetch;
  const res = await judgeRelevance("anything", ROWS);
  assert.equal(res.applied, false);
  assert.equal(res.keep.size, 3);
});

test("judgeRelevance honours an explicit empty verdict — nothing relevant", async () => {
  // The one case where dropping everything IS correct: the model read the rows and said none
  // qualify. The route then declines through its existing empty-shortlist path.
  globalThis.fetch = (async () => reply("[]")) as typeof globalThis.fetch;
  const res = await judgeRelevance("what is the capital of France", ROWS);
  assert.equal(res.applied, true);
  assert.equal(res.keep.size, 0);
  assert.equal(res.dropped.length, 3);
});

test("judgeRelevance skips the model call entirely for 0 or 1 rows", async () => {
  let called = false;
  globalThis.fetch = (async () => {
    called = true;
    return reply("[]");
  }) as typeof globalThis.fetch;

  const one = await judgeRelevance("q", [ROWS[0]]);
  assert.equal(called, false, "a single row is not worth a model call");
  assert.equal(one.keep.size, 1);

  const none = await judgeRelevance("q", []);
  assert.equal(called, false);
  assert.equal(none.keep.size, 0);
});

test("judgeRelevance sends the cheapest tier, not the generation tier", async () => {
  // Latency guard: this call sits inside a route capped at maxDuration = 60 that already
  // spends ~25-30s on generation. A reasoning model here would add its whole CoT to that.
  let sentModel: string | undefined;
  globalThis.fetch = (async (_url: string | URL, init?: RequestInit) => {
    sentModel = JSON.parse(String(init?.body)).model;
    return reply("[1]");
  }) as typeof globalThis.fetch;
  await judgeRelevance("q", ROWS);
  assert.equal(sentModel, "gemma4:31b");
});

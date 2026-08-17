// T49.2 — topic residual extraction + expansion parsing. Both are pure; the network path is
// covered by the fail-open cases.

import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

import { parseTerms, expandTopic } from "./topic-expansion.ts";
import { topicalResidual } from "./query-understanding.ts";

// --- topicalResidual ----------------------------------------------------------------------

test("topicalResidual strips question and domain scaffolding, leaving the topic", () => {
  assert.equal(topicalResidual("Which family offices invest in climate?", {}), "climate");
  assert.equal(topicalResidual("Which family offices invest in biotechnology?", {}), "biotechnology");
});

test("topicalResidual keeps an entity name — this is what makes name lookups work", () => {
  // "Who is Kapor Family Office?" -> "kapor". If the name were stripped, exact-name retrieval
  // (the most obviously correct behaviour this product has) would break.
  assert.equal(topicalResidual("Who is Kapor Family Office?", {}), "kapor");
  assert.equal(topicalResidual("tell me about QVT Family Office", {}), "qvt");
});

test("topicalResidual returns EMPTY for a purely structural question", () => {
  // The signal that there is no topic to be relevant to. Callers must fall back to the full
  // question rather than rank against an empty string.
  assert.equal(topicalResidual("Which family offices are based in Texas?", { hq_state: "TX" }), "");
  assert.equal(topicalResidual("multi family offices", { entity_type: "MFO" }), "");
});

test("topicalResidual drops the state the filter already captured, by name or code", () => {
  assert.equal(topicalResidual("family offices in New York", { hq_state: "NY" }), "");
  assert.equal(topicalResidual("family offices in TX", { hq_state: "TX" }), "");
});

test("topicalResidual drops money amounts belonging to the AUM filter", () => {
  assert.equal(topicalResidual("family offices with over $500M", { aum_min: 5e8 }), "");
});

test("topicalResidual keeps a multi-word topic intact", () => {
  assert.equal(topicalResidual("Which family offices invest in real estate?", {}), "real estate");
});

// --- parseTerms ---------------------------------------------------------------------------

test("parseTerms reads a comma list and always includes the original topic first", () => {
  const terms = parseTerms("clean energy, decarbonization, renewable", "climate");
  assert.equal(terms[0], "climate");
  assert.ok(terms.includes("clean energy"));
  assert.ok(terms.includes("decarbonization"));
});

test("parseTerms de-duplicates the topic when the model repeats it", () => {
  const terms = parseTerms("climate, climate, renewable", "climate");
  assert.deepEqual(terms, ["climate", "renewable"]);
});

test("parseTerms rejects sentence fragments, keeping only short terms", () => {
  const terms = parseTerms(
    "climate, I think the most relevant here would be anything to do with power, solar",
    "climate",
  );
  assert.ok(terms.includes("climate"));
  assert.ok(terms.includes("solar"));
  assert.ok(!terms.some((t) => t.includes("think")), "a prose fragment must not become a search term");
});

test("parseTerms falls back to the bare topic when the reply is unusable", () => {
  assert.deepEqual(parseTerms("", "climate"), ["climate"]);
  assert.deepEqual(parseTerms("I'm not sure what you mean.", "climate"), ["climate"]);
});

// --- expandTopic --------------------------------------------------------------------------

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

test("expandTopic fails OPEN to the bare topic when the call throws", async () => {
  globalThis.fetch = (async () => {
    throw new Error("offline");
  }) as typeof globalThis.fetch;
  assert.deepEqual(await expandTopic("climate"), ["climate"]);
});

test("expandTopic does NOT expand a name lookup", async () => {
  // Synonyms of a company name are other companies. A name must be searched literally.
  let called = false;
  globalThis.fetch = (async () => {
    called = true;
    return {} as Response;
  }) as typeof globalThis.fetch;
  assert.deepEqual(await expandTopic("kapor family office holdings"), ["kapor family office holdings"]);
  assert.equal(called, false, "a multi-word thesis must not trigger a model call");
});

test("expandTopic returns nothing for an empty topic — the structural path", async () => {
  assert.deepEqual(await expandTopic(""), []);
});

// T44.0 — capture / compare `/api/query` behavior across a retrieval rewrite.
//
// `/api/query` is the deployed product. T44 replaces its retrieval core, and the one
// regression that would matter most is silent: losing exact-name lookups, which today
// work through `lexicalRank`'s tsvector match and have no equivalent in a purely
// semantic sweep. You cannot diff against a baseline you did not record, so this runs
// first, before anything is changed.
//
//   node scripts/query_baseline.mjs --out baseline-before.json
//   node scripts/query_baseline.mjs --out baseline-after.json --compare baseline-before.json
//
// The comparison that decides pass/fail is `citedLost` — a record the old answer cited
// and the new one does not. Everything else is informational.

import { writeFileSync, readFileSync } from "node:fs";

const BASE = process.env.BASE_URL ?? "http://localhost:3111";

// Name lookups (the lexical path), sector asks, aggregates, a known-nothing query, and
// all 14 phrasings from the 2026-08-17 recall measurement — the ones that must stop
// being second-class once T44.1 lands.
const QUERIES = [
  "who is Kapor Family Office",
  "tell me about QVT Family Office",
  "Boston Family Office",
  "family offices in California",
  "family offices interested in real estate",
  "multi family offices with over $1 billion AUM",
  "how many SFOs are in Texas",
  "how many family offices are in the dataset",
  "what is the capital of France",
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

function arg(name) {
  const i = process.argv.indexOf(name);
  return i === -1 ? null : process.argv[i + 1];
}

// A broken server answers every query with nothing, and a comparison run against it
// records 23 empty results as if they were data — which is exactly how this harness
// once reported a confident 104-record "regression" that was really a wedged dev
// server (a `next build` had clobbered the `.next` dev artifacts). An unreliable
// harness is worse than no harness, so: assert health before capturing, and treat a
// non-200 on any query as a hard stop rather than an empty result.
async function assertHealthy() {
  const res = await fetch(`${BASE}/api/health`);
  if (!res.ok) throw new Error(`/api/health returned ${res.status} — server is not serving. Refusing to capture.`);
  const h = await res.json();
  if (!h.record_count) throw new Error(`/api/health reports record_count=${h.record_count} — empty corpus. Refusing to capture.`);
  console.log(`health: build=${h.build_hash} records=${h.record_count}\n`);
  return h;
}

async function runQuery(query) {
  const res = await fetch(`${BASE}/api/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${JSON.stringify(query)} — aborting rather than recording an empty result`);
  const text = await res.text();
  const events = [];
  for (const line of text.split("\n")) {
    const t = line.trim();
    if (!t.startsWith("data:")) continue;
    try { events.push(JSON.parse(t.slice(5).trim())); } catch { /* partial frame */ }
  }
  const done = events.find((e) => e.type === "done") ?? {};
  const tokens = events.filter((e) => e.type === "token");
  return {
    query,
    // The record ids the answer actually rests on — `done.records` when the route
    // reports them, else whatever the claim pills cited.
    cited: Array.isArray(done.records)
      ? [...done.records].sort()
      : [...new Set(events.filter((e) => e.type === "claim_verified").map((e) => e.claim.recordId))].sort(),
    answer: tokens.map((t) => t.text).join(" ").trim() || done.finalAnswerFallback || "",
    tokenKinds: tokens.reduce((acc, t) => ({ ...acc, [t.kind]: (acc[t.kind] ?? 0) + 1 }), {}),
    count: done.count ?? null,
    declined: !!done.declined,
    discarded: !!done.discarded,
    relaxedFilters: done.relaxedFilters ?? [],
    strippedFraction: done.strippedFraction ?? null,
  };
}

function compare(before, after) {
  const byQuery = new Map(before.results.map((r) => [r.query, r]));
  let lostTotal = 0;
  console.log("\n=== comparison (old -> new) ===\n");
  // The corpus is written by a separate pipeline and can move between captures. A
  // record that was cited before and is simply no longer IN the corpus is not a
  // retrieval regression, and conflating the two would blame the wrong change.
  if (before.buildHash !== after.buildHash) {
    console.log(`NOTE corpus changed between captures: ${before.buildHash ?? "?"} (${before.recordCount ?? "?"} records)`);
    console.log(`                                  ->  ${after.buildHash ?? "?"} (${after.recordCount ?? "?"} records)`);
    console.log(`     'lost' below may include records the corpus no longer holds — check before blaming retrieval.\n`);
  }
  for (const now of after.results) {
    const was = byQuery.get(now.query);
    if (!was) continue;
    const lost = was.cited.filter((id) => !now.cited.includes(id));
    const gained = now.cited.filter((id) => !was.cited.includes(id));
    lostTotal += lost.length;
    const flags = [];
    if (lost.length) flags.push(`LOST ${lost.length}`);
    if (gained.length) flags.push(`+${gained.length}`);
    if (was.declined !== now.declined) flags.push(`declined ${was.declined}->${now.declined}`);
    const mark = lost.length ? "!!" : flags.length ? " ~" : " =";
    console.log(`${mark} [${was.cited.length}->${now.cited.length}] ${flags.join(" ")}  ${now.query.slice(0, 62)}`);
    for (const id of lost) console.log(`      lost: ${id}`);
  }
  console.log(`\ncited records lost across all queries: ${lostTotal}`);
  console.log(lostTotal === 0 ? "PASS — no query lost a record it used to cite." : "FAIL — see LOST lines above.");
}

const out = arg("--out") ?? "baseline.json";
const health = await assertHealthy();
const results = [];
for (const q of QUERIES) {
  process.stdout.write(`. ${q.slice(0, 58)}\n`);
  // Deliberately NOT caught: a failed request means the run is invalid, not that the
  // query returned nothing. Aborting loudly beats a plausible-looking bad dataset.
  results.push(await runQuery(q));
}
const payload = {
  capturedAt: new Date().toISOString(),
  base: BASE,
  buildHash: health.build_hash,
  recordCount: health.record_count,
  results,
};
writeFileSync(out, JSON.stringify(payload, null, 2));
console.log(`\nwrote ${out} (${results.length} queries)`);

const cmp = arg("--compare");
if (cmp) compare(JSON.parse(readFileSync(cmp, "utf8")), payload);

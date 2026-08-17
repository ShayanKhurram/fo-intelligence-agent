// T49 — candidates.ts acceptance. The relevance gate (partitionByRelevance) is a pure
// function extracted from selectCandidates so it can be tested without a DB or network.
// Run with `npm test` (node --test, zero deps).

import { test } from "node:test";
import assert from "node:assert/strict";

import { partitionByRelevance, MAX_EVIDENCE_DISTANCE } from "./candidates.ts";
import type { PlanCandidate } from "./plan-rank.ts";

// Minimal candidate factory: fills every required RecordRow field with a neutral default
// so tests only name the fields they care about. Mirrors the `base` helper in
// plan-rank.test.ts so the two suites stay in sync on the RecordRow shape.
function base(overrides: Partial<PlanCandidate> & { record_id: string }): PlanCandidate {
  return {
    record_id: overrides.record_id,
    entity_name: overrides.entity_name ?? "Acme FO",
    entity_type: overrides.entity_type ?? "SFO",
    hq_state: overrides.hq_state ?? "CA",
    hq_country: overrides.hq_country ?? "US",
    aum_usd: overrides.aum_usd ?? null,
    aum_basis: overrides.aum_basis ?? null,
    aum_as_of: overrides.aum_as_of ?? null,
    mandates: overrides.mandates ?? [],
    fit_tags: overrides.fit_tags ?? [],
    check_size_min: overrides.check_size_min ?? null,
    check_size_max: overrides.check_size_max ?? null,
    principal_name: overrides.principal_name ?? null,
    principal_title: overrides.principal_title ?? null,
    principal_email: overrides.principal_email ?? null,
    principal_email_status: overrides.principal_email_status ?? "could_not_verify",
    principal_phone: overrides.principal_phone ?? null,
    principal_phone_status: overrides.principal_phone_status ?? "could_not_verify",
    most_recent_signal_date: overrides.most_recent_signal_date ?? null,
    urgency_tier: overrides.urgency_tier ?? null,
    record_confidence: overrides.record_confidence ?? "thin",
    outcome: overrides.outcome ?? "ship_with_caveats",
    outreach_hook: overrides.outreach_hook ?? null,
    evidenceDistance: overrides.evidenceDistance ?? null,
    evidenceChunk: overrides.evidenceChunk ?? null,
    fitRank: overrides.fitRank ?? 0,
    principal_linkedin: overrides.principal_linkedin ?? null,
  };
}

test("a zero-fitRank record is gated even with maximal reach and trust — relevance beats contactability", () => {
  // Exactly the offender the brief names: no semantic or lexical hit, but a confirmed
  // email and verified evidence. Before the gate this rode reach+trust into the top_n.
  const c = base({
    record_id: "contactable_only",
    entity_name: "Contactable Only FO",
    principal_email: "principal@fo.com",
    principal_email_status: "confirmed",
    record_confidence: "verified_evidence_present",
    fitRank: 0,
    evidenceChunk: { facet: "mandate", content: "Some real mandate text." },
  });
  const { kept, gated } = partitionByRelevance([c]);
  assert.equal(kept.length, 0, "a zero-fitRank record must not survive the gate");
  assert.equal(gated.length, 1);
  assert.equal(gated[0].record_id, "contactable_only");
  assert.ok(gated[0].reason.length > 0);
  assert.ok(/no semantic or keyword match/i.test(gated[0].reason));
});

test("a non-statement evidence chunk is gated even when fitRank is high", () => {
  // fitRank says this record matched; its best evidence chunk says it has nothing to say.
  // The non-statement is evidence of absence — gate it on the content path, not the rank.
  const c = base({
    record_id: "no_thesis",
    entity_name: "No Thesis FO",
    fitRank: 0.9,
    evidenceChunk: {
      facet: "mandate",
      content:
        "No investing thesis or mandate details have been confirmed for this record.",
    },
  });
  const { kept, gated } = partitionByRelevance([c]);
  assert.equal(kept.length, 0, "a non-statement best evidence must not survive the gate");
  assert.equal(gated.length, 1);
  assert.equal(gated[0].record_id, "no_thesis");
  assert.ok(/no investing thesis on file/i.test(gated[0].reason));
});

test("a real fitRank > 0 with a real evidence chunk is KEPT even when reach and trust are 0", () => {
  // "Grade, never gate" still holds for reach/recency/trust. A record with no contact
  // path, no signal date, and thin confidence — but a genuine semantic/lexical hit and a
  // real evidence chunk — must survive. The gate is on relevance only.
  const c = base({
    record_id: "fits_but_bare",
    entity_name: "Fits But Bare FO",
    principal_name: null,
    principal_email: null,
    principal_phone: null,
    most_recent_signal_date: null,
    record_confidence: "thin",
    fitRank: 0.6,
    evidenceChunk: { facet: "mandate", content: "Backs early-stage climate hardware." },
  });
  const { kept, gated } = partitionByRelevance([c]);
  assert.equal(gated.length, 0, "a relevant record must not be gated for weak reach/trust");
  assert.equal(kept.length, 1);
  assert.equal(kept[0].record_id, "fits_but_bare");
});

test("3 relevant + 7 zero-signal candidates gate down to exactly the 3 relevant ones", () => {
  // The top_n quota-vs-cap defect: with top_n: 10 the old code returned 10 (padding with
  // whatever ranked next). The gate drops the 7 zero-signal rows first; the caller's
  // .slice(0, top_n) then caps at 3, not pads to 10.
  const relevant = ["r1", "r2", "r3"].map((id, i) =>
    base({
      record_id: id,
      fitRank: 0.5 + i * 0.1,
      evidenceChunk: { facet: "mandate", content: "Climate decarbonization mandate." },
    }),
  );
  const zeroSignal = Array.from({ length: 7 }, (_, i) =>
    base({
      record_id: `z${i + 1}`,
      // Maximal reach/trust to prove they would have padded the list before the gate.
      principal_email: `z${i + 1}@fo.com`,
      principal_email_status: "confirmed",
      record_confidence: "verified_evidence_present",
      fitRank: 0,
      evidenceChunk: { facet: "mandate", content: "Whatever ingest text." },
    }),
  );
  const { kept, gated } = partitionByRelevance([...relevant, ...zeroSignal]);
  assert.equal(kept.length, 3, "only the 3 relevant records survive");
  assert.equal(gated.length, 7, "all 7 zero-signal records are gated");
  assert.deepEqual(
    kept.map((c) => c.record_id).sort(),
    ["r1", "r2", "r3"],
  );
});

test("every gated record appears in `gated` with a non-empty, distinct reason", () => {
  // One zero-fitRank record and one non-statement record: each gets its own human-readable
  // reason, neither is silently dropped.
  const zeroRank = base({
    record_id: "zero_rank",
    fitRank: 0,
    evidenceChunk: { facet: "mandate", content: "Some text." },
  });
  const nonStatement = base({
    record_id: "non_statement",
    fitRank: 0.8,
    evidenceChunk: {
      facet: "mandate",
      content: "No recent activity signal has been confirmed for this record.",
    },
  });
  const { gated } = partitionByRelevance([zeroRank, nonStatement]);
  assert.equal(gated.length, 2);
  for (const g of gated) {
    assert.ok(g.record_id, "gated entry has a record_id");
    assert.ok(g.entity_name, "gated entry has an entity_name");
    assert.ok(g.reason && g.reason.length > 0, `${g.record_id} has an empty reason`);
  }
  const reasons = gated.map((g) => g.reason);
  assert.equal(new Set(reasons).size, 2, "the two conditions produce distinct reasons");
});

// --- T49.1: the absolute distance ceiling ------------------------------------------------
// T49's two conditions left thesis queries broken — "invest in climate" returned 10 rows,
// none about climate, because every candidate holding a real chunk earns SOME fused rank.
// These pin the measured ceiling that fixes it (see MAX_EVIDENCE_DISTANCE's calibration note).

test("T49.1: evidence beyond MAX_EVIDENCE_DISTANCE is gated even with a high fitRank and real chunk", () => {
  // A real mandate chunk and a strong *positional* rank — it was the best of a bad set — but
  // an absolute distance showing the corpus holds nothing close. 0.765 is the measured
  // best-in-corpus for "what is the capital of France", a question this data cannot answer.
  const c = base({
    record_id: "far_but_top_ranked",
    entity_name: "Best Of A Bad Set FO",
    fitRank: 1.0,
    evidenceDistance: 0.765,
    evidenceChunk: { facet: "mandate", content: "Geography focus: global." },
  });
  const { kept, gated } = partitionByRelevance([c]);
  assert.equal(kept.length, 0, "a far record must not survive on positional rank alone");
  assert.equal(gated.length, 1);
  assert.ok(/too far from the question/i.test(gated[0].reason));
  assert.ok(gated[0].reason.includes("0.77"), "the reason names the measured distance");
});

test("T49.1: evidence inside MAX_EVIDENCE_DISTANCE is kept", () => {
  // The "real estate" case: 0.499 was the measured best for an answerable thesis.
  const c = base({
    record_id: "near",
    fitRank: 0.4,
    evidenceDistance: 0.262, // measured best for "invest in real estate?" — clearly answerable
    evidenceChunk: { facet: "mandate", content: "Entrepreneurial real estate investing." },
  });
  const { kept, gated } = partitionByRelevance([c]);
  assert.equal(gated.length, 0);
  assert.equal(kept.length, 1);
});

test("T49.1: the ceiling is exclusive — a record exactly at the boundary is kept", () => {
  const c = base({
    record_id: "boundary",
    fitRank: 0.4,
    evidenceDistance: MAX_EVIDENCE_DISTANCE,
    evidenceChunk: { facet: "mandate", content: "Real mandate text." },
  });
  assert.equal(partitionByRelevance([c]).kept.length, 1);
});

test("T49.1: a null evidenceDistance with fitRank > 0 is KEPT — the exact-name lookup path", () => {
  // The regression canary in unit form. A name lookup ("Who is Kapor Family Office?") can
  // match lexically with no embedding row at all, so evidenceDistance is null. Treating null
  // as "far away" would break exact-name retrieval, which candidates.ts's header calls the
  // most obviously correct behavior this product has.
  const c = base({
    record_id: "name_lookup",
    entity_name: "Kapor Family Office",
    fitRank: 0.5,
    evidenceDistance: null,
    evidenceChunk: null,
  });
  const { kept, gated } = partitionByRelevance([c]);
  assert.equal(gated.length, 0, "a lexical-only match must not be gated for a missing distance");
  assert.equal(kept.length, 1);
});

test("T49.1: when every candidate is too far, nothing is kept — the route then declines", () => {
  // "what is the capital of France" against this corpus: 241 real-statement records, none
  // closer than 0.765. Returning zero rows is the honest answer; the caller's existing
  // empty-shortlist path turns it into a decline instead of 10 confidently-wrong rows.
  const far = Array.from({ length: 5 }, (_, i) =>
    base({
      record_id: `far${i + 1}`,
      fitRank: 1 - i * 0.1,
      evidenceDistance: 0.765 + i * 0.01,
      evidenceChunk: { facet: "activity", content: "Recent investments: 13F value $322,470,867." },
    }),
  );
  const { kept, gated } = partitionByRelevance(far);
  assert.equal(kept.length, 0, "an unanswerable query must yield no rows, not the best of a bad set");
  assert.equal(gated.length, 5);
  for (const g of gated) assert.ok(/too far from the question/i.test(g.reason));
});
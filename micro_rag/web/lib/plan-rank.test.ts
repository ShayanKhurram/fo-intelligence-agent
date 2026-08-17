// T42.2 — plan-rank.ts acceptance. Run with `npm test` (node --test, zero deps).

import { test } from "node:test";
import assert from "node:assert/strict";

import { rankCandidates, WEIGHTS, effectiveWeights, type PlanCandidate } from "./plan-rank.ts";
import type { PlanSpec } from "./plan-spec.ts";

const AS_OF = "2026-08-16";

const SPEC: PlanSpec = {
  thesis: "US industrial decarbonization",
  top_n: 10,
  asOf: AS_OF,
  mandates_any: ["industrial", "decarbonization"],
};

// A minimal candidate factory: fills every required RecordRow field with a neutral
// default so tests only name the fields they care about. `record_id` is taken from the
// caller so determinism is observable.
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
    principal_linkedin: overrides.principal_linkedin ?? null,
  };
}

test("determinism: identical scores come back in stable record_id order, and shuffling input gives the same order", () => {
  // Two candidates built to tie on every score: same null evidence, no contact, null
  // signal, same confidence/outcome. Only record_id differs.
  const a = base({ record_id: "rec_zeta", entity_name: "Zeta FO" });
  const b = base({ record_id: "rec_alpha", entity_name: "Alpha FO" });

  const order1 = rankCandidates([a, b], SPEC).map((r) => r.record_id);
  const order2 = rankCandidates([b, a], SPEC).map((r) => r.record_id);

  // record_id ascending on a tie.
  assert.deepEqual(order1, ["rec_alpha", "rec_zeta"]);
  assert.deepEqual(order2, ["rec_alpha", "rec_zeta"]);
});

test("a single_source email outranks an otherwise-identical phone-only record", () => {
  const email = base({
    record_id: "email_rec",
    principal_email: "jane@acme.com",
    principal_email_status: "single_source",
  });
  const phone = base({
    record_id: "phone_rec",
    principal_phone: "+1-415-555-0100",
    principal_phone_status: "single_source",
  });
  const ranked = rankCandidates([phone, email], SPEC);
  assert.equal(ranked[0].record_id, "email_rec");
  assert.equal(ranked[1].record_id, "phone_rec");
  assert.ok(ranked[0].score > ranked[1].score);
});

test("a stale signal date outranks an otherwise-identical record with null", () => {
  const stale = base({ record_id: "stale_rec", most_recent_signal_date: "2024-01-01" });
  const none = base({ record_id: "none_rec", most_recent_signal_date: null });
  const ranked = rankCandidates([none, stale], SPEC);
  assert.equal(ranked[0].record_id, "stale_rec");
  assert.ok(ranked[0].score > ranked[1].score);
  // The null record names its gap.
  assert.ok(ranked[1].gaps.includes("no recent signal date"));
});

test("a record with no contact of any kind is still returned, scored, with its gap named", () => {
  const bare = base({ record_id: "bare_rec", principal_name: null });
  const ranked = rankCandidates([bare], SPEC);
  assert.equal(ranked.length, 1);
  assert.equal(ranked[0].record_id, "bare_rec");
  assert.ok(ranked[0].gaps.some((g) => g.includes("no named principal")));
  assert.ok(ranked[0].gaps.includes("no email"));
});

test("every returned candidate has a non-empty why", () => {
  const candidates = [
    base({ record_id: "r1", principal_email: "a@x.com", principal_email_status: "single_source" }),
    base({ record_id: "r2", most_recent_signal_date: "2026-07-01" }),
    base({ record_id: "r3", record_confidence: "verified_evidence_present" }),
    base({ record_id: "r4" }), // nothing at all
  ];
  const ranked = rankCandidates(candidates, SPEC);
  for (const r of ranked) {
    assert.ok(r.why.length > 0, `${r.record_id} has empty why`);
  }
});

test("scores stay within [0,1] with no NaN/throw when every optional field is null", () => {
  const empty = base({ record_id: "empty_rec" });
  const ranked = rankCandidates([empty], SPEC);
  assert.equal(ranked.length, 1);
  const r = ranked[0];
  for (const [k, v] of Object.entries(r.scores)) {
    assert.ok(!Number.isNaN(v), `${k} is NaN`);
    assert.ok(v >= 0 && v <= 1, `${k}=${v} out of [0,1]`);
  }
  assert.ok(r.score >= 0 && r.score <= 1 && !Number.isNaN(r.score));
});

test("a pattern_inferred email ranks below single_source and above phone-only, and its why names it inferred", () => {
  // T42.2b — a guessed `first.last@domain` must not rank level with a sourced address.
  const single = base({
    record_id: "single_rec",
    principal_email: "jane@acme.com",
    principal_email_status: "single_source",
  });
  const inferred = base({
    record_id: "inferred_rec",
    principal_email: "first.last@acme.com",
    principal_email_status: "pattern_inferred",
  });
  const phone = base({
    record_id: "phone_rec",
    principal_phone: "+1-415-555-0100",
    principal_phone_status: "single_source",
  });
  const ranked = rankCandidates([phone, inferred, single], SPEC);
  const byId = new Map(ranked.map((r) => [r.record_id, r]));
  const s = byId.get("single_rec")!;
  const i = byId.get("inferred_rec")!;
  const p = byId.get("phone_rec")!;

  // Strict ordering on both the reach sub-score and the composite.
  assert.ok(i.scores.reach < s.scores.reach, "inferred email reach below single_source");
  assert.ok(i.scores.reach > p.scores.reach, "inferred email reach above phone-only");
  assert.ok(i.score < s.score, "inferred email composite below single_source");
  assert.ok(i.score > p.score, "inferred email composite above phone-only");

  // Determinism: same input in the canonical ladder order.
  assert.deepEqual(
    ranked.map((r) => r.record_id),
    ["single_rec", "inferred_rec", "phone_rec"],
  );

  // The why line must say the address is inferred, not sourced, so a user can tell the
  // two apart without opening the drawer.
  assert.ok(
    i.why.some((w) => /inferred/i.test(w) && /not sourced/i.test(w)),
    `inferred why does not name it inferred/not-sourced: ${JSON.stringify(i.why)}`,
  );
  // And the sourced email's why must NOT carry the inferred caveat.
  assert.ok(
    !s.why.some((w) => /inferred/i.test(w)),
    `single_source why wrongly marked inferred: ${JSON.stringify(s.why)}`,
  );
});

test("WEIGHTS is exported and the composite equals the weighted sum of the sub-scores", () => {
  assert.deepEqual(WEIGHTS, { fit: 0.35, reach: 0.30, recency: 0.20, trust: 0.15 });
  const c = base({
    record_id: "rec",
    principal_email: "jane@acme.com",
    principal_email_status: "single_source",
    most_recent_signal_date: "2026-06-01",
    record_confidence: "single_source_only",
    outcome: "ship_with_caveats",
    evidenceDistance: 0.4,
    evidenceChunk: { facet: "mandate", content: "industrial decarbonization thesis" },
    fit_tags: ["industrial"],
  });
  const [r] = rankCandidates([c], SPEC);
  const expected =
    WEIGHTS.fit * r.scores.fit +
    WEIGHTS.reach * r.scores.reach +
    WEIGHTS.recency * r.scores.recency +
    WEIGHTS.trust * r.scores.trust;
  assert.equal(r.score, expected);
});
// T44.5 — the two defects behind "it gives the same answer for every query".

test("a boilerplate evidence chunk scores fit 0 and names the gap, instead of mid-range similarity", () => {
  const boiler = base({
    record_id: "no_thesis",
    evidenceDistance: 0.45,
    evidenceChunk: {
      facet: "mandate",
      content: "CHS FAMILY OFFICE's investing mandate. No investing thesis or mandate details have been confirmed for this record.",
    },
  });
  const real = base({
    record_id: "has_thesis",
    evidenceDistance: 0.45,
    evidenceChunk: { facet: "mandate", content: "Backs early-stage industrial decarbonization hardware." },
  });
  const ranked = rankCandidates([boiler, real], SPEC);
  const b = ranked.find((r) => r.record_id === "no_thesis")!;
  const h = ranked.find((r) => r.record_id === "has_thesis")!;

  // Identical evidenceDistance: without the non-statement guard these tie exactly, which
  // is how records with nothing on file held top places for every unrelated thesis.
  assert.equal(b.scores.fit, 0, "a non-statement is evidence of absence, never of fit");
  assert.ok(h.scores.fit > 0, "a real thesis still scores");
  assert.ok(h.score > b.score, "the record with a thesis must outrank the one without");
  assert.ok(b.gaps.includes("no investing thesis on file"));
  assert.equal(ranked[0].record_id, "has_thesis");
});

test("a sub-score that is identical across candidates does not consume weight", () => {
  // Only fit varies; reach/recency/trust are flat. All the weight must fall on fit, so the
  // composite gap equals the fit gap exactly — not 0.35 of it.
  const mk = (id: string, content: string, dist: number) =>
    base({ record_id: id, evidenceDistance: dist, evidenceChunk: { facet: "mandate", content } });
  const ranked = rankCandidates(
    [mk("a", "industrial decarbonization thesis", 0.2), mk("b", "industrial decarbonization thesis", 0.9)],
    SPEC,
  );
  const w = effectiveWeights(ranked.map((r) => r.scores));
  assert.equal(w.fit, 1, "fit is the only varying sub-score, so it takes the whole weight");
  assert.equal(w.reach, 0);
  assert.equal(w.recency, 0);
  assert.equal(w.trust, 0);

  const [top, bottom] = ranked;
  assert.equal(top.record_id, "a");
  const fitGap = top.scores.fit - bottom.scores.fit;
  assert.ok(Math.abs((top.score - bottom.score) - fitGap) < 1e-9,
    "a flat sub-score must not compress the signal that varies");
});

test("effectiveWeights falls back to the static weights when nothing varies", () => {
  const flat = [
    { fit: 0.5, reach: 1, recency: 0, trust: 0.48 },
    { fit: 0.5, reach: 1, recency: 0, trust: 0.48 },
  ];
  assert.deepEqual(effectiveWeights(flat), WEIGHTS);
  // A single candidate has no ordering to inform either.
  assert.deepEqual(effectiveWeights([{ fit: 0.5, reach: 1, recency: 0, trust: 0.48 }]), WEIGHTS);
});

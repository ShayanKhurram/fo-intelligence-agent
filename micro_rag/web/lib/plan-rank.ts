// Plan ranking — micro_rag_plan.md T42.2. The comparison pass that turns a retrieved
// candidate list into a *plan*: a deterministic, explainable ordering. Pure: no imports
// from ./db, ./embeddings, or ./ollama, and no `new Date()` — recency is computed against
// `spec.asOf` so the same corpus + spec always produces the same order. A scorer that
// reads the wall clock cannot be tested, and a plan that reshuffles between two identical
// requests is not a plan.
//
// Grade, never gate. No record in the corpus has a `verified` email (0 of 339) and only
// 5 have cross-class verified evidence, so any rule that *requires* either returns the
// empty set and the feature ships broken. Low-evidence records are scored low and still
// returned, with the gap named — reachability and trust are scores, never predicates.

import type { RecordRow } from "./types.ts";
import type { PlanSpec } from "./plan-spec.ts";

export const WEIGHTS = { fit: 0.35, reach: 0.30, recency: 0.20, trust: 0.15 } as const;

export type EvidenceChunk = { facet: string; content: string };

export type PlanCandidate = RecordRow & {
  // Precomputed by T42.3's best-evidence sweep — cosine distance of the best evidence
  // chunk to the spec thesis. Lower is better. null when no evidence chunk was found;
  // that is scored, not crashed on.
  evidenceDistance: number | null;
  evidenceChunk: EvidenceChunk | null;
  // `principal_linkedin` is provenance-sourced and not on every RecordRow, so it is
  // optional here. When present it slots into the reach ladder between phone and a
  // bare name; when absent the ladder just skips that rung.
  principal_linkedin?: string | null;
};

export type SubScores = { fit: number; reach: number; recency: number; trust: number };

export type RankedCandidate = {
  record_id: string;
  entity_name: string;
  score: number;
  scores: SubScores;
  why: string[];
  gaps: string[];
  evidenceChunk: EvidenceChunk | null;
};

const UNRELIABLE_STATUSES = new Set([
  "could_not_verify",
  "removed_failed_validation",
  "contradicted",
]);

// Settled-but-weak email statuses. A `first.last@domain` address constructed by the
// pipeline's pattern matcher (or one whose value only passed a format check) is NOT the
// same evidence as an address someone actually sourced. They stay reachable (above a
// usable phone, because an address you can at least try is better than none) but rank
// strictly below a `single_source`/`confirmed` address. Found by the 501-row re-measure:
// `pattern_inferred` did not exist at 339 and now covers 38 records.
const INFERRED_EMAIL_STATUSES = new Set(["pattern_inferred", "format_only"]);

// --- date arithmetic without `new Date()` -------------------------------------

// Howard Hinnant's `days_from_civil` — converts a (y, m, d) to a count of days since
// 1970-01-01 with no Date object and no wall-clock read. Lets recency diff two ISO
// dates deterministically.
function daysFromCivil(y: number, m: number, d: number): number {
  y -= m <= 2 ? 1 : 0;
  const era = Math.floor((y >= 0 ? y : y - 399) / 400);
  const yoe = y - era * 400;
  const doy = Math.floor((153 * (m + (m > 2 ? -3 : 9)) + 2) / 5) + d - 1;
  const doe = yoe * 365 + Math.floor(yoe / 4) - Math.floor(yoe / 100) + doy;
  return era * 146097 + doe - 719468;
}

/** Parses the leading YYYY-MM-DD of an ISO date/time string into a day count, or null. */
function toDayNumber(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return null;
  return daysFromCivil(+m[1], +m[2], +m[3]);
}

// --- sub-scores ---------------------------------------------------------------

function fitScore(c: PlanCandidate, spec: PlanSpec): { score: number; why: string[]; gaps: string[] } {
  const why: string[] = [];
  const gaps: string[] = [];

  // Evidence distance → similarity in [0,1]. Cosine distance ranges [0,2]; map linearly
  // so 0 distance = 1.0 and 2 = 0. Clamp for safety.
  let distScore: number;
  if (c.evidenceDistance == null) {
    distScore = 0;
    gaps.push("no evidence chunk for the thesis");
  } else {
    distScore = Math.max(0, Math.min(1, 1 - c.evidenceDistance / 2));
    why.push(`evidence distance ${c.evidenceDistance.toFixed(3)} (${c.evidenceChunk?.facet ?? "no facet"})`);
  }

  // fit_tags overlap with the spec's mandate keywords. Fraction of spec tags the
  // candidate covers; 0 when either side is empty (no signal, not a penalty beyond 0).
  let tagsScore = 0;
  const specTags = spec.mandates_any ?? [];
  if (specTags.length > 0 && c.fit_tags.length > 0) {
    const specSet = new Set(specTags.map((t) => t.toLowerCase()));
    const hit = c.fit_tags.filter((t) => specSet.has(t.toLowerCase())).length;
    tagsScore = hit / specTags.length;
    if (hit > 0) why.push(`matches ${hit} of ${specTags.length} spec mandate tag${hit === 1 ? "" : "s"}`);
  }

  const score = 0.75 * distScore + 0.25 * tagsScore;
  return { score, why, gaps };
}

function reachScore(c: PlanCandidate): { score: number; why: string[]; gaps: string[] } {
  const why: string[] = [];
  const gaps: string[] = [];

  const emailUsable = !!c.principal_email && !UNRELIABLE_STATUSES.has(c.principal_email_status);
  const phoneUsable = !!c.principal_phone && !UNRELIABLE_STATUSES.has(c.principal_phone_status);
  const linkedin = !!c.principal_linkedin;
  const named = !!c.principal_name;

  // The ladder the brief fixes: single_source email > phone > linkedin > bare name > nothing.
  if (emailUsable) {
    if (INFERRED_EMAIL_STATUSES.has(c.principal_email_status)) {
      // Reachable, but the address is inferred / format-checked rather than sourced —
      // the `why` line has to say so, so a user reading the plan can tell the two apart
      // without opening the drawer.
      return {
        score: 0.85,
        why: [`email address inferred, not sourced (${c.principal_email_status})`],
        gaps,
      };
    }
    return {
      score: 1.0,
      why: [`reachable via email (${c.principal_email_status})`],
      gaps,
    };
  }
  if (phoneUsable) {
    why.push("reachable via phone");
    if (!c.principal_email) gaps.push("no email");
    return { score: 0.75, why, gaps };
  }
  if (linkedin) {
    why.push("linkedin available");
    if (!c.principal_email) gaps.push("no email");
    if (!c.principal_phone) gaps.push("no phone");
    return { score: 0.5, why, gaps };
  }
  if (named) {
    why.push("named principal but no contact path");
    gaps.push("no email", "no phone", "no linkedin");
    return { score: 0.25, why, gaps };
  }
  gaps.push("no named principal", "no email", "no phone", "no linkedin");
  return { score: 0.0, why, gaps };
}

function recencyScore(c: PlanCandidate, spec: PlanSpec): { score: number; why: string[]; gaps: string[] } {
  const why: string[] = [];
  const gaps: string[] = [];
  const asOfDays = toDayNumber(spec.asOf);
  const signalDays = toDayNumber(c.most_recent_signal_date);
  if (asOfDays == null || signalDays == null) {
    gaps.push("no recent signal date");
    return { score: 0.0, why, gaps };
  }
  const days = asOfDays - signalDays;
  if (days <= 90) {
    why.push(`most recent signal ${days}d old (≤90d)`);
    return { score: 1.0, why, gaps };
  }
  if (days <= 180) {
    why.push(`most recent signal ${days}d old (≤180d)`);
    return { score: 0.75, why, gaps };
  }
  if (days <= 365) {
    why.push(`most recent signal ${days}d old (≤365d)`);
    return { score: 0.5, why, gaps };
  }
  why.push(`most recent signal ${days}d old (stale)`);
  return { score: 0.25, why, gaps };
}

function trustScore(c: PlanCandidate): { score: number; why: string[]; gaps: string[] } {
  const why: string[] = [];
  const gaps: string[] = [];
  // verified > single_source_only > thin — a blunt ladder matching record_confidence.
  let base: number;
  switch (c.record_confidence) {
    case "verified_evidence_present":
      base = 1.0;
      break;
    case "single_source_only":
      base = 0.6;
      break;
    default:
      base = 0.3;
      break;
  }
  why.push(`record confidence: ${c.record_confidence}`);
  const caveated = c.outcome === "ship_with_caveats";
  if (caveated) {
    base = base * 0.8;
    why.push("scaled by 0.8 (outcome: ship_with_caveats)");
  }
  return { score: base, why, gaps };
}

// --- main ---------------------------------------------------------------------

export function rankCandidates(candidates: PlanCandidate[], spec: PlanSpec): RankedCandidate[] {
  const ranked: RankedCandidate[] = candidates.map((c) => {
    const fit = fitScore(c, spec);
    const reach = reachScore(c);
    const recency = recencyScore(c, spec);
    const trust = trustScore(c);

    const scores: SubScores = {
      fit: fit.score,
      reach: reach.score,
      recency: recency.score,
      trust: trust.score,
    };
    const score =
      WEIGHTS.fit * scores.fit +
      WEIGHTS.reach * scores.reach +
      WEIGHTS.recency * scores.recency +
      WEIGHTS.trust * scores.trust;

    const why = [...fit.why, ...reach.why, ...recency.why, ...trust.why];
    const gaps = [...fit.gaps, ...reach.gaps, ...recency.gaps, ...trust.gaps];

    return {
      record_id: c.record_id,
      entity_name: c.entity_name,
      score,
      scores,
      why,
      gaps,
      evidenceChunk: c.evidenceChunk,
    };
  });

  // Determinism: score desc, then record_id asc. Ties break on record_id so the same
  // corpus always produces the same order regardless of input ordering.
  ranked.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    return a.record_id < b.record_id ? -1 : a.record_id > b.record_id ? 1 : 0;
  });
  return ranked;
}
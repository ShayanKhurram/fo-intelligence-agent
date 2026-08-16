import { NextRequest } from "next/server";
import { getPool } from "@/lib/db";
import { parsePlanRequest, type PlanSpec, type ParsedPlan } from "@/lib/plan-spec";
import { sweepCandidates, bestEvidencePerRecord, type Excluded } from "@/lib/plan-retrieval";
import { rankCandidates, type PlanCandidate } from "@/lib/plan-rank";
import {
  loadRecordFacts,
  generateAnswer,
  verifyEntailment,
  checkSentence,
  splitSentences,
} from "@/lib/grounding";
import type { RetrievedChunk } from "@/lib/retrieval";
import { noMatchMessage, entailmentDiscardedMessage, timeoutMessage } from "@/lib/failures";
import { sseResponse, type Emit } from "@/lib/sse";

// Same Hobby-tier ceiling as /api/query. Budget: one embedding call (best-evidence
// sweep) and one generation call (the approach notes) — parsePlanRequest is a pure
// heuristic, sweepCandidates is SQL, loadRecordFacts is SQL, verifyEntailment is local.
export const maxDuration = 60;

// The plan route declines anything that isn't a plan ask — it never silently falls
// through to a search. Pointed at /api/query, where search and aggregate already live.
const NOT_A_PLAN_MESSAGE =
  "That doesn't look like a first-approach request — tell me about a raise and which offices to approach, or use /api/query for search and counts.";

// Copied from app/api/query/route.ts per the brief (do not refactor the query route to
// share it). One writer, never throws into the response path.
async function logQuery(entry: Record<string, unknown>) {
  try {
    const pool = getPool();
    await pool.query(
      `INSERT INTO query_log (query_text, intent, parsed_filters, relaxed_filters, retrieved_record_ids,
        top_score, gate1_decision, raw_answer, stripped_sentences, stripped_fraction, gate2_decision, final_answer, build_hash)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)`,
      [
        entry.query_text, entry.intent, JSON.stringify(entry.parsed_filters ?? {}),
        JSON.stringify(entry.relaxed_filters ?? []), entry.retrieved_record_ids ?? [],
        entry.top_score ?? null, entry.gate1_decision ?? null, entry.raw_answer ?? null,
        JSON.stringify(entry.stripped_sentences ?? []), entry.stripped_fraction ?? null,
        entry.gate2_decision ?? null, entry.final_answer ?? null, entry.build_hash ?? null,
      ]
    );
  } catch (e) {
    // Logging must never break the response to the user.
    console.error("query_log write failed", e);
  }
}

// Relaxation for the sweep's CORE filters only (entity_type / hq_state / aum_min /
// aum_max). Mirrors retrieval.ts's relaxOneFilter order (aum -> state -> type), minus
// `mandates_any` which the sweep deliberately does not filter on. Used only when the
// sweep matches NOTHING — we relax to find any approachable office rather than widen
// into an unrelated list when rows exist but are all excluded.
function relaxOnePlanFilter(spec: PlanSpec): { next: PlanSpec; description: string } | null {
  const order: (keyof PlanSpec)[] = ["aum_min", "aum_max", "hq_state", "entity_type"];
  for (const key of order) {
    if (spec[key] !== undefined) {
      const next = { ...spec };
      const dropped = next[key];
      delete next[key];
      const label: Record<string, string> = {
        aum_min: "minimum AUM",
        aum_max: "maximum AUM",
        hq_state: `state (${dropped})`,
        entity_type: `office type (${dropped})`,
      };
      return { next, description: label[key] ?? String(key) };
    }
  }
  return null;
}

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => null);
  const query: string = body?.query?.trim();

  if (!query) {
    return sseResponse(async (emit) => {
      emit({ type: "done", records: [], relaxedFilters: [], error: true, finalAnswerFallback: "Ask a question first." });
    });
  }

  return sseResponse(async (emit) => {
    try {
      await runPlan(query, emit);
    } catch (e) {
      console.error("plan failed", e);
      await logQuery({ query_text: query, intent: "plan", final_answer: timeoutMessage() });
      emit({ type: "done", records: [], relaxedFilters: [], error: true, finalAnswerFallback: timeoutMessage() });
    }
  });
}

async function runPlan(query: string, emit: Emit) {
  // asOf is the route's own "today" — the only place a wall-clock read is allowed (the
  // ranker in plan-rank.ts is forbidden `new Date()` precisely so it stays testable).
  const asOf = new Date().toISOString().slice(0, 10);

  emit({ type: "stage", id: "understanding", label: "Reading your question", status: "active" });
  const parsed: ParsedPlan = await parsePlanRequest(query, asOf);
  emit({ type: "filters", filters: [], parsedFilters: parsed });
  emit({ type: "stage", id: "understanding", label: "Reading your question", status: "done" });

  // 1. Intent gate — this route does not silently do a search.
  if (parsed.intent !== "plan") {
    await logQuery({ query_text: query, intent: parsed.intent, parsed_filters: parsed, gate1_decision: "decline_out_of_scope" });
    emit({ type: "done", records: [], relaxedFilters: [], declined: true, finalAnswerFallback: NOT_A_PLAN_MESSAGE });
    return;
  }

  // 2. Sweep with core filters + relaxation trail. Relax ONLY when the sweep matches
  // nothing at all (0 rows) — that is "a query matching nothing", which declines rather
  // than widening into an unrelated list. When rows match but every one is excluded,
  // we proceed with an empty shortlist and the full excluded appendix (the gaps are
  // half the deliverable, not something to hide behind a decline).
  emit({ type: "stage", id: "filtering", label: "Sweeping the dataset", status: "active" });
  let currentSpec: PlanSpec = parsed;
  const relaxedFilters: string[] = [];
  let sweep = await sweepCandidates(currentSpec);
  let totalRows = sweep.candidates.length + sweep.excluded.length;
  while (totalRows === 0) {
    const relaxed = relaxOnePlanFilter(currentSpec);
    if (!relaxed) break;
    currentSpec = relaxed.next;
    relaxedFilters.push(relaxed.description);
    sweep = await sweepCandidates(currentSpec);
    totalRows = sweep.candidates.length + sweep.excluded.length;
  }
  // A plan that silently ignored most of the corpus is exactly the kind of quiet
  // dishonesty this project's gates exist to prevent — so when the cap binds, the
  // filtering stage says so explicitly ("ranked N of M matching records").
  const filteringDetail = sweep.truncated
    ? `ranked ${sweep.sweptConsidered.toLocaleString()} of ${sweep.sweptTotal.toLocaleString()} matching records`
    : `${totalRows} candidate record(s)`;
  emit({ type: "stage", id: "filtering", label: "Sweeping the dataset", status: "done", detail: filteringDetail });

  // Gate 1 — zero matches decline (with the relaxation trail), never a quiet widening.
  if (totalRows === 0) {
    const answer = noMatchMessage(relaxedFilters, 0);
    await logQuery({
      query_text: query, intent: "plan", parsed_filters: parsed, relaxed_filters: relaxedFilters,
      gate1_decision: "decline_zero_results", final_answer: answer,
    });
    emit({ type: "done", records: [], relaxedFilters: relaxedFilters, declined: true, finalAnswerFallback: answer });
    return;
  }

  // 3. Best-evidence sweep — embed the thesis ONCE, one round-trip for every candidate.
  emit({ type: "stage", id: "matching", label: "Matching mandate and activity", status: "active" });
  const candidateIds = sweep.candidates.map((c) => c.record_id);
  const evidence = await bestEvidencePerRecord(candidateIds, parsed.thesis);

  // Attach the precomputed evidence to each candidate — the ranker is pure and does no
  // retrieval, so it receives the result rather than issuing it.
  const candidates: PlanCandidate[] = sweep.candidates.map((r) => {
    const ev = evidence.get(r.record_id) ?? null;
    return {
      ...r,
      evidenceDistance: ev ? ev.distance : null,
      evidenceChunk: ev ? { facet: ev.facet, content: ev.content } : null,
    };
  });

  // 4. Rank — the structured comparison pass. The ranked table is emitted from these
  // scores and never passes through the model.
  const ranked = rankCandidates(candidates, currentSpec).slice(0, currentSpec.top_n);
  emit({
    type: "plan",
    rows: ranked,
    excluded: sweep.excluded,
    candidateCount: sweep.candidates.length,
    sweptTotal: sweep.sweptTotal,
    sweptConsidered: sweep.sweptConsidered,
    truncated: sweep.truncated,
  });
  emit({ type: "stage", id: "matching", label: "Matching mandate and activity", status: "done" });

  // 5. Load confirmed facts for the shortlist only (Gate 3: loadRecordFacts never hands
  // the model an unsettled field — we do not build our own fact sheet).
  const shortlistIds = ranked.map((r) => r.record_id);
  const facts = await loadRecordFacts(shortlistIds);

  // 6. ONE generation pass — a short approach note per shortlisted office, grounded in
  // the evidence chunks and confirmed facts. The model can only tag what Gate 3 let
  // through, so it cannot invent a contact path or a mandate the corpus doesn't support.
  emit({ type: "stage", id: "checking", label: "Checking evidence", status: "active" });

  const genChunks: RetrievedChunk[] = ranked
    .map((r) => {
      const ev = r.evidenceChunk;
      if (!ev) return null;
      return {
        chunk_id: `${r.record_id}::${ev.facet}`,
        record_id: r.record_id,
        facet: ev.facet,
        content: ev.content,
        entity_name: r.entity_name,
      };
    })
    .filter((c): c is RetrievedChunk => c !== null);

  const noteQuery =
    `Write a concise first-approach note for each family office listed above. For each office, ` +
    `one or two sentences a fundraiser could use to open a conversation, citing only confirmed ` +
    `fields and the retrieved mandate/activity context with [record_id:field] tags. ` +
    `The fundraiser's ask: ${query}`;

  let rawNotes = "";
  let gate2Decision: "proceed" | "discard_over_threshold" = "proceed";
  let strippedFraction = 0;

  if (shortlistIds.length === 0) {
    // No approachable offices — the plan's gap half is the excluded appendix already
    // emitted. No generation call is needed (and none would have anything to cite).
    emit({ type: "stage", id: "checking", label: "Checking evidence", status: "done" });
    await logQuery({
      query_text: query, intent: "plan", parsed_filters: parsed, relaxed_filters: relaxedFilters,
      retrieved_record_ids: candidateIds, gate1_decision: "proceed", final_answer: "",
    });
    emit({ type: "done", records: shortlistIds, relaxedFilters: relaxedFilters });
    return;
  }

  rawNotes = await generateAnswer(noteQuery, genChunks, facts);
  const entailment = verifyEntailment(rawNotes, facts);
  gate2Decision = entailment.decision;
  strippedFraction = entailment.strippedFraction;

  await logQuery({
    query_text: query, intent: "plan", parsed_filters: parsed, relaxed_filters: relaxedFilters,
    retrieved_record_ids: candidateIds, gate1_decision: "proceed", raw_answer: rawNotes,
    stripped_sentences: entailment.strippedSentences, stripped_fraction: entailment.strippedFraction,
    gate2_decision: entailment.decision,
    final_answer: entailment.decision === "proceed" ? entailment.finalAnswer : entailmentDiscardedMessage(),
  });

  if (entailment.decision === "discard_over_threshold") {
    emit({ type: "stage", id: "checking", label: "Checking evidence", status: "done" });
    emit({
      type: "done", records: shortlistIds, relaxedFilters: relaxedFilters,
      strippedFraction: entailment.strippedFraction, discarded: true,
      finalAnswerFallback: entailmentDiscardedMessage(),
    });
    return;
  }

  // Reveal the verified notes sentence-by-sentence with their claim pills — the same
  // shape /api/query streams, so the client's token/claim_verified machinery works
  // unchanged. Sentences Gate 2 stripped are simply never emitted.
  for (const sentence of splitSentences(entailment.finalAnswer)) {
    const check = checkSentence(sentence, facts);
    if (check.kind === "claim") {
      emit({ type: "token", text: check.display, kind: "claim" });
      emit({
        type: "claim_verified",
        claim: { sentence: check.display, recordId: check.recordId, field: check.field, value: check.value, status: check.status },
      });
    } else if (check.kind === "neutral") {
      emit({ type: "token", text: check.display, kind: "neutral" });
    }
  }

  emit({ type: "stage", id: "checking", label: "Checking evidence", status: "done" });
  emit({ type: "done", records: shortlistIds, relaxedFilters: relaxedFilters, strippedFraction: entailment.strippedFraction });
}
import { NextRequest } from "next/server";
import { getPool } from "@/lib/db";
import { understandQuery, filtersToChips, type ParsedFilters } from "@/lib/query-understanding";
import { selectCandidates } from "@/lib/candidates";
import { runAggregate } from "@/lib/aggregate";
import { loadRecordFacts, generateAnswerStream, verifyEntailment, checkSentence, splitSentences, normalizeTagPosition } from "@/lib/grounding";
import { loadRecordRows } from "@/lib/records";
import { noMatchMessage, entailmentDiscardedMessage, timeoutMessage } from "@/lib/failures";
import { sseResponse, type Emit } from "@/lib/sse";
import { judgeRelevance } from "@/lib/relevance-judge";

// T44.3 — `/api/query` is the only endpoint. The separate `/api/plan` mode is gone; the
// answer shape (`count` / `one` / `many`) follows the question's grammatical form, and the
// unified retrieval core (`selectCandidates`) feeds every shape. The SSE contract and
// stage ids are unchanged so the client keeps working: the `plan` event still carries the
// ranked rows + excluded appendix, scored entirely before any model call so the ranked table
// is never authored by generation. T49.3 narrows that rule rather than breaking it: the
// relevance judge runs before the `plan` event but may only SUBTRACT rows it identifies as
// irrelevant — it cannot reorder, rescore, or add, and it fails open. See step 4b.

// Cold start downloads the ONNX embedding model (~23MB quantized) and inits onnxruntime
// before the first inference; that plus the LLM stream can exceed the default limit. 60s
// is the Hobby-tier ceiling. Budget: one embedding (inside selectCandidates) + the T49.3
// relevance-judge call (cheapest tier, classification only) + one generation pass. The judge
// is why the cheap tier matters there: generation alone already spends ~25-30s of this.
export const maxDuration = 60;

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

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => null);
  const query: string = body?.query?.trim();
  const overrideFilters: ParsedFilters | undefined = body?.overrideFilters ?? undefined;

  if (!query) {
    return sseResponse(async (emit) => {
      emit({ type: "done", records: [], relaxedFilters: [], error: true, finalAnswerFallback: "Ask a question first." });
    });
  }

  return sseResponse(async (emit) => {
    try {
      await runQuery(query, overrideFilters, emit);
    } catch (e) {
      console.error("query failed", e);
      await logQuery({ query_text: query, intent: "many", final_answer: timeoutMessage() });
      emit({ type: "done", records: [], relaxedFilters: [], error: true, finalAnswerFallback: timeoutMessage() });
    }
  });
}

async function runQuery(query: string, overrideFilters: ParsedFilters | undefined, emit: Emit) {
  // asOf is the route's own "today" — the one place a wall-clock read belongs. The ranker
  // (plan-rank.ts) is forbidden `new Date()` precisely so it stays deterministic and testable.
  const asOf = new Date().toISOString().slice(0, 10);

  emit({ type: "stage", id: "understanding", label: "Reading your question", status: "active" });
  const parsed = await understandQuery(query, asOf);
  // A filter-chip removal resubmits with the caller's already-parsed filters (minus the
  // dropped one) rather than re-parsing raw text. shape / top_n / asOf still come from the
  // query parse; only the filters are overridden.
  const understanding = { ...parsed, filters: overrideFilters ?? parsed.filters };
  const chips = filtersToChips(understanding.filters);
  emit({ type: "filters", filters: chips, parsedFilters: understanding.filters });
  emit({ type: "stage", id: "understanding", label: "Reading your question", status: "done" });

  // 2. shape === "count" → the existing aggregate short-circuit, unchanged.
  if (understanding.shape === "count") {
    emit({ type: "stage", id: "filtering", label: "Counting matching records", status: "active" });
    const { count, filterDescription } = await runAggregate(understanding.filters);
    const answer = `${count} record(s) match: ${filterDescription}.`;
    emit({ type: "stage", id: "filtering", label: "Counting matching records", status: "done" });
    await logQuery({
      query_text: query, intent: understanding.shape, parsed_filters: understanding.filters,
      final_answer: answer, gate1_decision: "proceed",
    });
    emit({ type: "done", records: [], relaxedFilters: [], count, finalAnswerFallback: answer });
    return;
  }

  // 3. selectCandidates — the unified retrieval core (sweep + semantic + lexical fused +
  // rank). It relaxes one core filter at a time when the sweep matches nothing and reports
  // the trail; the route just consumes the result.
  emit({ type: "stage", id: "filtering", label: "Filtering the dataset", status: "active" });
  const select = await selectCandidates(understanding);
  const totalRows = select.sweptConsidered;
  // A plan that silently ignored most of the corpus is exactly the kind of quiet dishonesty
  // this project's gates exist to prevent — so when the cap binds, the filtering stage says so.
  const filteringDetail = select.truncated
    ? `ranked ${select.sweptConsidered.toLocaleString()} of ${select.sweptTotal.toLocaleString()} matching records`
    : `${totalRows} candidate record(s)`;
  emit({ type: "stage", id: "filtering", label: "Filtering the dataset", status: "done", detail: filteringDetail });

  // 4. Gate 1 — zero rows decline with the relaxation trail, as /api/plan did.
  if (totalRows === 0) {
    const answer = noMatchMessage(select.relaxedFilters, 0);
    await logQuery({
      query_text: query, intent: understanding.shape, parsed_filters: understanding.filters,
      relaxed_filters: select.relaxedFilters, gate1_decision: "decline_zero_results", final_answer: answer,
    });
    emit({ type: "done", records: [], relaxedFilters: select.relaxedFilters, declined: true, finalAnswerFallback: answer });
    return;
  }

  // 4b. T49.3 — LLM relevance judge over the ranked shortlist.
  //
  // This AMENDS the invariant stated below, deliberately. The rule was "the ranked table is
  // emitted before any model call, so a model that cannot invent the order cannot flatter the
  // list". That rule exists to stop a model reordering or inflating results, and it still
  // holds: the judge cannot reorder, cannot rescore, and cannot add. It may only remove rows
  // it positively identifies as irrelevant, and `judgeRelevance` fails OPEN on every error or
  // unparseable reply. What it buys is the one thing embedding distance provably cannot do on
  // this corpus — read "Geography focus: Subscribers only." and see that it does not answer a
  // question about climate (see MAX_EVIDENCE_DISTANCE in lib/candidates.ts for the
  // measurements that rule out doing this with a threshold).
  emit({ type: "stage", id: "matching", label: "Matching mandate and activity", status: "active" });
  // A purely structural question ("based in Texas?") has no topic to be relevant TO — the SQL
  // filter already established relevance, and the judge's own rules tell it to keep every row.
  // Skipping the call there is free correctness AND removes ~10s from the budget: measured
  // worst case for this route is ~50s against maxDuration = 60, so a call that cannot change
  // the answer is one worth not making.
  const judged = understanding.thesis
    ? await judgeRelevance(query, select.ranked)
    : { keep: new Set(select.ranked.map((r) => r.record_id)), dropped: [], applied: false };
  const rankedRows = select.ranked.filter((r) => judged.keep.has(r.record_id));
  // Judged-out rows join the excluded appendix, same convention as the structural sweep and
  // the T49 relevance gate — a dropped record is always reported, never silently vanished.
  const excludedRows = [
    ...select.excluded,
    ...judged.dropped.map((d) => ({ ...d, reason: "does not answer the question asked" })),
  ];
  const candidateCount = totalRows - excludedRows.length;
  const shortlistIds = rankedRows.map((r) => r.record_id);

  // 5. Load confirmed facts (Gate 3 — loadRecordFacts never hands the model an unsettled
  // field) and full record rows for the rail. The ranked table (plan event) still carries the
  // structured scores, computed before any model call — the judge subtracts from that table,
  // it never authors it.
  const [facts, recordRows] = await Promise.all([loadRecordFacts(shortlistIds), loadRecordRows(shortlistIds)]);
  emit({ type: "records", records: recordRows, candidateCount });
  emit({
    type: "plan",
    rows: rankedRows,
    excluded: excludedRows,
    candidateCount,
    sweptTotal: select.sweptTotal,
    sweptConsidered: select.sweptConsidered,
    truncated: select.truncated,
  });
  emit({ type: "stage", id: "matching", label: "Matching mandate and activity", status: "done" });

  // 6. One generation pass.
  //   shape "one"  → narrate the top-ranked record only, the way a lookup always answered.
  //   shape "many" → per-office approach notes over the shortlist.
  emit({ type: "stage", id: "checking", label: "Checking evidence", status: "active" });

  const isMany = understanding.shape === "many";
  const narrationRows = isMany ? rankedRows : rankedRows.slice(0, 1);
  const narrationIds = narrationRows.map((r) => r.record_id);

  if (narrationIds.length === 0) {
    // Rows matched but none survived into a shortlist (all excluded, or no evidence to
    // cite). The excluded appendix is the honest deliverable here; no generation call.
    emit({ type: "stage", id: "checking", label: "Checking evidence", status: "done" });
    await logQuery({
      query_text: query, intent: understanding.shape, parsed_filters: understanding.filters,
      relaxed_filters: select.relaxedFilters, retrieved_record_ids: shortlistIds,
      gate1_decision: "proceed", final_answer: "",
    });
    emit({ type: "done", records: shortlistIds, relaxedFilters: select.relaxedFilters });
    return;
  }

  // genChunks: the evidence chunks the model is allowed to see. One per shortlisted
  // office (the best mandate/activity/why_now chunk) — same shape the plan route used.
  const genChunks = narrationRows
    .filter((r) => r.evidenceChunk)
    .map((r) => ({
      chunk_id: r.record_id + "::" + r.evidenceChunk!.facet,
      record_id: r.record_id,
      facet: r.evidenceChunk!.facet,
      content: r.evidenceChunk!.content,
      entity_name: r.entity_name,
    }));

  // The generation prompt. For "one" the question itself is the prompt (a lookup answer,
  // grounded by the system prompt's [record_id:field] rule); for "many" it is the
  // per-office approach-note instruction the plan route used.
  // The user's question IS the prompt. This previously hardcoded "write a concise
  // first-approach note for each family office listed above" for every `many` query and
  // appended the real question as a trailing hint — so every answer came back as the same
  // roster of "I noticed <background>. I'd love to connect with <principal_name>",
  // whatever had been asked. That instruction was correct on the old /api/plan route,
  // which only ever produced outreach plans; carrying it into the route that answers
  // everything is what made every answer look identical.
  //
  // Deliberately NOT solved by classifying whether the ask is an outreach request — that
  // is the phrasing-detector mistake that scored 1 of 14. Ask the question, supply the
  // records, and let the answer take the shape the question calls for.
  const noteQuery = isMany
    ? `${query}\n\n(Several records are provided above. Answer using the ones that actually bear on the question.)`
    : understanding.semantic;

  // 7. Stream the answer, revealing it sentence-by-sentence: Gate 2 (entailment) must
  // clear a sentence before it's ever shown, so text is never displayed transiently only
  // to be yanked back. The check is a mechanical tag lookup, so the reveal-then-verify
  // gap is imperceptible. This is the existing UX and it works — kept unchanged.
  let rawAnswer = "";
  let finalizedCount = 0;

  const finalizeUpTo = (limit: number) => {
    const normalized = normalizeTagPosition(rawAnswer);
    const sentences = splitSentences(normalized);
    // If the last (still-growing) segment is a half-streamed [record:field] tag — a "["
    // with no closing "]" yet — the sentence BEFORE it is the one whose tag is still
    // arriving, and normalizeTagPosition has NOT yet pulled that tag back across the
    // preceding period (its rewrite needs the complete "[...]"). splitSentences therefore
    // split at that period, leaving the second-to-last segment tagless. Finalizing it now
    // would lock it in as "invalid" and advance finalizedCount past it; when "]" lands and
    // the boundary rewrites, the now-complete tagged sentence sits at an index we have
    // already passed and is lost. Defer it (finalize one fewer) until the tag closes.
    let effective = limit;
    if (
      sentences.length >= 2 &&
      sentences[sentences.length - 1].lastIndexOf("[") > sentences[sentences.length - 1].lastIndexOf("]")
    ) {
      effective = Math.min(effective, sentences.length - 2);
    }
    while (finalizedCount < Math.min(effective, sentences.length)) {
      const sentence = sentences[finalizedCount];
      const check = checkSentence(sentence, facts);
      if (check.kind === "claim") {
        emit({ type: "token", text: check.display, kind: "claim" });
        emit({
          type: "claim_verified",
          claim: { sentence: check.display, recordId: check.recordId, field: check.field, value: check.value, status: check.status },
        });
      } else if (check.kind === "neutral" || check.kind === "authorial") {
        // T52.4 — advice ("Lead with a crypto-aligned thesis") reaches the reader as prose
        // with no claim pill, exactly like an honest "not confirmed". It asserts nothing
        // about a record, so there is nothing for a pill to point at.
        emit({ type: "token", text: check.display, kind: "neutral" });
      }
      // "structural" is markdown scaffolding — dropped silently, since this UI renders flat
      // prose and a literal "---" or "### 2." would only be noise.
      // "invalid" sentences are the whole point of Gate 2: never shown, silently dropped.
      finalizedCount++;
    }
  };

  for await (const delta of generateAnswerStream(noteQuery, genChunks, facts)) {
    rawAnswer += delta;
    const sentenceCount = splitSentences(normalizeTagPosition(rawAnswer)).length;
    // Defer the LAST split segment: it may still be growing (its trailing [record:field]
    // tag, which the model emits AFTER the sentence text, might not have streamed in yet).
    finalizeUpTo(sentenceCount - 1);
  }
  finalizeUpTo(Number.MAX_SAFE_INTEGER);

  const entailment = verifyEntailment(rawAnswer, facts);

  // 8. Log with intent set from shape.
  await logQuery({
    query_text: query, intent: understanding.shape, parsed_filters: understanding.filters,
    relaxed_filters: select.relaxedFilters, retrieved_record_ids: shortlistIds,
    gate1_decision: "proceed", raw_answer: rawAnswer,
    stripped_sentences: entailment.strippedSentences, stripped_fraction: entailment.strippedFraction,
    gate2_decision: entailment.decision,
    final_answer: entailment.finalAnswer,
  });

  emit({ type: "stage", id: "checking", label: "Checking evidence", status: "done" });
  // T51.6 — the whole-answer net is a SIGNAL, not a retraction. It used to return early
  // here with `discarded: true` and no surviving text, and the client replaced the entire
  // streamed answer with the fallback string — so a user watched a correct answer appear
  // and then be wiped. That contradicted this route's own rule (step 7 above: "text is
  // never displayed transiently only to be yanked back"), and it was wrong on the merits:
  // per-sentence Gate 2 already refused to display any sentence whose tag didn't resolve,
  // so what is on screen is grounded by construction. Retracting it destroyed correct work
  // to punish sentences the user never saw. Production bore that out — 24 of 25 discards
  // were formatting artifacts, not fabrications (see PLAN.md T51).
  //
  // The flag still rides along: the client keeps the streamed sentences and shows the
  // caution beneath them, and shows the fallback ALONE only when nothing survived (a truly
  // degenerate generation, e.g. the logged one that emitted "<pad><pad><pad>"). Logging is
  // unchanged, so the discard rate stays measurable in `query_log`.
  emit({
    type: "done", records: shortlistIds, relaxedFilters: select.relaxedFilters,
    strippedFraction: entailment.strippedFraction,
    ...(entailment.decision === "discard_over_threshold"
      ? { discarded: true, finalAnswerFallback: entailmentDiscardedMessage() }
      : {}),
  });
}
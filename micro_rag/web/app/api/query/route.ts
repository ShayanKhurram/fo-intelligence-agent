import { NextRequest } from "next/server";
import { getPool } from "@/lib/db";
import { understandQuery, filtersToChips, type ParsedFilters } from "@/lib/query-understanding";
import { hybridRetrieve } from "@/lib/retrieval";
import { runAggregate } from "@/lib/aggregate";
import { gate1, loadRecordFacts, generateAnswerStream, verifyEntailment, checkSentence, splitSentences, normalizeTagPosition } from "@/lib/grounding";
import { loadRecordRows } from "@/lib/records";
import { noMatchMessage, retrievalFloorMessage, outOfScopeMessage, entailmentDiscardedMessage, timeoutMessage } from "@/lib/failures";
import { sseResponse, type Emit } from "@/lib/sse";

// Cold start downloads the ONNX embedding model from the HF hub (~23MB quantized) and inits
// onnxruntime before the first inference; that plus the LLM stream can exceed the default
// limit. 60s is the Hobby-tier ceiling. Once an instance is warm the model is cached in /tmp
// and requests are fast.
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
      await logQuery({ query_text: query, final_answer: timeoutMessage() });
      emit({ type: "done", records: [], relaxedFilters: [], error: true, finalAnswerFallback: timeoutMessage() });
    }
  });
}

async function runQuery(query: string, overrideFilters: ParsedFilters | undefined, emit: Emit) {
  emit({ type: "stage", id: "understanding", label: "Reading your question", status: "active" });

  // A filter-chip removal resubmits with the caller's already-parsed filters (minus the
  // dropped one) rather than re-running the heuristic parser against raw text — dropping
  // "over $200M" from the text is fragile; dropping the structured key it produced isn't.
  const understanding = overrideFilters
    ? { filters: overrideFilters, semantic: query, intent: "search" as const }
    : await understandQuery(query);

  const chips = filtersToChips(understanding.filters);
  emit({ type: "filters", filters: chips, parsedFilters: understanding.filters });
  emit({ type: "stage", id: "understanding", label: "Reading your question", status: "done" });

  if (understanding.intent === "out_of_scope") {
    await logQuery({ query_text: query, intent: understanding.intent, gate1_decision: "decline_out_of_scope" });
    emit({ type: "done", records: [], relaxedFilters: [], declined: true, finalAnswerFallback: outOfScopeMessage() });
    return;
  }

  if (understanding.intent === "aggregate") {
    emit({ type: "stage", id: "filtering", label: "Counting matching records", status: "active" });
    const { count, filterDescription } = await runAggregate(understanding.filters);
    const answer = `${count} record(s) match: ${filterDescription}.`;
    emit({ type: "stage", id: "filtering", label: "Counting matching records", status: "done" });
    await logQuery({
      query_text: query, intent: understanding.intent, parsed_filters: understanding.filters,
      final_answer: answer, gate1_decision: "proceed",
    });
    emit({ type: "done", records: [], relaxedFilters: [], count, finalAnswerFallback: answer });
    return;
  }

  emit({ type: "stage", id: "filtering", label: "Filtering the dataset", status: "active" });
  let lastCandidateCount = 0;
  const retrieval = await hybridRetrieve(understanding.semantic, understanding.filters, (count) => {
    lastCandidateCount = count;
    emit({ type: "stage", id: "filtering", label: "Filtering the dataset", status: "active", detail: `${count} candidate record(s)` });
  });
  const decision = gate1(retrieval.topScore, retrieval.chunks.length, false);

  if (decision !== "proceed") {
    emit({ type: "stage", id: "filtering", label: "Filtering the dataset", status: "done" });
    const answer =
      decision === "decline_zero_results"
        ? noMatchMessage(retrieval.relaxedFilters, retrieval.candidateCount)
        : retrievalFloorMessage();
    await logQuery({
      query_text: query, intent: understanding.intent, parsed_filters: understanding.filters,
      relaxed_filters: retrieval.relaxedFilters, top_score: retrieval.topScore, gate1_decision: decision,
      final_answer: answer,
    });
    emit({
      type: "done", records: [], relaxedFilters: retrieval.relaxedFilters,
      declined: decision === "decline_low_score", finalAnswerFallback: answer,
    });
    return;
  }
  emit({ type: "stage", id: "filtering", label: "Filtering the dataset", status: "done", detail: `${lastCandidateCount} candidate record(s)` });

  emit({ type: "stage", id: "matching", label: "Matching mandate and activity", status: "active" });
  const recordIds = [...new Set(retrieval.chunks.map((c) => c.record_id))];
  const [facts, recordRows] = await Promise.all([loadRecordFacts(recordIds), loadRecordRows(recordIds)]);
  emit({ type: "records", records: recordRows, candidateCount: retrieval.candidateCount });
  emit({ type: "stage", id: "matching", label: "Matching mandate and activity", status: "done" });

  emit({ type: "stage", id: "checking", label: "Checking evidence", status: "active" });

  // Stream the answer, revealing it sentence-by-sentence rather than raw model token by
  // token: Gate 2 (entailment) must clear a sentence before it's ever shown, so text is
  // never displayed transiently only to be yanked back later. Because the check here is
  // a mechanical tag lookup (not an LLM call), the reveal-then-verify gap is imperceptible
  // in practice — this is what ui_plan.md §5 describes as "the sentence streams plain, and
  // its pill scales in only once that sentence has cleared the gate."
  let rawAnswer = "";
  let finalizedCount = 0;

  const finalizeUpTo = (limit: number) => {
    const normalized = normalizeTagPosition(rawAnswer);
    const sentences = splitSentences(normalized);
    while (finalizedCount < Math.min(limit, sentences.length)) {
      const sentence = sentences[finalizedCount];
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
      // "invalid" sentences are the whole point of Gate 2: never shown, silently dropped —
      // exactly what the non-streaming verifyEntailment() has always done.
      finalizedCount++;
    }
  };

  for await (const delta of generateAnswerStream(understanding.semantic, retrieval.chunks, facts)) {
    rawAnswer += delta;
    const sentenceCount = splitSentences(normalizeTagPosition(rawAnswer)).length;
    // Defer the LAST split segment: it may still be growing (its trailing [record:field]
    // tag, which the model emits AFTER the sentence text, might not have streamed in yet).
    finalizeUpTo(sentenceCount - 1);
  }
  // Stream is done — the last segment can no longer grow, finalize everything left.
  finalizeUpTo(Number.MAX_SAFE_INTEGER);

  const entailment = verifyEntailment(rawAnswer, facts);

  await logQuery({
    query_text: query, intent: understanding.intent, parsed_filters: understanding.filters,
    relaxed_filters: retrieval.relaxedFilters, retrieved_record_ids: recordIds, top_score: retrieval.topScore,
    gate1_decision: decision, raw_answer: rawAnswer, stripped_sentences: entailment.strippedSentences,
    stripped_fraction: entailment.strippedFraction, gate2_decision: entailment.decision,
    final_answer: entailment.decision === "proceed" ? entailment.finalAnswer : entailmentDiscardedMessage(),
  });

  if (entailment.decision === "discard_over_threshold") {
    // The whole-answer safety net fired after individual sentences had already streamed —
    // rare (>30% of claim sentences failed), but when it happens the already-shown answer
    // text/pills are not trustworthy as a set and must be retracted, not left standing.
    emit({
      type: "done", records: recordIds, relaxedFilters: retrieval.relaxedFilters,
      strippedFraction: entailment.strippedFraction, discarded: true, finalAnswerFallback: entailmentDiscardedMessage(),
    });
    return;
  }

  emit({ type: "stage", id: "checking", label: "Checking evidence", status: "done" });
  emit({
    type: "done", records: recordIds, relaxedFilters: retrieval.relaxedFilters,
    strippedFraction: entailment.strippedFraction,
  });
}

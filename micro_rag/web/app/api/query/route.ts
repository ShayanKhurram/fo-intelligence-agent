import { NextRequest, NextResponse } from "next/server";
import { getPool } from "@/lib/db";
import { understandQuery } from "@/lib/query-understanding";
import { hybridRetrieve } from "@/lib/retrieval";
import { runAggregate } from "@/lib/aggregate";
import { gate1, loadRecordFacts, generateAnswer, verifyEntailment } from "@/lib/grounding";
import { noMatchMessage, retrievalFloorMessage, outOfScopeMessage, entailmentDiscardedMessage, timeoutMessage } from "@/lib/failures";

// Cold start downloads the ONNX embedding model from the HF hub (~23MB quantized) and inits
// onnxruntime before the first inference; that plus two Ollama calls can exceed the default
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
  if (!query) {
    return NextResponse.json({ error: "query is required" }, { status: 400 });
  }

  try {
    const understanding = await understandQuery(query);

    if (understanding.intent === "out_of_scope") {
      await logQuery({ query_text: query, intent: understanding.intent, gate1_decision: "decline_out_of_scope" });
      return NextResponse.json({ answer: outOfScopeMessage(), records: [], declined: true });
    }

    if (understanding.intent === "aggregate") {
      const { count, filterDescription } = await runAggregate(understanding.filters);
      const answer = `${count} record(s) match: ${filterDescription}.`;
      await logQuery({
        query_text: query, intent: understanding.intent, parsed_filters: understanding.filters,
        final_answer: answer, gate1_decision: "proceed",
      });
      return NextResponse.json({ answer, count, records: [] });
    }

    const retrieval = await hybridRetrieve(understanding.semantic, understanding.filters);
    const decision = gate1(retrieval.topScore, retrieval.chunks.length, false);

    if (decision !== "proceed") {
      const answer =
        decision === "decline_zero_results"
          ? noMatchMessage(retrieval.relaxedFilters, retrieval.candidateCount)
          : retrievalFloorMessage();
      await logQuery({
        query_text: query, intent: understanding.intent, parsed_filters: understanding.filters,
        relaxed_filters: retrieval.relaxedFilters, top_score: retrieval.topScore, gate1_decision: decision,
        final_answer: answer,
      });
      return NextResponse.json({ answer, records: [], declined: decision === "decline_low_score" });
    }

    const recordIds = [...new Set(retrieval.chunks.map((c) => c.record_id))];
    const facts = await loadRecordFacts(recordIds);
    const rawAnswer = await generateAnswer(understanding.semantic, retrieval.chunks, facts);
    const entailment = verifyEntailment(rawAnswer, facts);

    const finalAnswer = entailment.decision === "discard_over_threshold" ? entailmentDiscardedMessage() : entailment.finalAnswer;

    await logQuery({
      query_text: query, intent: understanding.intent, parsed_filters: understanding.filters,
      relaxed_filters: retrieval.relaxedFilters, retrieved_record_ids: recordIds, top_score: retrieval.topScore,
      gate1_decision: decision, raw_answer: rawAnswer, stripped_sentences: entailment.strippedSentences,
      stripped_fraction: entailment.strippedFraction, gate2_decision: entailment.decision, final_answer: finalAnswer,
    });

    return NextResponse.json({
      answer: finalAnswer,
      records: recordIds,
      relaxedFilters: retrieval.relaxedFilters,
      strippedFraction: entailment.strippedFraction,
    });
  } catch (e) {
    console.error("query failed", e);
    await logQuery({ query_text: query, final_answer: timeoutMessage() });
    return NextResponse.json({ answer: timeoutMessage(), records: [], error: true }, { status: 200 });
  }
}

// Hybrid retrieval — micro_rag_plan.md §4.2. Structured pre-filter -> semantic +
// lexical over the candidate set -> reciprocal rank fusion -> rerank.
import { getPool } from "./db";
import { embedQuery } from "./embeddings";
import type { ParsedFilters } from "./query-understanding";

export type RetrievedChunk = {
  chunk_id: string;
  record_id: string;
  facet: string;
  content: string;
  entity_name: string;
};

export type RetrievalResult = {
  chunks: RetrievedChunk[];
  candidateCount: number;
  topScore: number;
  relaxedFilters: string[]; // human-readable descriptions of what was relaxed, in order
};

const RRF_K = 60;

function buildWhereClause(filters: ParsedFilters): { clause: string; params: unknown[] } {
  const conditions: string[] = [];
  const params: unknown[] = [];

  if (filters.entity_type) {
    params.push(filters.entity_type);
    conditions.push(`r.entity_type = $${params.length}`);
  }
  if (filters.hq_state) {
    params.push(filters.hq_state);
    conditions.push(`r.hq_state = $${params.length}`);
  }
  if (filters.aum_min != null) {
    params.push(filters.aum_min);
    conditions.push(`r.aum_usd >= $${params.length}`);
  }
  if (filters.aum_max != null) {
    params.push(filters.aum_max);
    conditions.push(`r.aum_usd <= $${params.length}`);
  }
  if (filters.mandates_any && filters.mandates_any.length > 0) {
    params.push(filters.mandates_any);
    conditions.push(`r.mandates && $${params.length}::text[]`);
  }

  return { clause: conditions.length ? `WHERE ${conditions.join(" AND ")}` : "", params };
}

async function candidateRecordIds(filters: ParsedFilters): Promise<string[]> {
  const pool = getPool();
  const { clause, params } = buildWhereClause(filters);
  const { rows } = await pool.query(`SELECT record_id FROM records r ${clause}`, params);
  return rows.map((r) => r.record_id);
}

/** Relaxes ONE filter at a time, in a fixed priority order, never silently (plan §4.2).
 * Returns the new filter set and a human-readable description of what was dropped. */
function relaxOneFilter(filters: ParsedFilters): { next: ParsedFilters; description: string } | null {
  const order: (keyof ParsedFilters)[] = ["aum_min", "aum_max", "hq_state", "mandates_any", "entity_type"];
  for (const key of order) {
    if (filters[key] !== undefined) {
      const next = { ...filters };
      const dropped = next[key];
      delete next[key];
      const label: Record<string, string> = {
        aum_min: "minimum AUM",
        aum_max: "maximum AUM",
        hq_state: `state (${dropped})`,
        mandates_any: "mandate focus",
        entity_type: `office type (${dropped})`,
      };
      return { next, description: label[key] ?? String(key) };
    }
  }
  return null;
}

async function fetchCandidateChunks(recordIds: string[]): Promise<{ chunk_id: string; record_id: string; facet: string; content: string; embedding: number[]; entity_name: string }[]> {
  if (recordIds.length === 0) return [];
  const pool = getPool();
  const { rows } = await pool.query(
    `SELECT c.chunk_id, c.record_id, c.facet, c.content, c.embedding, r.entity_name
     FROM chunks c JOIN records r ON r.record_id = c.record_id
     WHERE c.record_id = ANY($1::text[])`,
    [recordIds]
  );
  return rows;
}

async function semanticRank(queryVec: number[], recordIds: string[], topN: number): Promise<{ chunk_id: string; rank: number }[]> {
  if (recordIds.length === 0) return [];
  const pool = getPool();
  const vecLiteral = `[${queryVec.join(",")}]`;
  const { rows } = await pool.query(
    `SELECT chunk_id FROM chunks WHERE record_id = ANY($1::text[])
     ORDER BY embedding <=> $2::vector LIMIT $3`,
    [recordIds, vecLiteral, topN]
  );
  return rows.map((r, i) => ({ chunk_id: r.chunk_id, rank: i + 1 }));
}

async function lexicalRank(query: string, recordIds: string[], topN: number): Promise<{ chunk_id: string; rank: number }[]> {
  if (recordIds.length === 0) return [];
  const pool = getPool();
  const { rows } = await pool.query(
    `SELECT chunk_id, ts_rank(content_tsv, plainto_tsquery('english', $1)) AS score
     FROM chunks WHERE record_id = ANY($2::text[]) AND content_tsv @@ plainto_tsquery('english', $1)
     ORDER BY score DESC LIMIT $3`,
    [query, recordIds, topN]
  );
  return rows.map((r, i) => ({ chunk_id: r.chunk_id, rank: i + 1 }));
}

function reciprocalRankFusion(rankLists: { chunk_id: string; rank: number }[][]): Map<string, number> {
  const scores = new Map<string, number>();
  for (const list of rankLists) {
    for (const { chunk_id, rank } of list) {
      scores.set(chunk_id, (scores.get(chunk_id) ?? 0) + 1 / (RRF_K + rank));
    }
  }
  return scores;
}

export async function hybridRetrieve(
  semanticQuery: string,
  filters: ParsedFilters,
  onCandidateStep?: (count: number) => void
): Promise<RetrievalResult> {
  let currentFilters = filters;
  const relaxedFilters: string[] = [];
  let recordIds = await candidateRecordIds(currentFilters);
  onCandidateStep?.(recordIds.length);

  // Filter relaxation: never fall through to unfiltered search silently (plan §4.2).
  while (recordIds.length === 0) {
    const relaxed = relaxOneFilter(currentFilters);
    if (!relaxed) break; // nothing left to relax
    currentFilters = relaxed.next;
    relaxedFilters.push(relaxed.description);
    recordIds = await candidateRecordIds(currentFilters);
    onCandidateStep?.(recordIds.length);
  }

  if (recordIds.length === 0) {
    return { chunks: [], candidateCount: 0, topScore: 0, relaxedFilters };
  }

  const queryVec = await embedQuery(semanticQuery);
  const [semanticList, lexicalList] = await Promise.all([
    semanticRank(queryVec, recordIds, 20),
    lexicalRank(semanticQuery, recordIds, 20),
  ]);
  const fused = reciprocalRankFusion([semanticList, lexicalList]);

  const top12 = [...fused.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12);
  const topScore = top12.length > 0 ? top12[0][1] : 0;

  const allCandidateChunks = await fetchCandidateChunks(recordIds);
  const byId = new Map(allCandidateChunks.map((c) => [c.chunk_id, c]));

  // Rerank: top 5 distinct records (one chunk per record, the highest-fused one) —
  // a cross-encoder/LLM rerank is a further refinement; RRF order is used directly
  // here as the rerank input signal, which is already a reasonable ranking.
  const seenRecords = new Set<string>();
  const chunks: RetrievedChunk[] = [];
  for (const [chunkId] of top12) {
    const chunk = byId.get(chunkId);
    if (!chunk || seenRecords.has(chunk.record_id)) continue;
    seenRecords.add(chunk.record_id);
    chunks.push({
      chunk_id: chunk.chunk_id,
      record_id: chunk.record_id,
      facet: chunk.facet,
      content: chunk.content,
      entity_name: chunk.entity_name,
    });
    if (chunks.length >= 5) break;
  }

  return { chunks, candidateCount: recordIds.length, topScore, relaxedFilters };
}

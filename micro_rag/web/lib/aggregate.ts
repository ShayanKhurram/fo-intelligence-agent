// Aggregate queries — micro_rag_plan.md §4.3. "How many..." must be answered by
// parameterised SQL over the full table, never by retrieving chunks and guessing.
import { getPool } from "./db";
import type { ParsedFilters } from "./query-understanding";

export async function runAggregate(filters: ParsedFilters): Promise<{ count: number; filterDescription: string }> {
  const pool = getPool();
  const conditions: string[] = [];
  const params: unknown[] = [];
  const descriptions: string[] = [];

  if (filters.entity_type) {
    params.push(filters.entity_type);
    conditions.push(`entity_type = $${params.length}`);
    descriptions.push(`entity_type = ${filters.entity_type}`);
  }
  if (filters.hq_state) {
    params.push(filters.hq_state);
    conditions.push(`hq_state = $${params.length}`);
    descriptions.push(`hq_state = ${filters.hq_state}`);
  }
  if (filters.aum_min != null) {
    params.push(filters.aum_min);
    conditions.push(`aum_usd >= $${params.length}`);
    descriptions.push(`aum_usd >= ${filters.aum_min}`);
  }
  if (filters.aum_max != null) {
    params.push(filters.aum_max);
    conditions.push(`aum_usd <= $${params.length}`);
    descriptions.push(`aum_usd <= ${filters.aum_max}`);
  }
  if (filters.mandates_any && filters.mandates_any.length > 0) {
    params.push(filters.mandates_any);
    conditions.push(`mandates && $${params.length}::text[]`);
    descriptions.push(`mandates includes any of [${filters.mandates_any.join(", ")}]`);
  }

  const where = conditions.length ? `WHERE ${conditions.join(" AND ")}` : "";
  const { rows } = await pool.query(`SELECT COUNT(*) AS count FROM records ${where}`, params);
  return {
    count: parseInt(rows[0].count, 10),
    filterDescription: descriptions.length ? descriptions.join(" AND ") : "no filter (whole dataset)",
  };
}

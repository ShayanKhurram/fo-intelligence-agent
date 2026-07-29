// Loads the full record rows (entity name/type/AUM/principal/etc.) for a set of
// record_ids — the rail's skeleton-card data. Separate from lib/grounding.ts's
// `loadRecordFacts` (which loads confirmed field VALUES for citation) — this loads the
// records table's own denormalized columns for display, unfiltered by claim status,
// since the UI shows unconfirmed fields too (greyed, per ui_plan.md's whole premise).
import { getPool } from "./db";
import type { RecordRow } from "./types";

export async function loadRecordRows(recordIds: string[]): Promise<RecordRow[]> {
  if (recordIds.length === 0) return [];
  const pool = getPool();
  const [{ rows }, { rows: hookRows }] = await Promise.all([
    pool.query(`SELECT * FROM records WHERE record_id = ANY($1::text[])`, [recordIds]),
    // outreach_hook is a Layer-V-enriched provenance field (app/enrichment.py's wave_2),
    // not a `records` column — the main-column card's pull-quote (ui_plan.md §6) is the
    // one place it's rendered, so it's joined in here rather than added as a full column.
    pool.query(
      `SELECT record_id, value FROM provenance WHERE record_id = ANY($1::text[]) AND field_name = 'outreach_hook'`,
      [recordIds]
    ),
  ]);
  const hooks = new Map(hookRows.map((r) => [r.record_id, r.value as string]));
  // Preserve the caller's ranking order (already rank-fused by retrieval) rather than
  // whatever order Postgres happens to return.
  const byId = new Map(rows.map((r) => [r.record_id, { ...r, outreach_hook: hooks.get(r.record_id) ?? null } as RecordRow]));
  return recordIds.map((id) => byId.get(id)).filter((r): r is RecordRow => !!r);
}

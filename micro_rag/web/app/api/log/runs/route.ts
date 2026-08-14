import { NextResponse } from "next/server";
import { getPool } from "@/lib/db";

// T37 — the agent's run log, mirrored here from the machine that actually runs the agent
// (it cannot run on Vercel: 145MB of SQLite state, minutes per lead against function
// timeouts, a background scheduler loop, and a headless-Chromium fetch tier). This route
// only reads what was pushed into agent_runs / agent_field_provenance.
//
// Three shapes from one route, chosen by query param, mirroring the local API's
// /api/runs, /api/runs/{id} and /api/runs/{id}/provenance:
//   (none)                      -> the runs list
//   ?run_id=X                   -> that run's per-lead summary
//   ?run_id=X&entity_id=Y       -> that lead's full field records

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const runId = searchParams.get("run_id");
  const entityId = searchParams.get("entity_id");
  const limit = Math.min(Math.max(Number(searchParams.get("limit") ?? 50), 1), 200);

  try {
    const pool = getPool();

    if (runId && entityId) {
      const { rows } = await pool.query(
        `SELECT record FROM agent_field_provenance
         WHERE run_id = $1 AND entity_id = $2
         ORDER BY field`,
        [runId, entityId]
      );
      return NextResponse.json({
        schema_version: 1,
        run_id: runId,
        entity_id: entityId,
        count: rows.length,
        records: rows.map((r) => r.record),
      });
    }

    if (runId) {
      const { rows } = await pool.query(
        `SELECT entity_id,
                MAX(canonical_name)                      AS canonical_name,
                COUNT(*)::int                            AS field_count,
                COUNT(*) FILTER (WHERE shipped)::int     AS shipped_count,
                COUNT(*) FILTER (WHERE NOT shipped)::int AS blank_count
         FROM agent_field_provenance
         WHERE run_id = $1
         GROUP BY entity_id
         ORDER BY entity_id`,
        [runId]
      );
      const run = await pool.query(`SELECT * FROM agent_runs WHERE run_id = $1`, [runId]);
      if (run.rows.length === 0) {
        return NextResponse.json({ error: "unknown run_id" }, { status: 404 });
      }
      return NextResponse.json({ schema_version: 1, run: run.rows[0], leads: rows });
    }

    const { rows } = await pool.query(
      `SELECT r.*,
              (SELECT COUNT(DISTINCT entity_id)::int
                 FROM agent_field_provenance p WHERE p.run_id = r.run_id) AS lead_count
       FROM agent_runs r
       ORDER BY r.started_at DESC NULLS LAST
       LIMIT $1`,
      [limit]
    );
    return NextResponse.json({ schema_version: 1, runs: rows });
  } catch (e) {
    // A missing table means the agent has never pushed a log yet — a state, not a fault.
    const message = String(e);
    if (/relation .*agent_(runs|field_provenance).* does not exist/i.test(message)) {
      return NextResponse.json({ schema_version: 1, runs: [], not_synced_yet: true });
    }
    return NextResponse.json({ status: "db_unavailable", error: message }, { status: 503 });
  }
}

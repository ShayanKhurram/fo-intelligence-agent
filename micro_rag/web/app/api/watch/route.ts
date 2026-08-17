import { NextResponse } from "next/server";
import { getPool } from "@/lib/db";
import { ensureWatchSchema } from "@/lib/watch-schema";
import { loadWatchBoard } from "@/lib/watch";

// T46.3 — the Intent Watcher board. Read-only: surfaces the baseline corpus (provenance
// activity signals) plus any watch_signals rows from research runs. See lib/watch.ts.

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const pool = getPool();
    // The watcher owns its own tables; create them on first contact. The corpus tables
    // (records/provenance) are owned by the ingest and must already exist.
    await ensureWatchSchema(pool);
    const board = await loadWatchBoard();
    return NextResponse.json({ schema_version: 1, board });
  } catch (e) {
    // A missing relation is a state, not a fault — mirror app/api/log/runs/route.ts's
    // discipline. The corpus may not be ingested yet, or the watcher tables could not be
    // created (permissions); either way the board is empty, not broken.
    const message = String(e);
    if (
      /relation "?(provenance|records|watch_signals|watch_org_state|watch_runs|watch_run_targets)"? does not exist/i.test(
        message,
      )
    ) {
      return NextResponse.json({ schema_version: 1, board: { orgs: [] }, not_ready: true });
    }
    return NextResponse.json({ status: "db_unavailable", error: message }, { status: 503 });
  }
}
// T50.2 — the Intent Watcher run, now a thin Vercel adapter.
//
// The sweep itself lives in `lib/watch-run.ts`. This file only parses HTTP requests,
// calls the shared sweep module, and frames the responses. It is the FALLBACK path:
// Vercel's function timeout still caps a single connection to 45s (the budget passed to
// runSlice below), after which the browser reconnects to resume — the same resumable slice loop
// that has always run here. The Render service (`service/watch-service.ts`) hosts the
// SAME `runSlice` with a 6-hour budget so a run finishes in one connection.
//
// Every response shape the client sees is byte-identical to the pre-extraction route:
// POST returns the run object, DELETE returns {ok,run_id}, GET streams SSE. There is no
// SQL in this file — it lives entirely in `lib/watch-run.ts`.
import { NextResponse } from "next/server";
import { sseStream } from "@/lib/sse";
import { startRun, cancelRun, runSlice } from "@/lib/watch-run";
import type { WatchStreamEvent } from "@/lib/watch-types";

// 60s is the Hobby-tier ceiling (app/api/query/route.ts documents the same). The slice
// loop self-limits to a 45s wall-clock budget and emits `paused`, so a single request
// never approaches the hard limit.
export const maxDuration = 60;
export const dynamic = "force-dynamic";

// ---------------------------------------------------------------------------
// POST — start (or reuse) a run
// ---------------------------------------------------------------------------

export async function POST(req: Request) {
  const body = await req.json().catch(() => ({} as Record<string, unknown>));
  try {
    const result = await startRun({ scope: body?.scope, lookbackDays: body?.lookbackDays });
    return NextResponse.json(result);
  } catch (e) {
    return NextResponse.json(
      { status: "db_unavailable", error: String(e) },
      { status: 503 },
    );
  }
}

// ---------------------------------------------------------------------------
// DELETE — cancel a running run
// ---------------------------------------------------------------------------

export async function DELETE(request: Request) {
  const { searchParams } = new URL(request.url);
  const runId = searchParams.get("run_id");
  if (!runId) {
    return NextResponse.json({ error: "missing run_id" }, { status: 400 });
  }
  try {
    const result = await cancelRun(runId);
    return NextResponse.json(result);
  } catch (e) {
    return NextResponse.json(
      { status: "db_unavailable", error: String(e) },
      { status: 503 },
    );
  }
}

// ---------------------------------------------------------------------------
// GET — the SSE stream
// ---------------------------------------------------------------------------

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const runId = searchParams.get("run_id");
  if (!runId) {
    return NextResponse.json({ error: "missing run_id" }, { status: 400 });
  }

  return sseStream<WatchStreamEvent>(
    (emit) => runSlice(runId, emit, { budgetMs: 45_000 }),
    { type: "done", completed: 0, found: 0, error: "stream_failed" },
  );
}
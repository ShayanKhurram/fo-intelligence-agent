// T50.3 — the Intent Watcher research sweep as a standalone Node service for Render.
//
// Why this exists: the Vercel route (app/api/watch/run/route.ts) is capped by Vercel's
// function timeout, so it self-limits to a 45-second slice and the browser reconnects to
// resume. A full sweep over ~480 organizations takes minutes. This host has no request
// ceiling, so a run finishes in one connection.
//
// It is a plain `node:http` server (no Express, no framework) that imports the SAME
// `startRun` / `cancelRun` / `runSlice` the Vercel adapter imports from
// `../lib/watch-run.ts`. There is exactly one copy of the sweep logic.
import http from "node:http";
import type { IncomingMessage, ServerResponse } from "node:http";
import { startRun, cancelRun, runSlice } from "../lib/watch-run.ts";
import type { WatchStreamEvent } from "../lib/watch-types.ts";

// Budget passed to runSlice. 6h by default — a full sweep finishes in one connection
// instead of being sliced into 45s Vercel requests. Override with WATCH_SLICE_BUDGET_MS.
const BUDGET_MS = Number(process.env.WATCH_SLICE_BUDGET_MS) || 21_600_000;

// CORS allowlist — NEVER `*`. A comma-separated list of exact origins read at startup.
// An origin not on the list gets no Access-Control-Allow-Origin header at all.
const ALLOWED_ORIGINS = new Set(
  (process.env.WATCH_CORS_ORIGINS || "")
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0),
);

const PORT = Number(process.env.PORT) || 3001;

// ---------------------------------------------------------------------------
// CORS
// ---------------------------------------------------------------------------

/** Returns the per-origin CORS headers for a request, or an empty object if the
 *  request's Origin is absent or not on the allowlist. Never sets `*` and never sets
 *  Access-Control-Allow-Credentials (the client sends no cookies). */
function corsHeaders(req: IncomingMessage): Record<string, string> {
  const origin = req.headers.origin;
  const h: Record<string, string> = {};
  if (typeof origin === "string" && origin.length > 0 && ALLOWED_ORIGINS.has(origin)) {
    h["Access-Control-Allow-Origin"] = origin;
    h["Vary"] = "Origin";
  }
  return h;
}

// ---------------------------------------------------------------------------
// Body reading
// ---------------------------------------------------------------------------

async function readJsonBody(req: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  for await (const c of req) {
    chunks.push(typeof c === "string" ? Buffer.from(c) : c);
  }
  const text = Buffer.concat(chunks).toString("utf8");
  if (text.length === 0) return {};
  try {
    return JSON.parse(text) as Record<string, unknown>;
  } catch {
    return {};
  }
}

// ---------------------------------------------------------------------------
// Small response helpers
// ---------------------------------------------------------------------------

function sendJson(
  res: ServerResponse,
  status: number,
  body: unknown,
  extra: Record<string, string> = {},
): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json",
    ...corsHeaders(res.req!),
    ...extra,
  });
  res.end(payload);
}

function notFound(res: ServerResponse): void {
  sendJson(res, 404, { error: "not_found" });
}

// ---------------------------------------------------------------------------
// SSE GET — the stream
// ---------------------------------------------------------------------------

function handleStream(req: IncomingMessage, res: ServerResponse, runId: string): void {
  let headersSent = false;
  let ended = false;
  let closed = false;
  let heartbeat: ReturnType<typeof setInterval> | null = null;

  const startStream = () => {
    headersSent = true;
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      "Connection": "keep-alive",
      "X-Accel-Buffering": "no",
      ...corsHeaders(req),
    });
    // Heartbeat: write an SSE comment every 15s. A comment (`: ping`) is ignored by the
    // client parser; it only keeps an idle proxy hop from dropping a legitimately quiet
    // stream between organizations.
    heartbeat = setInterval(() => {
      if (!closed) {
        try {
          res.write(": ping\n\n");
        } catch {
          // socket already gone — nothing to do
        }
      }
    }, 15_000);
  };

  // `emit` lazily starts the stream on the first event, so a runSlice that throws before
  // emitting anything can still be answered with a 500 JSON body (no headers sent yet).
  const emit = (e: WatchStreamEvent): void => {
    if (closed || ended) return;
    if (!headersSent) startStream();
    res.write(`event: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`);
  };

  // Client disconnect: stop emitting and clear the heartbeat. Do NOT cancel the run in
  // the database — its pending targets are meant to be resumed by the next connection,
  // exactly like the existing paused path on Vercel.
  req.on("close", () => {
    closed = true;
    if (heartbeat) {
      clearInterval(heartbeat);
      heartbeat = null;
    }
  });

  runSlice(runId, emit, { budgetMs: BUDGET_MS })
    .then(() => {
      if (ended || closed) return;
      // runSlice always emits at least one event, so headers are normally already sent.
      // If it somehow resolved without emitting, start an empty stream so end() is valid.
      if (!headersSent) startStream();
      ended = true;
      if (heartbeat) {
        clearInterval(heartbeat);
        heartbeat = null;
      }
      res.end();
    })
    .catch((e) => {
      console.error("watch runSlice failed", e);
      if (ended || closed) return;
      if (!headersSent) {
        // Threw before anything was written — a JSON error body is still possible.
        res.writeHead(500, {
          "Content-Type": "application/json",
          ...corsHeaders(req),
        });
        ended = true;
        res.end(JSON.stringify({ status: "stream_failed", error: String(e) }));
      } else {
        // Threw after the stream started — the only way left to tell the client is an
        // SSE event, the same fallback lib/sse.ts uses.
        try {
          res.write(
            `event: done\ndata: ${JSON.stringify({ type: "done", completed: 0, found: 0, error: "stream_failed" })}\n\n`,
          );
        } catch {
          // socket already gone — nothing to do
        }
        ended = true;
        if (heartbeat) {
          clearInterval(heartbeat);
          heartbeat = null;
        }
        res.end();
      }
    });
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
  const path = url.pathname;
  const method = req.method || "GET";

  // /health must NOT touch the database or read DATABASE_URL — it is Render's liveness
  // probe and must answer even if Postgres is briefly unreachable.
  if (path === "/health" && method === "GET") {
    sendJson(res, 200, { ok: true });
    return;
  }

  if (path === "/api/watch/run") {
    if (method === "OPTIONS") {
      // Preflight: CORS headers only, no body. The allow-Methods/Headers are added on top
      // of the per-origin allowlist (origin still must be on the list to get ACAO).
      res.writeHead(204, {
        ...corsHeaders(req),
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
      });
      res.end();
      return;
    }

    if (method === "POST") {
      try {
        const body = await readJsonBody(req);
        const result = await startRun({
          scope: body?.scope,
          lookbackDays: body?.lookbackDays,
        });
        sendJson(res, 200, result);
      } catch (e) {
        sendJson(res, 503, { status: "db_unavailable", error: String(e) });
      }
      return;
    }

    if (method === "GET") {
      const runId = url.searchParams.get("run_id");
      if (!runId) {
        sendJson(res, 400, { error: "missing run_id" });
        return;
      }
      handleStream(req, res, runId);
      return;
    }

    if (method === "DELETE") {
      const runId = url.searchParams.get("run_id");
      if (!runId) {
        sendJson(res, 400, { error: "missing run_id" });
        return;
      }
      try {
        const result = await cancelRun(runId);
        sendJson(res, 200, result);
      } catch (e) {
        sendJson(res, 503, { status: "db_unavailable", error: String(e) });
      }
      return;
    }

    // Any other method on this path (e.g. PUT) → 404.
    notFound(res);
    return;
  }

  notFound(res);
});

// No server-side timeouts. Node's default `requestTimeout` is 5 minutes — that would
// kill a long sweep mid-stream and silently reintroduce the very ceiling this service
// exists to remove. Zero disables both the whole-request and the headers-only timeouts.
server.requestTimeout = 0;
server.headersTimeout = 0;

server.listen(PORT, "0.0.0.0", () => {
  console.log(
    `fo-watch-research listening on 0.0.0.0:${PORT} (budgetMs=${BUDGET_MS})` +
      ` CORS allowlist=[${[...ALLOWED_ORIGINS].join(", ")}]`,
  );
});
// Small SSE plumbing helper. A typed emitter callback plus a ReadableStream builder —
// nothing framework-specific; just formats each event as the standard
// `event: <type>\ndata: <json>\n\n` wire shape Next.js's Response can stream directly.
import type { QueryStreamEvent } from "./types";

export type Emit = (event: QueryStreamEvent) => void;

function format(event: QueryStreamEvent): string {
  return `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;
}

/** `run` receives an `emit` function and drives the whole query pipeline, emitting
 * events as it goes. The stream closes automatically once `run` resolves (or after
 * emitting a best-effort error event if it throws — a thrown error this late means the
 * response has already started, so a JSON error body is no longer possible; an SSE
 * event is the only way left to tell the client something went wrong). */
export function sseResponse(run: (emit: Emit) => Promise<void>): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      const emit: Emit = (event) => {
        controller.enqueue(encoder.encode(format(event)));
      };
      try {
        await run(emit);
      } catch (e) {
        console.error("SSE stream failed mid-run", e);
        try {
          emit({ type: "done", records: [], relaxedFilters: [], error: true, finalAnswerFallback: "Couldn't complete that search — try again." });
        } catch {
          // controller may already be closed if the client disconnected — nothing to do
        }
      } finally {
        controller.close();
      }
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no", // disable nginx/proxy buffering so events flush immediately
    },
  });
}

/** Generic twin of `sseResponse` for event unions other than `QueryStreamEvent` — the
 * Intent Watcher's `WatchStreamEvent`. Same wire shape (`event: <type>\ndata: <json>\n\n`)
 * and flush behaviour. `errorEvent` is the best-effort final event emitted if `run`
 * throws after the stream has already started (a JSON error body is no longer possible
 * at that point, just as in `sseResponse`). */
export function sseStream<T extends { type: string }>(
  run: (emit: (event: T) => void) => Promise<void>,
  errorEvent?: T,
): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      const emit = (event: T) => {
        controller.enqueue(encoder.encode(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`));
      };
      try {
        await run(emit);
      } catch (e) {
        console.error("SSE stream failed mid-run", e);
        if (errorEvent !== undefined) {
          try { emit(errorEvent); } catch { /* controller may already be closed */ }
        }
      } finally {
        controller.close();
      }
    },
  });
  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}

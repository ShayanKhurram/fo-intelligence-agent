// T46.3 — the Intent Watcher run SSE contract. Imported by the route (producer) and,
// next round, by the client component (consumer), exactly as `lib/types.ts`'s
// `QueryStreamEvent` is today. `WatchSignal` is a TYPE-ONLY import from `watch.ts` so
// this module stays client-bundle-safe — the type is erased at compile time and never
// pulls `lib/db.ts` (server-only) into the client.
import type { WatchSignal } from "./watch.ts";

export type WatchMeaningUpdate = {
  dedupeKey: string;
  kind: string;
  meaning: string | null;
  confidence: string | null;
};

export type WatchStreamEvent =
  // Emitted once at stream open: the run's identity, total queued, completed-so-far, and
  // backend. `completed` is non-zero on a resumed slice.
  | { type: "run"; runId: string; queued: number; completed: number; backend: string }
  // Emitted right before a search starts, so the UI can show "scanning · <name>".
  | { type: "scanning"; recordId: string; entityName: string; index: number; total: number }
  // Two-phase reveal, phase 1: emitted the MOMENT searchActivity returns. Every signal
  // carries `meaning: null` and a provisional `kind` ("firm_news"); the `meaning` event
  // fills them in later. `skipped`/`error` are set when the org yielded nothing.
  | {
      type: "org";
      recordId: string;
      entityName: string;
      signals: WatchSignal[];
      skipped?: string;
      error?: string;
      index: number;
      total: number;
    }
  // Two-phase reveal, phase 2: the LLM's interpretation of one org's articles, keyed by
  // dedupeKey so the client can patch the matching signal from the `org` event.
  | { type: "meaning"; recordId: string; updates: WatchMeaningUpdate[] }
  // Measured progress. `msPerOrg`/`etaMs` are `null` until at least 3 targets have
  // completed — a made-up countdown is worse than none.
  | {
      type: "progress";
      completed: number;
      total: number;
      found: number;
      msPerOrg: number | null;
      etaMs: number | null;
    }
  // The 45s wall-clock budget ran out with targets still pending — the client reconnects
  // to the same run_id and the next slice continues. Slice boundaries are invisible to
  // the user.
  | { type: "paused"; remaining: number }
  // Terminal. `cancelled` when a DELETE stopped the run; `error: "credits_exhausted"`
  // when Serper's balance ran out (the run is failed, not retried per-org).
  | { type: "done"; completed: number; found: number; cancelled?: boolean; error?: string };
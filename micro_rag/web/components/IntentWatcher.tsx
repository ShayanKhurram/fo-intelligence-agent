"use client";

import { useCallback, useMemo, useReducer, useRef, useState } from "react";
import type { WatchBoard, WatchBoardOrg, WatchSignal } from "@/lib/watch";
import type { WatchStreamEvent } from "@/lib/watch-types";
import { KIND_ORDER, boardSummary, kindMeta } from "@/lib/watch-format";
import { OrgSignalCard } from "./OrgSignalCard";
import { RunControl, type RunPhase } from "./RunControl";
import { useThread } from "./ThreadProvider";
import { researchUrl } from "@/lib/research-origin";

// T46.4 — the Intent Watcher page body.
//
// The standing board is server-rendered and handed in as a prop (no loading flash, and
// `lib/db.ts` never reaches the client bundle). Everything below is the run: an SSE
// stream whose 45-second slice boundaries are invisible — on `paused` the client silently
// reconnects to the same run_id and the meter keeps moving.

const PAGE = 50;

type State = {
  phase: RunPhase;
  runId: string | null;
  completed: number;
  total: number;
  found: number;
  etaMs: number | null;
  scanning: string | null;
  foundOrgs: WatchBoardOrg[]; // newest first — what THIS run discovered
  error: string | null;
  cancelled: boolean;
  finishedOnce: boolean;
};

const INITIAL: State = {
  phase: "idle",
  runId: null,
  completed: 0,
  total: 0,
  found: 0,
  etaMs: null,
  scanning: null,
  foundOrgs: [],
  error: null,
  cancelled: false,
  finishedOnce: false,
};

type Action =
  | { type: "starting" }
  | { type: "started"; runId: string; queued: number; completed: number }
  | { type: "event"; event: WatchStreamEvent }
  | { type: "fail"; message: string }
  | { type: "stopped" };

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "starting":
      // A fresh run clears the previous run's band, but never the standing list.
      return { ...INITIAL, phase: "starting", foundOrgs: [], finishedOnce: state.finishedOnce };
    case "started":
      return {
        ...state,
        phase: "running",
        runId: action.runId,
        total: action.queued,
        completed: action.completed,
      };
    case "fail":
      return { ...state, phase: "finished", error: action.message, finishedOnce: true };
    case "stopped":
      return { ...state, phase: "finished", finishedOnce: true };
    case "event":
      break;
  }

  const e = action.event;
  switch (e.type) {
    case "run":
      return { ...state, phase: "running", total: e.queued, completed: e.completed };
    case "scanning":
      return { ...state, scanning: e.entityName };
    case "org": {
      // Orgs that yielded nothing are counted in progress, not shown as empty cards —
      // an empty card is noise, and with this corpus most orgs yield nothing.
      if (e.signals.length === 0) return state;
      const org: WatchBoardOrg = {
        record_id: e.recordId,
        entity_name: e.entityName,
        entity_type: "",
        hq_state: null,
        aum_usd: null,
        signals: e.signals,
        newestAt: e.signals.reduce<string | null>(
          (best, s) => (s.observed_at && (!best || s.observed_at > best) ? s.observed_at : best),
          null,
        ),
        baselineCount: 0,
        freshCount: e.signals.length,
      };
      // Newest first. An org already in the band (a resumed slice re-emitting it) is
      // replaced in place rather than duplicated.
      const rest = state.foundOrgs.filter((o) => o.record_id !== e.recordId);
      return { ...state, foundOrgs: [org, ...rest] };
    }
    case "meaning": {
      // Patch in place, matched on the signal's id (the route sets id = dedupeKey(url)).
      // Deliberately does NOT re-sort the band: a card must not jump under the cursor
      // when its interpretation lands a moment after the headline.
      const byKey = new Map(e.updates.map((u) => [u.dedupeKey, u]));
      return {
        ...state,
        foundOrgs: state.foundOrgs.map((o) =>
          o.record_id !== e.recordId
            ? o
            : {
                ...o,
                signals: o.signals.map((s): WatchSignal => {
                  const u = byKey.get(String(s.id));
                  return u ? { ...s, kind: u.kind, meaning: u.meaning, confidence: u.confidence } : s;
                }),
              },
        ),
      };
    }
    case "progress":
      return { ...state, completed: e.completed, total: e.total, found: e.found, etaMs: e.etaMs };
    case "paused":
      // Never surfaced. The caller reconnects and the meter keeps moving.
      return state;
    case "done":
      return {
        ...state,
        phase: "finished",
        completed: e.completed,
        found: e.found,
        cancelled: !!e.cancelled,
        scanning: null,
        finishedOnce: true,
        error:
          e.error === "credits_exhausted"
            ? "The search backend is out of credits — results below are what the sweep reached before it stopped."
            : e.error
              ? `The run stopped early (${e.error}).`
              : null,
      };
    default:
      return state;
  }
}

type KindFilter = "all" | (typeof KIND_ORDER)[number];

export function IntentWatcher({ board }: { board: WatchBoard }) {
  const [state, dispatch] = useReducer(reducer, INITIAL);
  const [scope, setScope] = useState(60);
  const [kindFilter, setKindFilter] = useState<KindFilter>("all");
  const [newOnly, setNewOnly] = useState(false);
  const [visible, setVisible] = useState(PAGE);
  // T47.6 — the evidence surface is owned by the shell now, so /watch gets the same three
  // responsive forms (sheet / drawer / pinned panel) that the thread does, instead of its
  // own private copy of the old right-hand drawer.
  const { openEvidence } = useThread();
  const abortRef = useRef<AbortController | null>(null);
  const stoppedRef = useRef(false);

  const orgs = board.orgs;

  const summary = useMemo(() => {
    const signalCount = orgs.reduce((n, o) => n + o.signals.length, 0);
    const freshest = orgs.reduce<string | null>(
      (best, o) => (o.newestAt && (!best || o.newestAt > best) ? o.newestAt : best),
      null,
    );
    return boardSummary(orgs.length, signalCount, freshest);
  }, [orgs]);

  // Counts per kind, computed once over the standing list — they label the filter pills
  // and decide which pills are disabled (a filter that can only produce an empty screen
  // is worse than no filter).
  const kindCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const o of orgs) for (const s of o.signals) counts.set(s.kind, (counts.get(s.kind) ?? 0) + 1);
    return counts;
  }, [orgs]);

  const filtered = useMemo(() => {
    const source = newOnly ? state.foundOrgs : orgs;
    if (kindFilter === "all") return source;
    return source
      .map((o) => ({ ...o, signals: o.signals.filter((s) => s.kind === kindFilter) }))
      .filter((o) => o.signals.length > 0);
  }, [orgs, state.foundOrgs, kindFilter, newOnly]);

  /** Reads one SSE slice. Resolves "paused" when the server's 45s budget expired with
   * work remaining, "done" when terminal, "aborted" when the user pressed Stop. */
  const readSlice = useCallback(async (runId: string, signal: AbortSignal): Promise<"paused" | "done"> => {
    const res = await fetch(researchUrl(`/api/watch/run?run_id=${encodeURIComponent(runId)}`), { signal });
    if (!res.body) throw new Error("no response body");
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let outcome: "paused" | "done" = "done";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() ?? "";
      for (const block of blocks) {
        const line = block.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        let event: WatchStreamEvent;
        try {
          event = JSON.parse(line.slice(5).trim());
        } catch {
          continue; // a malformed chunk — skip rather than kill the stream
        }
        if (event.type === "paused") outcome = "paused";
        dispatch({ type: "event", event });
      }
    }
    return outcome;
  }, []);

  const start = useCallback(async () => {
    stoppedRef.current = false;
    dispatch({ type: "starting" });
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const res = await fetch(researchUrl("/api/watch/run"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope }),
      });
      const data = await res.json();
      if (!data.run_id) {
        dispatch({ type: "fail", message: data.empty ? "Every organization has been researched already." : "Could not start a run." });
        return;
      }
      dispatch({ type: "started", runId: data.run_id, queued: data.queued, completed: 0 });

      // Slice loop. `paused` is a server-side wall-clock boundary, not a user-visible
      // state: reconnect immediately and keep the same progress. Only three CONSECUTIVE
      // reconnect failures are worth telling the user about.
      let failures = 0;
      while (!stoppedRef.current) {
        let outcome: "paused" | "done";
        try {
          outcome = await readSlice(data.run_id, controller.signal);
          failures = 0;
        } catch {
          if (controller.signal.aborted || stoppedRef.current) return;
          failures++;
          if (failures >= 3) {
            dispatch({ type: "fail", message: "Lost contact with the run. Reload to see what it saved." });
            return;
          }
          await new Promise((r) => setTimeout(r, 800 * failures));
          continue;
        }
        if (outcome === "done") return;
      }
    } catch {
      if (!stoppedRef.current) dispatch({ type: "fail", message: "Could not start a run." });
    }
  }, [scope, readSlice]);

  const stop = useCallback(async () => {
    stoppedRef.current = true;
    const runId = state.runId;
    abortRef.current?.abort();
    dispatch({ type: "stopped" });
    if (runId) {
      await fetch(researchUrl(`/api/watch/run?run_id=${encodeURIComponent(runId)}`), { method: "DELETE" }).catch(() => {});
    }
  }, [state.runId]);

  const researched = state.finishedOnce ? state.completed : 0;
  const remaining = Math.max(0, orgs.length - researched);

  // T47.1 — the route no longer builds its own container or its own nav; AppShell owns
  // both. The run control stays with this page's heading because it acts on THIS page.
  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-6 sm:px-6">
      <header className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="display text-xl text-[var(--text-hi)]">Intent watcher</h1>
          <p className="mono mt-0.5 text-[11px] text-[var(--text-mid)]">{summary}</p>
          <p className="mt-2 max-w-2xl text-sm text-[var(--text-mid)]">
            Every organization in the corpus that shows signs of activity — what it did, and what that
            activity plausibly means. Run research to re-check the stalest organizations for anything new.
          </p>
        </div>
        <RunControl
          phase={state.phase}
          scope={scope}
          onScopeChange={setScope}
          onStart={start}
          onStop={stop}
          completed={state.completed}
          total={state.total}
          found={state.found}
          etaMs={state.etaMs}
          scanning={state.scanning}
          remainingUnresearched={remaining}
          orgCount={orgs.length}
          error={state.error}
        />
      </header>

        {/* Filters. Client-side over already-loaded data; never refetches. */}
        <div className="mb-5 flex flex-wrap items-center gap-1.5">
          {(["all", ...KIND_ORDER] as KindFilter[]).map((k) => {
            const count = k === "all" ? orgs.reduce((n, o) => n + o.signals.length, 0) : (kindCounts.get(k) ?? 0);
            const meta = k === "all" ? null : kindMeta(k);
            const disabled = count === 0;
            const activePill = kindFilter === k;
            return (
              <button
                key={k}
                onClick={() => {
                  setKindFilter(k);
                  setVisible(PAGE);
                }}
                disabled={disabled}
                className={`mono rounded-[var(--r-pill)] border px-2.5 py-1 text-[11px] transition-colors ${
                  activePill
                    ? "border-[var(--edge-lit)] bg-[var(--bg-raised)] text-[var(--text-hi)]"
                    : "border-[var(--edge)] text-[var(--text-mid)] hover:text-[var(--text-hi)]"
                } disabled:cursor-not-allowed disabled:opacity-30`}
                style={activePill && meta ? { color: meta.color } : undefined}
              >
                {meta ? `${meta.glyph} ${meta.label}` : "all"} <span className="text-[var(--text-low)]">{count}</span>
              </button>
            );
          })}
          <span className="flex-1" />
          <button
            onClick={() => {
              setNewOnly((v) => !v);
              setVisible(PAGE);
            }}
            disabled={state.foundOrgs.length === 0}
            aria-pressed={newOnly}
            className={`mono rounded-[var(--r-pill)] border px-2.5 py-1 text-[11px] ${
              newOnly
                ? "border-[var(--live)] text-[var(--live)]"
                : "border-[var(--edge)] text-[var(--text-mid)] hover:text-[var(--text-hi)]"
            } disabled:cursor-not-allowed disabled:opacity-30`}
          >
            New in this run {state.foundOrgs.length > 0 ? state.foundOrgs.length : ""}
          </button>
        </div>

        {/* Band 1 — what this run found. Grows as `org` events arrive. */}
        {!newOnly && (state.phase !== "idle" || state.foundOrgs.length > 0) && (
          <section className="mb-8">
            <h2
              aria-live="polite"
              className="mono mb-3 text-xs uppercase tracking-wide text-[var(--text-low)]"
            >
              Found in this run · {state.foundOrgs.length} organization
              {state.foundOrgs.length === 1 ? "" : "s"}
            </h2>
            {state.foundOrgs.length === 0 ? (
              <p className="rounded-[var(--r-md)] border border-[var(--edge)] bg-[var(--bg-raised)] px-4 py-3 text-sm text-[var(--text-mid)]">
                {state.phase === "finished"
                  ? state.cancelled
                    ? `Stopped after ${state.completed} organization${state.completed === 1 ? "" : "s"}. Nothing new was found before it stopped.`
                    : `Scanned ${state.completed} organization${state.completed === 1 ? "" : "s"}. Nothing new since the last sweep.`
                  : "Searching — discoveries appear here as they are found."}
              </p>
            ) : (
              <div className="space-y-3">
                {state.foundOrgs.map((o) => (
                  <div key={o.record_id} className="enter-pill">
                    <OrgSignalCard org={o} onOpenEvidence={openEvidence} />
                  </div>
                ))}
              </div>
            )}
          </section>
        )}

        {/* Band 2 — the standing list. Never reorders while a run is in flight. */}
        <section>
          <h2 className="mono mb-3 text-xs uppercase tracking-wide text-[var(--text-low)]">
            {newOnly ? "New in this run" : "All activity"} · {filtered.length} organization
            {filtered.length === 1 ? "" : "s"}
          </h2>
          {filtered.length === 0 ? (
            <p className="rounded-[var(--r-md)] border border-[var(--edge)] bg-[var(--bg-raised)] px-4 py-3 text-sm text-[var(--text-mid)]">
              {orgs.length === 0
                ? "No organization in the corpus carries an activity signal yet."
                : "Nothing matches that filter."}
            </p>
          ) : (
            <>
              <div className="space-y-3">
                {filtered.slice(0, visible).map((o) => (
                  <OrgSignalCard key={o.record_id} org={o} onOpenEvidence={openEvidence} />
                ))}
              </div>
              {/* 478 cards in one render janks the page; no virtualization library is
                  worth a new dependency for a list this shape. */}
              {filtered.length > visible && (
                <button
                  onClick={() => setVisible((v) => v + PAGE)}
                  className="mono mt-4 w-full rounded-[var(--r-md)] border border-[var(--edge)] py-2.5 text-xs text-[var(--text-mid)] hover:text-[var(--text-hi)]"
                >
                  Show more · {filtered.length - visible} remaining
                </button>
              )}
            </>
          )}
        </section>
    </div>
  );
}

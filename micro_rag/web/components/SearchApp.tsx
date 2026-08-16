"use client";

import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import type { ClaimVerifiedPayload, FilterChip, QueryStreamEvent, RecordRow } from "@/lib/types";
import type { ParsedFilters } from "@/lib/query-understanding";
import { removeFilterKey } from "@/lib/query-understanding";
import { StageStrip, type StageState } from "./StageStrip";
import { FilterChips } from "./FilterChips";
import { RecordCard, RecordCardSkeleton } from "./RecordCard";
import { ClaimPill } from "./ClaimPill";
import { InputBar } from "./InputBar";
import { EvidenceDrawer } from "./EvidenceDrawer";
import { EvidenceTab } from "./EvidenceTab";
import { RecordsTable } from "./RecordsTable";
import { ShareIcon } from "./icons";

// ui_plan.md §7.2 precedent: placeholders until verified against the live deployed
// system (build-order step 12). Kept from the prior design — same discipline applies.
const EXAMPLE_QUERIES = [
  "Single-family offices with confirmed AUM over $500M",
  "Who are the named principals across these records?",
  "How many records are multi-family offices?",
  "Family offices with a recent dated activity signal",
];

type AnswerSegment = { text: string; kind: "claim" | "neutral"; claim?: ClaimVerifiedPayload };

type Tab = "answer" | "records" | "evidence";

type State = {
  submittedQuery: string | null;
  activeFilters: ParsedFilters;
  filterChips: FilterChip[];
  stages: StageState[];
  stagesCollapsed: boolean;
  records: RecordRow[];
  recordsLoading: boolean;
  candidateCount: number | null;
  segments: AnswerSegment[];
  claims: ClaimVerifiedPayload[];
  loading: boolean;
  declined: boolean;
  discarded: boolean;
  error: boolean;
  fallbackMessage: string | null;
  relaxedFilters: string[];
  countResult: number | null;
};

const INITIAL_STATE: State = {
  submittedQuery: null,
  activeFilters: {},
  filterChips: [],
  stages: [],
  stagesCollapsed: false,
  records: [],
  recordsLoading: false,
  candidateCount: null,
  segments: [],
  claims: [],
  loading: false,
  declined: false,
  discarded: false,
  error: false,
  fallbackMessage: null,
  relaxedFilters: [],
  countResult: null,
};

type Action =
  | { type: "submit"; query: string; filters: ParsedFilters }
  | { type: "event"; event: QueryStreamEvent }
  | { type: "toggle_stages" };

function upsertStage(stages: StageState[], next: StageState): StageState[] {
  const idx = stages.findIndex((s) => s.id === next.id);
  if (idx === -1) return [...stages, next];
  const copy = [...stages];
  copy[idx] = next;
  return copy;
}

function reducer(state: State, action: Action): State {
  if (action.type === "submit") {
    return {
      ...INITIAL_STATE,
      submittedQuery: action.query,
      activeFilters: action.filters,
      loading: true,
      recordsLoading: true,
    };
  }
  if (action.type === "toggle_stages") {
    return { ...state, stagesCollapsed: !state.stagesCollapsed };
  }

  const e = action.event;
  switch (e.type) {
    case "stage":
      return { ...state, stages: upsertStage(state.stages, { id: e.id, label: e.label, status: e.status, detail: e.detail }) };
    case "filters":
      return { ...state, filterChips: e.filters, activeFilters: e.parsedFilters };
    case "records":
      return { ...state, records: e.records, recordsLoading: false, candidateCount: e.candidateCount };
    case "token":
      return { ...state, segments: [...state.segments, { text: e.text, kind: e.kind }] };
    case "claim_verified": {
      const segments = [...state.segments];
      // Attach the claim to the most recent claim-kind segment that doesn't have one yet
      // (the `token` event for this sentence was always emitted immediately before
      // `claim_verified` — see route.ts's finalizeUpTo).
      for (let i = segments.length - 1; i >= 0; i--) {
        if (segments[i].kind === "claim" && !segments[i].claim) {
          segments[i] = { ...segments[i], claim: e.claim };
          break;
        }
      }
      return { ...state, segments, claims: [...state.claims, e.claim] };
    }
    case "done":
      return {
        ...state,
        loading: false,
        recordsLoading: false,
        declined: !!e.declined,
        discarded: !!e.discarded,
        error: !!e.error,
        fallbackMessage: e.finalAnswerFallback ?? null,
        relaxedFilters: e.relaxedFilters,
        countResult: e.count ?? null,
        stagesCollapsed: true,
      };
    default:
      return state;
  }
}

export function SearchApp() {
  const [state, dispatch] = useReducer(reducer, INITIAL_STATE);
  const [query, setQuery] = useState("");
  const [activeTab, setActiveTab] = useState<Tab>("answer");
  const [evidence, setEvidence] = useState<{ recordId: string; field?: string | null } | null>(null);
  const [railFocusIndex, setRailFocusIndex] = useState(-1);
  const inputRef = useRef<HTMLInputElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  const runQuery = useCallback(async (q: string, filters: ParsedFilters, overrideFilters?: ParsedFilters) => {
    dispatch({ type: "submit", query: q, filters });
    try {
      const res = await fetch("/api/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q, overrideFilters }),
      });
      if (!res.body) throw new Error("no response body");
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const blocks = buffer.split("\n\n");
        buffer = blocks.pop() ?? "";
        for (const block of blocks) {
          const dataLine = block.split("\n").find((l) => l.startsWith("data:"));
          if (!dataLine) continue;
          try {
            const event: QueryStreamEvent = JSON.parse(dataLine.slice(5).trim());
            dispatch({ type: "event", event });
          } catch {
            // a malformed chunk — skip rather than crash the whole render
          }
        }
      }
    } catch {
      dispatch({
        type: "event",
        event: { type: "done", records: [], relaxedFilters: [], error: true, finalAnswerFallback: "Couldn't complete that search. Try again." },
      });
    }
  }, []);

  function submit(q: string) {
    if (!q.trim()) return;
    setActiveTab("answer");
    setRailFocusIndex(-1);
    setQuery("");
    runQuery(q, {});
  }

  function removeFilter(key: string) {
    if (!state.submittedQuery) return;
    const next = removeFilterKey(state.activeFilters, key);
    runQuery(state.submittedQuery, next, next);
  }

  function openEvidence(recordId: string, field?: string | null) {
    returnFocusRef.current = document.activeElement as HTMLElement;
    setEvidence({ recordId, field });
  }

  const entityNames = new Map(state.records.map((r) => [r.record_id, r.entity_name]));

  // Keyboard paths — ui_plan.md §8: "/" focuses input, ↑↓ moves the rail, Enter opens a
  // record, Esc closes the drawer (handled inside EvidenceDrawer itself).
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      const target = e.target as HTMLElement;
      const typing = target.tagName === "INPUT" || target.tagName === "TEXTAREA";
      if (e.key === "/" && !typing) {
        e.preventDefault();
        inputRef.current?.focus();
      } else if (e.key === "ArrowDown" && !typing && state.records.length > 0) {
        e.preventDefault();
        setRailFocusIndex((i) => Math.min(i + 1, state.records.length - 1));
      } else if (e.key === "ArrowUp" && !typing && state.records.length > 0) {
        e.preventDefault();
        setRailFocusIndex((i) => Math.max(i - 1, 0));
      } else if (e.key === "Enter" && !typing && railFocusIndex >= 0 && state.records[railFocusIndex]) {
        openEvidence(state.records[railFocusIndex].record_id);
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [state.records, railFocusIndex]);

  async function share() {
    const answerText = state.segments.map((s) => s.text).join(" ") || state.fallbackMessage || "";
    const text = `${state.submittedQuery ?? ""}\n\n${answerText}`;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // clipboard permission denied — no-op, nothing else to fall back to here
    }
  }

  const stageSummary =
    state.stages.length > 0
      ? `${state.records.length} record${state.records.length === 1 ? "" : "s"} · ${state.claims.length} field${state.claims.length === 1 ? "" : "s"}`
      : null;

  return (
    <div className="mx-auto flex min-h-screen w-full max-w-5xl flex-col px-4 sm:px-6">
      <header className="sticky top-0 z-20 flex items-center justify-between border-b border-[var(--edge)] bg-[var(--bg-base)]/90 py-3 backdrop-blur">
        <div className="flex items-center gap-1">
          {(["answer", "records", "evidence"] as Tab[]).map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              aria-current={activeTab === tab}
              className={`mono rounded-[var(--r-sm)] px-3 py-1.5 text-xs uppercase tracking-wide transition-colors ${
                activeTab === tab ? "bg-[var(--bg-raised)] text-[var(--text-hi)]" : "text-[var(--text-low)] hover:text-[var(--text-mid)]"
              }`}
            >
              {tab}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          {/* T37 — the agent's run log, mirrored here from the machine that runs it. */}
          {/* T42.5 — the First Approach Plan route. */}
          <a
            href="/plan"
            className="mono rounded-[var(--r-sm)] border border-[var(--edge)] px-3 py-1.5 text-xs text-[var(--text-mid)] hover:text-[var(--text-hi)]"
          >
            Plan
          </a>
          <a
            href="/log"
            className="mono rounded-[var(--r-sm)] border border-[var(--edge)] px-3 py-1.5 text-xs text-[var(--text-mid)] hover:text-[var(--text-hi)]"
          >
            Log
          </a>
          <button
            onClick={share}
            disabled={!state.submittedQuery}
            className="mono flex items-center gap-1.5 rounded-[var(--r-sm)] border border-[var(--edge)] px-3 py-1.5 text-xs text-[var(--text-mid)] hover:text-[var(--text-hi)] disabled:opacity-30"
          >
            <ShareIcon /> Share
          </button>
        </div>
      </header>

      <main className="flex-1 py-6">
        {!state.submittedQuery && (
          <div className="mx-auto max-w-xl py-16 text-center">
            <h1 className="display mb-2 text-2xl">FO Intelligence</h1>
            <p className="mb-8 text-sm text-[var(--text-mid)]">
              Private-wealth intelligence assembled from public obligation filings (13F, 990-PF, Form 5500, Form D).
            </p>
            <div className="flex flex-wrap justify-center gap-2">
              {EXAMPLE_QUERIES.map((q) => (
                <button
                  key={q}
                  onClick={() => {
                    setQuery(q);
                    submit(q);
                  }}
                  className="rounded-[var(--r-pill)] border border-[var(--edge)] px-3 py-1.5 text-sm text-[var(--text-mid)] hover:text-[var(--text-hi)]"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}

        {state.submittedQuery && (
          <div className={`grid grid-cols-1 gap-6 ${activeTab === "answer" ? "lg:grid-cols-[62%_1fr]" : ""}`}>
            <div className="min-w-0">
              <div className="mb-4 flex justify-end">
                <span className="mono rounded-[var(--r-pill)] bg-[var(--bg-raised)] px-3 py-1 text-xs text-[var(--text-mid)]">
                  {state.submittedQuery}
                </span>
              </div>

              {activeTab === "answer" && (
                <>
                  <StageStrip
                    stages={state.stages}
                    collapsed={state.stagesCollapsed}
                    summary={stageSummary}
                    onToggle={() => dispatch({ type: "toggle_stages" })}
                  />
                  <FilterChips chips={state.filterChips} onRemove={removeFilter} />

                  {state.relaxedFilters.length > 0 && (
                    <div className="mb-4 flex flex-wrap gap-2">
                      {state.relaxedFilters.map((f) => (
                        <span key={f} className="status-partial mono rounded-[var(--r-pill)] border border-[var(--partial)]/30 px-3 py-1 text-xs">
                          relaxed: {f}
                        </span>
                      ))}
                    </div>
                  )}

                  <div aria-live="polite" className="leading-relaxed">
                    {state.discarded ? (
                      <p className="text-[var(--text-mid)]">{state.fallbackMessage}</p>
                    ) : (
                      <p>
                        {state.segments.map((seg, i) =>
                          seg.claim ? (
                            <span key={i}>
                              {seg.text} <ClaimPill claim={seg.claim} onOpen={openEvidence} />
                            </span>
                          ) : (
                            <span key={i}>{seg.text} </span>
                          )
                        )}
                        {state.loading && <span className="stream-cursor" aria-hidden="true" />}
                        {!state.loading && state.fallbackMessage && !state.discarded && state.segments.length === 0 && (
                          <span className={state.error || state.declined ? "text-[var(--text-mid)]" : "display text-lg"}>
                            {state.fallbackMessage}
                          </span>
                        )}
                      </p>
                    )}
                  </div>

                  {state.records.length > 0 && (
                    <div className="mt-6 space-y-3">
                      {state.records.map((r) => (
                        <RecordCard key={r.record_id} record={r} variant="main" onOpen={openEvidence} />
                      ))}
                    </div>
                  )}
                </>
              )}

              {activeTab === "records" && <RecordsTable records={state.records} onOpen={openEvidence} />}
              {activeTab === "evidence" && <EvidenceTab claims={state.claims} entityNames={entityNames} />}
            </div>

            {activeTab === "answer" && (
              <aside className="glass h-fit p-4 lg:sticky lg:top-16" aria-label="Retrieved records">
                <h2 className="mono mb-3 text-xs uppercase tracking-wide text-[var(--text-low)]">
                  Records {state.candidateCount != null && <span className="text-[var(--text-low)]">· {state.candidateCount} candidates</span>}
                </h2>
                <div className="space-y-2">
                  {state.recordsLoading &&
                    Array.from({ length: 3 }).map((_, i) => <RecordCardSkeleton key={i} variant="rail" />)}
                  {!state.recordsLoading &&
                    state.records.map((r, i) => (
                      <RecordCard key={r.record_id} record={r} variant="rail" onOpen={openEvidence} focused={i === railFocusIndex} />
                    ))}
                  {!state.recordsLoading && state.records.length === 0 && (
                    <p className="text-xs text-[var(--text-low)]">No records yet.</p>
                  )}
                </div>
              </aside>
            )}
          </div>
        )}
      </main>

      <InputBar
        ref={inputRef}
        value={query}
        onChange={setQuery}
        onSubmit={() => submit(query)}
        loading={state.loading}
        hasFilters={state.filterChips.length > 0}
      />

      {evidence && (
        <EvidenceDrawer
          recordId={evidence.recordId}
          focusField={evidence.field}
          onClose={() => setEvidence(null)}
          returnFocusRef={returnFocusRef}
        />
      )}
    </div>
  );
}

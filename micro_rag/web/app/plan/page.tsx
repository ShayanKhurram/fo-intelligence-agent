"use client";

import { useCallback, useReducer, useRef, useState } from "react";
import type {
  ClaimVerifiedPayload,
  QueryStreamEvent,
} from "@/lib/types";
import type { RankedCandidate } from "@/lib/plan-rank";
import type { Excluded } from "@/lib/plan-retrieval";
import { StageStrip, type StageState } from "@/components/StageStrip";
import { ClaimPill } from "@/components/ClaimPill";
import { EvidenceDrawer } from "@/components/EvidenceDrawer";
import { InputBar } from "@/components/InputBar";
import { ChevronIcon, ShareIcon } from "@/components/icons";

// T42.5 — the First Approach Plan UI. Mirrors components/SearchApp.tsx's SSE
// consumption pattern (same fetch→reader→split("\n\n")→dispatch loop, same Tailwind
// CSS-variable tokens, same `mono` class conventions) so a reader of one can read the
// other. Does NOT re-style anything that already exists.
//
// Three things render, in this order:
//   1. the ranked table — straight from the `plan` event, never re-ordered client-side
//      (the server's order IS the product; a model that cannot invent the order cannot
//      flatter the list);
//   2. per-office cards — the generated approach note for each shortlisted office, its
//      claims rendered through ClaimPill, clicking one opens EvidenceDrawer exactly as
//      a search answer's claim does; each card also shows that office's `why` and `gaps`;
//   3. the excluded appendix — collapsed by default, expanding to the full list with
//      reasons, labelled with the count. The honesty half of the deliverable.
// T42.6's truncation notice surfaces when the sweep had to cap the ranked set.

const EXAMPLE_PLANS = [
  "I'm raising a $12M Series A for a US industrial-decarbonization company — which family offices should I approach first?",
  "Raising a $5M seed round for a climate company — top 10 family offices to approach",
  "which MFOs in California should I approach first for a $20M growth round?",
];

type State = {
  submittedQuery: string | null;
  stages: StageState[];
  stagesCollapsed: boolean;
  rows: RankedCandidate[];
  excluded: Excluded[];
  candidateCount: number | null;
  sweptTotal: number | null;
  sweptConsidered: number | null;
  truncated: boolean;
  // The approach note streams as `token` (display text, claim or neutral) and
  // `claim_verified` (full payload, claim only). Both are stored in arrival order as
  // NoteItems so the render reproduces EXACTLY what Gate 2 kept — including the
  // `neutral` sentences ("No email has been confirmed for this office") that the gate
  // deliberately retains and excludes from the strip threshold. Dropping those at
  // render time would let the UI read more confident than the text the model produced,
  // which is exactly the failure mode this project's gates exist to prevent.
  noteItems: NoteItem[];
  // The office a neutral sentence attaches to: the recordId of the most recent claim
  // that arrived before it. `null` means it arrived before any claim — a lead paragraph.
  lastClaimRecordId: string | null;
  loading: boolean;
  declined: boolean;
  error: boolean;
  fallbackMessage: string | null;
  relaxedFilters: string[];
};

// A `claim` carries its full payload (sentence + recordId) from `claim_verified`. A
// `neutral` carries only display text from a `token` event; its recordId is whatever
// office was current when it arrived (or null, for preamble before the first claim).
type NoteItem =
  | { kind: "claim"; recordId: string; claim: ClaimVerifiedPayload }
  | { kind: "neutral"; recordId: string | null; text: string };

const INITIAL_STATE: State = {
  submittedQuery: null,
  stages: [],
  stagesCollapsed: false,
  rows: [],
  excluded: [],
  candidateCount: null,
  sweptTotal: null,
  sweptConsidered: null,
  truncated: false,
  noteItems: [],
  lastClaimRecordId: null,
  loading: false,
  declined: false,
  error: false,
  fallbackMessage: null,
  relaxedFilters: [],
};

type Action =
  | { type: "submit"; query: string }
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
    return { ...INITIAL_STATE, submittedQuery: action.query, loading: true };
  }
  if (action.type === "toggle_stages") {
    return { ...state, stagesCollapsed: !state.stagesCollapsed };
  }

  const e = action.event;
  switch (e.type) {
    case "stage":
      return { ...state, stages: upsertStage(state.stages, { id: e.id, label: e.label, status: e.status, detail: e.detail }) };
    case "filters":
      // parsedFilters arrives but the plan UI doesn't render filter chips — the ask is
      // prose, not a filter puzzle. No state change needed beyond ignoring it.
      return state;
    case "plan":
      return {
        ...state,
        rows: e.rows,
        excluded: e.excluded,
        candidateCount: e.candidateCount,
        sweptTotal: e.sweptTotal,
        sweptConsidered: e.sweptConsidered,
        truncated: e.truncated,
      };
    case "token":
      // `neutral` tokens MUST be kept — Gate 2 deliberately retains an explicit "we
      // don't know this" and excludes it from the strip threshold, so dropping it at
      // render would let the UI read more confident than the model's text. Attach a
      // neutral sentence to the office of the most recent claim that preceded it
      // (lastClaimRecordId), or to the lead paragraph if no claim has arrived yet.
      // `claim` tokens are NOT stored here: the claim's full payload (sentence +
      // recordId) arrives in the immediately-following `claim_verified` event, which
      // is where the item is built — storing both would double-render the sentence.
      if (e.kind === "neutral") {
        const item: NoteItem = { kind: "neutral", recordId: state.lastClaimRecordId, text: e.text };
        return { ...state, noteItems: [...state.noteItems, item] };
      }
      return state;
    case "claim_verified": {
      const item: NoteItem = { kind: "claim", recordId: e.claim.recordId, claim: e.claim };
      return { ...state, noteItems: [...state.noteItems, item], lastClaimRecordId: e.claim.recordId };
    }
    case "done":
      return {
        ...state,
        loading: false,
        declined: !!e.declined,
        error: !!e.error,
        fallbackMessage: e.finalAnswerFallback ?? null,
        relaxedFilters: e.relaxedFilters,
        stagesCollapsed: true,
      };
    default:
      return state;
  }
}

function fmtScore(n: number): string {
  return n.toFixed(2);
}

export default function PlanPage() {
  const [state, dispatch] = useReducer(reducer, INITIAL_STATE);
  const [query, setQuery] = useState("");
  const [excludedOpen, setExcludedOpen] = useState(false);
  const [evidence, setEvidence] = useState<{ recordId: string; field?: string | null } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  const runPlan = useCallback(async (q: string) => {
    dispatch({ type: "submit", query: q });
    try {
      const res = await fetch("/api/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q }),
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
        event: { type: "done", records: [], relaxedFilters: [], error: true, finalAnswerFallback: "Couldn't complete that plan. Try again." },
      });
    }
  }, []);

  function submit(q: string) {
    if (!q.trim()) return;
    setExcludedOpen(false);
    setQuery("");
    runPlan(q);
  }

  function openEvidence(recordId: string, field?: string | null) {
    returnFocusRef.current = document.activeElement as HTMLElement;
    setEvidence({ recordId, field });
  }

  // Split the ordered note into the lead paragraph (neutral sentences that arrived
  // before any claim — no office to attach to) and per-office groups (everything else,
  // in arrival order, so a card's claims and its "not confirmed" hedge interleave the
  // way the model wrote them).
  const leadNotes: string[] = [];
  const notesByRecord = new Map<string, NoteItem[]>();
  for (const item of state.noteItems) {
    if (item.recordId === null) {
      if (item.kind === "neutral") leadNotes.push(item.text);
      continue;
    }
    const list = notesByRecord.get(item.recordId) ?? [];
    list.push(item);
    notesByRecord.set(item.recordId, list);
  }
  const claimCount = state.noteItems.filter((n) => n.kind === "claim").length;

  async function share() {
    const lines = state.rows.map(
      (r, i) => `${i + 1}. ${r.entity_name} — score ${fmtScore(r.score)} (fit ${fmtScore(r.scores.fit)}, reach ${fmtScore(r.scores.reach)}, recency ${fmtScore(r.scores.recency)}, trust ${fmtScore(r.scores.trust)})`
    );
    const text = `${state.submittedQuery ?? ""}\n\n${lines.join("\n")}`;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // clipboard permission denied — no-op
    }
  }

  const stageSummary =
    state.rows.length > 0
      ? `${state.rows.length} office${state.rows.length === 1 ? "" : "s"} ranked · ${claimCount} cited field${claimCount === 1 ? "" : "s"}`
      : null;

  return (
    <div className="mx-auto flex min-h-screen w-full max-w-5xl flex-col px-4 sm:px-6">
      <header className="sticky top-0 z-20 flex items-center justify-between border-b border-[var(--edge)] bg-[var(--bg-base)]/90 py-3 backdrop-blur">
        <div>
          <p className="mono text-[10px] uppercase tracking-[0.2em] text-[var(--text-low)]">
            FO Intelligence Agent
          </p>
          <h1 className="display text-lg font-semibold text-[var(--text-hi)]">First Approach Plan</h1>
        </div>
        <div className="flex items-center gap-2">
          <a
            href="/"
            className="mono rounded-[var(--r-sm)] border border-[var(--edge)] px-3 py-1.5 text-xs text-[var(--text-mid)] hover:text-[var(--text-hi)]"
          >
            Search
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
          <div className="mx-auto max-w-2xl py-16 text-center">
            <p className="mb-3 text-sm text-[var(--text-mid)]">
              Tell me about a raise and which family offices to approach. The plan ranks the
              approachable offices by mandate fit, reach, recency, and trust — and names the ones it
              excluded, and why.
            </p>
            <div className="flex flex-col flex-wrap items-stretch justify-center gap-2">
              {EXAMPLE_PLANS.map((q) => (
                <button
                  key={q}
                  onClick={() => {
                    setQuery(q);
                    submit(q);
                  }}
                  className="rounded-[var(--r-pill)] border border-[var(--edge)] px-3 py-2 text-left text-sm text-[var(--text-mid)] hover:text-[var(--text-hi)]"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}

        {state.submittedQuery && (
          <div className="space-y-6">
            <div className="flex justify-end">
              <span className="mono rounded-[var(--r-pill)] bg-[var(--bg-raised)] px-3 py-1 text-xs text-[var(--text-mid)]">
                {state.submittedQuery}
              </span>
            </div>

            <StageStrip
              stages={state.stages}
              collapsed={state.stagesCollapsed}
              summary={stageSummary}
              onToggle={() => dispatch({ type: "toggle_stages" })}
            />

            {state.relaxedFilters.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {state.relaxedFilters.map((f) => (
                  <span key={f} className="status-partial mono rounded-[var(--r-pill)] border border-[var(--partial)]/30 px-3 py-1 text-xs">
                    relaxed: {f}
                  </span>
                ))}
              </div>
            )}

            {/* T42.6 — the sweep silently truncated and never said so was the defect; now
                it says so. A plan that ignored most of the corpus is the kind of quiet
                dishonesty the gates exist to prevent. */}
            {state.truncated && state.sweptTotal != null && state.sweptConsidered != null && (
              <div className="rounded-[var(--r-md)] border border-[var(--partial)]/40 bg-[var(--bg-glass)] px-4 py-3 text-sm text-[var(--text-mid)]">
                Ranked {state.sweptConsidered.toLocaleString()} of {state.sweptTotal.toLocaleString()} matching records —
                the sweep capped at the most the ranker could compare, so the rest were not considered.
              </div>
            )}

            {/* Decline / error path — a non-plan ask or an impossible filter set declines
                rather than widening into an unrelated list. */}
            {(state.declined || state.error) && state.fallbackMessage && state.rows.length === 0 && (
              <p className="text-[var(--text-mid)]">{state.fallbackMessage}</p>
            )}

            {/* Neutral preamble — sentences that arrived before the first claim, with no
                office to attach to. Rendered as a lead paragraph, not dropped. */}
            {leadNotes.length > 0 && (
              <p className="leading-relaxed text-[var(--text-mid)]">
                {leadNotes.join(" ")}
              </p>
            )}

            {/* 1. The ranked table — straight from the `plan` event, never re-ordered. */}
            {state.rows.length > 0 && (
              <div className="overflow-x-auto rounded-[var(--r-md)] border border-[var(--edge)] bg-[var(--bg-glass)]">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="mono border-b border-[var(--edge)] text-left text-[10px] uppercase tracking-wider text-[var(--text-low)]">
                      <th className="px-3 py-2 font-normal">#</th>
                      <th className="px-3 py-2 font-normal">Office</th>
                      <th className="px-3 py-2 text-right font-normal">Score</th>
                      <th className="px-3 py-2 text-right font-normal">Fit</th>
                      <th className="px-3 py-2 text-right font-normal">Reach</th>
                      <th className="px-3 py-2 text-right font-normal">Recency</th>
                      <th className="px-3 py-2 text-right font-normal">Trust</th>
                    </tr>
                  </thead>
                  <tbody>
                    {state.rows.map((r, i) => (
                      <tr key={r.record_id} className="border-b border-[var(--edge-soft)] last:border-0">
                        <td className="mono px-3 py-2 text-[var(--text-low)]">{i + 1}</td>
                        <td className="px-3 py-2 text-[var(--text-hi)]">{r.entity_name}</td>
                        <td className="mono px-3 py-2 text-right tabular-nums text-[var(--text-hi)]">{fmtScore(r.score)}</td>
                        <td className="mono px-3 py-2 text-right tabular-nums text-[var(--text-mid)]">{fmtScore(r.scores.fit)}</td>
                        <td className="mono px-3 py-2 text-right tabular-nums text-[var(--text-mid)]">{fmtScore(r.scores.reach)}</td>
                        <td className="mono px-3 py-2 text-right tabular-nums text-[var(--text-mid)]">{fmtScore(r.scores.recency)}</td>
                        <td className="mono px-3 py-2 text-right tabular-nums text-[var(--text-mid)]">{fmtScore(r.scores.trust)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* 2. Per-office cards — the approach note (claims via ClaimPill, click →
                EvidenceDrawer), plus that office's why lines and gaps. A neutral
                "not confirmed" sentence renders in the same card, unpilled and muted, so
                nothing Gate 2 kept is missing from the render. */}
            {state.rows.length > 0 && (
              <div className="space-y-3">
                {state.rows.map((r, i) => {
                  const officeNotes = notesByRecord.get(r.record_id) ?? [];
                  const hasAny = officeNotes.length > 0;
                  return (
                    <div key={r.record_id} className="glass rounded-[var(--r-md)] p-4">
                      <div className="mb-2 flex items-baseline gap-3">
                        <span className="mono text-[var(--text-low)]">{i + 1}</span>
                        <h2 className="display text-base text-[var(--text-hi)]">{r.entity_name}</h2>
                        <span className="mono text-xs text-[var(--text-low)]">{r.record_id}</span>
                        <span className="flex-1" />
                        <span className="mono text-xs text-[var(--text-mid)]">score {fmtScore(r.score)}</span>
                      </div>

                      {/* The approach note — the office's own claims (with pills) and
                          neutral hedges (plain, no pill), in arrival order. */}
                      <div className="leading-relaxed">
                        {!hasAny && state.loading && (
                          <span className="text-[var(--text-low)]">drafting note…</span>
                        )}
                        {!hasAny && !state.loading && (
                          <span className="text-[var(--text-low)]">No grounded note for this office.</span>
                        )}
                        {officeNotes.map((item, j) =>
                          item.kind === "claim" ? (
                            <span key={j}>
                              {item.claim.sentence} <ClaimPill claim={item.claim} onOpen={openEvidence} />{" "}
                            </span>
                          ) : (
                            // A neutral sentence carries no citation — that is the point.
                            // Muted so it reads as an honest hedge, visibly distinct from a
                            // cited claim (which carries a pill).
                            <span key={j} className="text-[var(--text-mid)]">
                              {item.text}{" "}
                            </span>
                          )
                        )}
                      </div>

                      {/* why: the reasons that produced the score, in plan-rank.ts's voice. */}
                      {r.why.length > 0 && (
                        <ul className="mono mt-3 list-disc pl-4 text-[11px] text-[var(--text-low)]">
                          {r.why.map((w, j) => (
                            <li key={j}>{w}</li>
                          ))}
                        </ul>
                      )}

                      {/* gaps: the fields that are missing — the honesty half of each card. */}
                      {r.gaps.length > 0 && (
                        <div className="mt-2 flex flex-wrap gap-1.5">
                          {r.gaps.map((g, j) => (
                            <span
                              key={j}
                              className="mono rounded-[var(--r-pill)] border border-[var(--edge)] px-2 py-0.5 text-[10px] text-[var(--text-low)]"
                            >
                              gap: {g}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            {/* 3. The excluded appendix — collapsed by default, labelled with the count.
                The honesty half of the deliverable, not hidden behind anything else. */}
            {state.excluded.length > 0 && (
              <div className="rounded-[var(--r-md)] border border-[var(--edge)] bg-[var(--bg-glass)]">
                <button
                  onClick={() => setExcludedOpen((v) => !v)}
                  className="flex w-full items-center justify-between px-4 py-3 text-left text-sm text-[var(--text-mid)] hover:text-[var(--text-hi)]"
                  aria-expanded={excludedOpen}
                >
                  <span>
                    <span className="mono text-[var(--text-hi)]">{state.excluded.length}</span> office{state.excluded.length === 1 ? "" : "s"} excluded
                  </span>
                  <ChevronIcon open={excludedOpen} />
                </button>
                {excludedOpen && (
                  <ul className="border-t border-[var(--edge)] px-4 py-2">
                    {state.excluded.map((x) => (
                      <li key={x.record_id} className="border-b border-[var(--edge-soft)] py-2 last:border-0">
                        <div className="flex flex-wrap items-baseline gap-2">
                          <span className="text-[var(--text-hi)]">{x.entity_name}</span>
                          <span className="mono text-[11px] text-[var(--text-low)]">{x.record_id}</span>
                        </div>
                        <p className="mono mt-0.5 text-[11px] text-[var(--text-low)]">{x.reason}</p>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
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
        hasFilters={false}
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
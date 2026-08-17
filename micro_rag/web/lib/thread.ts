import type { ClaimVerifiedPayload, FilterChip, QueryStreamEvent, RecordRow, StageId, StageStatus } from "./types";
import type { RankedCandidate } from "./plan-rank";
import type { Excluded } from "./plan-retrieval";
import type { ParsedFilters } from "./query-understanding";

// T47.2 — the thread.
//
// The whole point of this module is one property the previous reducer did not have:
// submitting a question APPENDS a turn and never touches the turns before it. The old
// reducer returned `...INITIAL_STATE` on submit (SearchApp.tsx:97-105 at 19c372c), so the
// second question destroyed the first answer, its records and its evidence, and two
// results could never be compared.
//
// Kept pure and free of React so the append-never-destroys property is unit-testable —
// see lib/thread.test.ts.
//
// NOTE ON WHAT THIS IS NOT: a thread is not a conversation. `/api/query` is stateless, so
// nothing here carries context between turns, and the UI is deliberately worded as a log
// of independent questions ("Ask a new question", no follow-up suggestion chips). Making
// it look conversational before T47.9's server-side query rewriting exists would promise
// memory the backend does not have.

export type StageState = { id: StageId; label: string; status: StageStatus; detail?: string };

export type AnswerSegment = { text: string; kind: "claim" | "neutral"; claim?: ClaimVerifiedPayload };

/** Which view of a single answer is showing. T47.5 — per turn, never app-global. */
export type TurnView = "answer" | "records" | "ranked" | "evidence";

/**
 * `stopped` is a first-class terminal state, distinct from `done` and `error`: T47.3
 * requires that aborting mid-stream keeps the partial answer on screen and labelled,
 * rather than discarding work the user already paid for.
 */
export type TurnStatus = "streaming" | "done" | "stopped" | "error";

export type Turn = {
  id: string;
  query: string;
  activeFilters: ParsedFilters;
  filterChips: FilterChip[];
  stages: StageState[];
  stagesCollapsed: boolean;
  records: RecordRow[];
  recordsLoading: boolean;
  candidateCount: number | null;
  segments: AnswerSegment[];
  claims: ClaimVerifiedPayload[];
  planRows: RankedCandidate[];
  excluded: Excluded[];
  sweptTotal: number | null;
  sweptConsidered: number | null;
  truncated: boolean;
  status: TurnStatus;
  declined: boolean;
  discarded: boolean;
  fallbackMessage: string | null;
  relaxedFilters: string[];
  countResult: number | null;
  view: TurnView;
};

export type ThreadState = { turns: Turn[] };

export const INITIAL_THREAD: ThreadState = { turns: [] };

export type ThreadAction =
  | { type: "submit"; id: string; query: string; filters: ParsedFilters }
  // A chip removal re-runs the SAME question with a loosened filter set. That is a
  // revision of one question, not a new one, so it resets that turn's result in place and
  // keeps its id and position rather than appending a near-duplicate turn.
  | { type: "rerun"; id: string; filters: ParsedFilters }
  | { type: "event"; id: string; event: QueryStreamEvent }
  | { type: "toggle_stages"; id: string }
  | { type: "set_view"; id: string; view: TurnView }
  | { type: "stopped"; id: string }
  | { type: "clear" };

function blankTurn(id: string, query: string, filters: ParsedFilters): Turn {
  return {
    id,
    query,
    activeFilters: filters,
    filterChips: [],
    stages: [],
    stagesCollapsed: false,
    records: [],
    recordsLoading: true,
    candidateCount: null,
    segments: [],
    claims: [],
    planRows: [],
    excluded: [],
    sweptTotal: null,
    sweptConsidered: null,
    truncated: false,
    status: "streaming",
    declined: false,
    discarded: false,
    fallbackMessage: null,
    relaxedFilters: [],
    countResult: null,
    view: "answer",
  };
}

function upsertStage(stages: StageState[], next: StageState): StageState[] {
  const idx = stages.findIndex((s) => s.id === next.id);
  if (idx === -1) return [...stages, next];
  const copy = [...stages];
  copy[idx] = next;
  return copy;
}

/** Applies one SSE event to one turn. Unchanged in behaviour from the pre-T47 reducer. */
function applyEvent(turn: Turn, e: QueryStreamEvent): Turn {
  switch (e.type) {
    case "stage":
      return { ...turn, stages: upsertStage(turn.stages, { id: e.id, label: e.label, status: e.status, detail: e.detail }) };
    case "filters":
      return { ...turn, filterChips: e.filters, activeFilters: e.parsedFilters };
    case "records":
      return { ...turn, records: e.records, recordsLoading: false, candidateCount: e.candidateCount };
    case "plan":
      // Rows are kept in the server's order — the order IS the product; re-sorting
      // client-side would let the UI flatter the ranking (settled at T44.4).
      return {
        ...turn,
        planRows: e.rows,
        excluded: e.excluded,
        candidateCount: e.candidateCount,
        sweptTotal: e.sweptTotal,
        sweptConsidered: e.sweptConsidered,
        truncated: e.truncated,
      };
    case "token":
      return { ...turn, segments: [...turn.segments, { text: e.text, kind: e.kind }] };
    case "claim_verified": {
      const segments = [...turn.segments];
      // Attach the claim to the most recent claim-kind segment that doesn't have one yet
      // (the `token` event for this sentence is always emitted immediately before
      // `claim_verified` — see route.ts's finalizeUpTo).
      for (let i = segments.length - 1; i >= 0; i--) {
        if (segments[i].kind === "claim" && !segments[i].claim) {
          segments[i] = { ...segments[i], claim: e.claim };
          break;
        }
      }
      return { ...turn, segments, claims: [...turn.claims, e.claim] };
    }
    case "done":
      return {
        ...turn,
        status: e.error ? "error" : "done",
        recordsLoading: false,
        declined: !!e.declined,
        discarded: !!e.discarded,
        fallbackMessage: e.finalAnswerFallback ?? null,
        relaxedFilters: e.relaxedFilters,
        countResult: e.count ?? null,
        stagesCollapsed: true,
      };
    default:
      return turn;
  }
}

function mapTurn(state: ThreadState, id: string, fn: (t: Turn) => Turn): ThreadState {
  const idx = state.turns.findIndex((t) => t.id === id);
  if (idx === -1) return state;
  const turns = [...state.turns];
  turns[idx] = fn(turns[idx]);
  return { turns };
}

export function threadReducer(state: ThreadState, action: ThreadAction): ThreadState {
  switch (action.type) {
    case "submit":
      // THE property this module exists for: append, never reset.
      return { turns: [...state.turns, blankTurn(action.id, action.query, action.filters)] };

    case "rerun":
      return mapTurn(state, action.id, (t) => blankTurn(t.id, t.query, action.filters));

    case "event":
      return mapTurn(state, action.id, (t) => applyEvent(t, action.event));

    case "toggle_stages":
      return mapTurn(state, action.id, (t) => ({ ...t, stagesCollapsed: !t.stagesCollapsed }));

    case "set_view":
      return mapTurn(state, action.id, (t) => ({ ...t, view: action.view }));

    case "stopped":
      // Abort keeps everything already streamed. Only a turn still in flight can stop —
      // a late abort must never demote a turn that already finished cleanly.
      return mapTurn(state, action.id, (t) =>
        t.status === "streaming" ? { ...t, status: "stopped", recordsLoading: false, stagesCollapsed: true } : t
      );

    case "clear":
      return INITIAL_THREAD;

    default:
      return state;
  }
}

/**
 * Counts for a turn's view control (T47.5). A view whose count is 0 is not offered —
 * the control must never advertise an empty tab.
 */
export function viewCounts(turn: Turn): { records: number; ranked: number; evidence: number; excluded: number } {
  return {
    records: turn.records.length,
    ranked: turn.planRows.length,
    evidence: turn.claims.length,
    excluded: turn.excluded.length,
  };
}

/** The plain text of a turn's answer, for the clipboard. */
export function answerText(turn: Turn): string {
  const streamed = turn.segments.map((s) => s.text).join(" ").trim();
  return streamed || turn.fallbackMessage || "";
}

/** Short label for the rail's history list. */
export function turnLabel(turn: Turn): string {
  const q = turn.query.trim();
  return q.length > 54 ? `${q.slice(0, 53)}…` : q;
}

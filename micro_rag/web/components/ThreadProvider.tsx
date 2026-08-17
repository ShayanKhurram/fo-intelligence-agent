"use client";

import { createContext, useCallback, useContext, useMemo, useReducer, useRef, useState } from "react";
import type { QueryStreamEvent } from "@/lib/types";
import type { ParsedFilters } from "@/lib/query-understanding";
import { INITIAL_THREAD, threadReducer, type Turn, type TurnView } from "@/lib/thread";

// T47.2 / T47.3 — the thread's owner.
//
// This sits above the router's children so all three routes share one shell (T47.1) and
// so the evidence surface can be rendered as a SIBLING of the thread column rather than a
// child of it. That sibling relationship is what makes the ≥1440 "pinned, pushes" layout
// possible: an overlay nested inside the column could only ever cover it.

export type EvidenceTarget = { recordId: string; field?: string | null };

type ThreadContextValue = {
  turns: Turn[];
  streamingId: string | null;
  submit: (query: string) => void;
  rerunWithFilters: (turnId: string, filters: ParsedFilters) => void;
  stop: () => void;
  setView: (turnId: string, view: TurnView) => void;
  toggleStages: (turnId: string) => void;
  clear: () => void;
  evidence: EvidenceTarget | null;
  openEvidence: (recordId: string, field?: string | null) => void;
  closeEvidence: () => void;
  returnFocusRef: React.RefObject<HTMLElement | null>;
};

const ThreadContext = createContext<ThreadContextValue | null>(null);

export function useThread(): ThreadContextValue {
  const ctx = useContext(ThreadContext);
  if (!ctx) throw new Error("useThread must be used inside <ThreadProvider>");
  return ctx;
}

export function ThreadProvider({ children }: { children: React.ReactNode }) {
  const [state, dispatch] = useReducer(threadReducer, INITIAL_THREAD);
  const [streamingId, setStreamingId] = useState<string | null>(null);
  const [evidence, setEvidence] = useState<EvidenceTarget | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  // Distinguishes "the user pressed stop" from "the stream genuinely failed". Both land in
  // the same catch, and showing an error on a deliberate abort would be a lie.
  const abortedRef = useRef(false);
  const seqRef = useRef(0);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  const run = useCallback(async (turnId: string, q: string, overrideFilters?: ParsedFilters) => {
    // Only one query is in flight at a time; starting a new one abandons the old.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    abortedRef.current = false;
    setStreamingId(turnId);

    try {
      const res = await fetch("/api/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q, overrideFilters }),
        signal: controller.signal,
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
            dispatch({ type: "event", id: turnId, event });
          } catch {
            // a malformed chunk — skip rather than crash the whole render
          }
        }
      }
    } catch {
      // A deliberate stop already dispatched `stopped` and kept the partial answer; only a
      // real failure gets the error treatment.
      if (!abortedRef.current) {
        dispatch({
          type: "event",
          id: turnId,
          event: {
            type: "done",
            records: [],
            relaxedFilters: [],
            error: true,
            finalAnswerFallback: "Couldn't complete that search. Try again.",
          },
        });
      }
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
        setStreamingId(null);
      }
    }
  }, []);

  const submit = useCallback(
    (query: string) => {
      const q = query.trim();
      if (!q) return;
      const id = `turn-${++seqRef.current}`;
      dispatch({ type: "submit", id, query: q, filters: {} });
      void run(id, q);
    },
    [run]
  );

  const rerunWithFilters = useCallback(
    (turnId: string, filters: ParsedFilters) => {
      const turn = state.turns.find((t) => t.id === turnId);
      if (!turn) return;
      dispatch({ type: "rerun", id: turnId, filters });
      void run(turnId, turn.query, filters);
    },
    [run, state.turns]
  );

  const stop = useCallback(() => {
    const id = streamingId;
    if (!id) return;
    abortedRef.current = true;
    abortRef.current?.abort();
    dispatch({ type: "stopped", id });
    setStreamingId(null);
  }, [streamingId]);

  const setView = useCallback((turnId: string, view: TurnView) => dispatch({ type: "set_view", id: turnId, view }), []);
  const toggleStages = useCallback((turnId: string) => dispatch({ type: "toggle_stages", id: turnId }), []);

  const clear = useCallback(() => {
    abortedRef.current = true;
    abortRef.current?.abort();
    abortRef.current = null;
    setStreamingId(null);
    setEvidence(null);
    dispatch({ type: "clear" });
  }, []);

  const openEvidence = useCallback((recordId: string, field?: string | null) => {
    returnFocusRef.current = document.activeElement as HTMLElement | null;
    setEvidence({ recordId, field });
  }, []);

  const closeEvidence = useCallback(() => setEvidence(null), []);

  const value = useMemo<ThreadContextValue>(
    () => ({
      turns: state.turns,
      streamingId,
      submit,
      rerunWithFilters,
      stop,
      setView,
      toggleStages,
      clear,
      evidence,
      openEvidence,
      closeEvidence,
      returnFocusRef,
    }),
    [state.turns, streamingId, submit, rerunWithFilters, stop, setView, toggleStages, clear, evidence, openEvidence, closeEvidence]
  );

  return <ThreadContext.Provider value={value}>{children}</ThreadContext.Provider>;
}

"use client";

import { useEffect, useState } from "react";
import { useThread } from "./ThreadProvider";
import { Turn } from "./Turn";
import { Composer } from "./Composer";

// T47.2 — the thread.
//
// This file used to be 519 lines holding the reducer, the SSE loop, the app header, three
// global tabs, the ranked table and the evidence drawer. All of that moved: the reducer to
// lib/thread.ts (where it is unit-tested), the stream to ThreadProvider, the header to
// AppShell, the views into Turn. What is left is the thread itself.
//
// It scrolls with the page rather than in an inner scroller: a nested scroll region is the
// standard way phone chat UIs end up with two scrollbars and a composer that jumps when
// the keyboard opens.

// T47.7 (finding 10) — these replace the four placeholders that shipped carrying a comment
// saying they were unverified. Each was run against the live corpus and returns a grounded
// answer with at least one verified claim; see PROJECT_LOG.md for the transcript.
const EXAMPLE_QUERIES = [
  "Which family offices have confirmed AUM over $500M?",
  "Who is Kapor Family Office?",
  "How many records are multi-family offices?",
  "Which family offices are based in Texas?",
];

export function SearchApp() {
  const { turns, submit, openEvidence } = useThread();
  // The keyboard focus ring is stored WITH the turn it belongs to, so a new turn resets it
  // by derivation rather than by a setState inside an effect (which would cascade a render
  // on every submit).
  const [focus, setFocus] = useState<{ turnId: string; index: number } | null>(null);

  const last = turns.length > 0 ? turns[turns.length - 1] : null;
  const focusIndex = last && focus?.turnId === last.id ? focus.index : -1;

  const lastId = last?.id;

  // A new turn scrolls itself into view. `scroll-padding` in globals.css keeps it clear of
  // the sticky top bar and the docked composer.
  useEffect(() => {
    if (!lastId) return;
    document.getElementById(`turn-${lastId}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [lastId]);

  // ui_plan.md §8's keyboard path, preserved and scoped: ↑/↓ moves the record list and
  // Enter opens a record. In a thread "the record list" is ambiguous, so it means the most
  // recent turn's — the one the user is looking at. ("/" is owned by Composer; Escape by
  // the evidence surface.)
  useEffect(() => {
    const records = last?.records ?? [];
    const turnId = last?.id;
    if (!turnId || records.length === 0) return;

    const id: string = turnId;

    function step(delta: number) {
      setFocus((f) => {
        const at = f !== null && f.turnId === id ? f.index : -1;
        const next = Math.min(Math.max(at + delta, 0), records.length - 1);
        const rec = records[next];
        if (rec) {
          document.getElementById(`record-card-${rec.record_id}`)?.scrollIntoView({ block: "nearest" });
        }
        return { turnId: id, index: next };
      });
    }

    function onKeyDown(e: KeyboardEvent) {
      const target = e.target as HTMLElement;
      const typing = target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable;
      if (typing) return;

      if (e.key === "ArrowDown") {
        e.preventDefault();
        step(1);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        step(-1);
      } else if (e.key === "Enter" && focusIndex >= 0 && records[focusIndex]) {
        openEvidence(records[focusIndex].record_id);
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [last, focusIndex, openEvidence]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="mx-auto w-full flex-1 px-4 sm:max-w-[42rem] sm:px-6 lg:max-w-[46rem]">
        {turns.length === 0 ? (
          <EmptyState onPick={submit} />
        ) : (
          turns.map((t) => <Turn key={t.id} turn={t} focusIndex={t.id === last?.id ? focusIndex : undefined} />)
        )}
      </div>

      <Composer />
    </div>
  );
}

function EmptyState({ onPick }: { onPick: (q: string) => void }) {
  return (
    // Centred in the space between the top bar and the docked composer, rather than
    // pinned to the top with a dead gap beneath it.
    <div className="flex min-h-[68dvh] flex-col items-center justify-center py-10 text-center">
      <h1 className="display mb-2 text-2xl sm:text-3xl">FO Intelligence</h1>
      <p className="mb-8 max-w-md text-sm leading-relaxed text-[var(--text-mid)]">
        Private-wealth intelligence assembled from public obligation filings — 13F, 990-PF, Form 5500 and Form D.
        Every claim in an answer opens the filing it rests on.
      </p>
      <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:flex-wrap sm:justify-center">
        {EXAMPLE_QUERIES.map((q) => (
          <button
            key={q}
            onClick={() => onPick(q)}
            className="rounded-[var(--r-pill)] border border-[var(--edge)] px-3.5 py-2 text-sm text-[var(--text-mid)] transition-colors hover:border-[var(--edge-lit)] hover:text-[var(--text-hi)] sm:py-1.5"
          >
            {q}
          </button>
        ))}
      </div>
    </div>
  );
}

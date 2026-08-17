"use client";

import { ChevronIcon } from "./icons";
import type { StageState } from "@/lib/thread";

export type { StageState };

// ui_plan.md §5 — "Show the real pipeline." These are the actual retrieval steps, so
// the loading state doubles as a demonstration of structured-plus-semantic retrieval,
// not spinner theatre.
export function StageStrip({
  stages,
  collapsed,
  summary,
  onToggle,
}: {
  stages: StageState[];
  collapsed: boolean;
  summary: string | null;
  onToggle: () => void;
}) {
  if (stages.length === 0) return null;

  if (collapsed && summary) {
    return (
      <button
        onClick={onToggle}
        className="glass mb-4 flex w-full items-center justify-between px-4 py-2 text-left text-sm text-[var(--text-mid)] hover:text-[var(--text-hi)]"
        aria-expanded={false}
      >
        <span className="status-dot status-dot--confirmed mono">{summary}</span>
        <ChevronIcon />
      </button>
    );
  }

  return (
    <div className="glass mb-4 px-4 py-3" aria-live="polite" aria-atomic="false">
      {summary && (
        <button
          onClick={onToggle}
          className="mb-2 flex w-full items-center justify-between text-left text-xs text-[var(--text-low)] hover:text-[var(--text-mid)]"
          aria-expanded={true}
        >
          <span>Pipeline</span>
          <ChevronIcon open />
        </button>
      )}
      <ol className="space-y-1.5">
        {stages.map((s) => (
          <li key={s.id} className="flex items-center gap-2 text-sm">
            <span
              className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${
                s.status === "done" ? "bg-[var(--confirmed)]" : "bg-[var(--live)] stage-dot--active"
              }`}
              aria-hidden="true"
            />
            <span className={s.status === "done" ? "text-[var(--text-low)] line-through decoration-[var(--confirmed)]/40" : "text-[var(--text-hi)]"}>
              {s.label}
            </span>
            {s.detail && <span className="mono text-xs text-[var(--text-low)]">{s.detail}</span>}
            {s.status === "done" && <span className="status-dot status-dot--confirmed text-xs" aria-label="done" />}
          </li>
        ))}
      </ol>
    </div>
  );
}

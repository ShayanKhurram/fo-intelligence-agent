"use client";

import { formatEta } from "@/lib/watch-format";

// T46.4 — the run control. The button BECOMES the progress meter in place: idle, running
// and finished are three states of one capsule, never a button that spawns a banner
// somewhere else on the page. The user's eye should never have to re-find the thing it
// just clicked.

export type RunPhase = "idle" | "starting" | "running" | "finished";

export type RunControlProps = {
  phase: RunPhase;
  scope: number;
  onScopeChange: (n: number) => void;
  onStart: () => void;
  onStop: () => void;
  completed: number;
  total: number;
  found: number;
  etaMs: number | null;
  scanning: string | null;
  remainingUnresearched: number;
  orgCount: number;
  error: string | null;
};

// Scope options. "All" is capped at the real org count so the control can never offer to
// research more organizations than exist.
function scopeOptions(orgCount: number): { value: number; label: string }[] {
  const opts = [
    { value: 60, label: "60 orgs · ~1 min" },
    { value: 240, label: "240 orgs · ~4 min" },
  ].filter((o) => o.value < orgCount);
  opts.push({ value: orgCount, label: `All ${orgCount} · ~${Math.max(1, Math.ceil(orgCount / 60))} min` });
  return opts;
}

export function RunControl({
  phase,
  scope,
  onScopeChange,
  onStart,
  onStop,
  completed,
  total,
  found,
  etaMs,
  scanning,
  remainingUnresearched,
  orgCount,
  error,
}: RunControlProps) {
  const active = phase === "running" || phase === "starting";
  const pct = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 0;

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-2">
        {phase === "idle" && (
          <>
            <label className="sr-only" htmlFor="watch-scope">
              How many organizations to research
            </label>
            <select
              id="watch-scope"
              value={scope}
              onChange={(e) => onScopeChange(Number(e.target.value))}
              className="mono rounded-[var(--r-sm)] border border-[var(--edge)] bg-[var(--bg-raised)] px-2 py-1.5 text-xs text-[var(--text-mid)]"
            >
              {scopeOptions(orgCount).map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            <button
              onClick={onStart}
              className="mono flex items-center gap-1.5 rounded-[var(--r-pill)] border border-[var(--live)]/50 bg-[var(--bg-raised)] px-3.5 py-1.5 text-xs text-[var(--text-hi)] hover:border-[var(--live)]"
            >
              <span aria-hidden="true">⟳</span> Run research
            </button>
          </>
        )}

        {active && (
          // The capsule is the progress bar. The fill is an absolutely positioned layer
          // BEHIND the label (low-alpha --live so the text stays legible above it), not a
          // separate bar element that would move the meter away from the control.
          <div
            role="progressbar"
            aria-valuenow={completed}
            aria-valuemin={0}
            aria-valuemax={total || 1}
            aria-valuetext={
              phase === "starting"
                ? "starting research"
                : `${completed} of ${total} organizations scanned, ${formatEta(etaMs)} remaining`
            }
            className="relative flex items-center gap-2.5 overflow-hidden rounded-[var(--r-pill)] border border-[var(--live)]/50 bg-[var(--bg-raised)] px-3.5 py-1.5"
          >
            <span
              aria-hidden="true"
              className="absolute inset-y-0 left-0 bg-[var(--live)]/20 transition-[width] duration-500 ease-out"
              style={{ width: `${pct}%` }}
            />
            <span className="mono relative text-xs tabular-nums text-[var(--text-hi)]">
              {phase === "starting" ? "starting…" : `${completed} / ${total} · ${formatEta(etaMs)}`}
            </span>
            <button
              onClick={onStop}
              className="mono relative rounded-[var(--r-pill)] border border-[var(--edge-lit)] px-2 py-0.5 text-[10px] uppercase tracking-wider text-[var(--text-mid)] hover:text-[var(--text-hi)]"
            >
              Stop
            </button>
          </div>
        )}

        {phase === "finished" && (
          <>
            <span className="mono rounded-[var(--r-pill)] border border-[var(--edge)] bg-[var(--bg-raised)] px-3 py-1.5 text-xs text-[var(--text-mid)]">
              {completed} scanned · <span className="text-[var(--text-hi)]">{found}</span> new signal
              {found === 1 ? "" : "s"}
            </span>
            {remainingUnresearched > 0 && (
              <button
                onClick={onStart}
                className="mono flex items-center gap-1.5 rounded-[var(--r-pill)] border border-[var(--live)]/50 bg-[var(--bg-raised)] px-3.5 py-1.5 text-xs text-[var(--text-hi)] hover:border-[var(--live)]"
              >
                <span aria-hidden="true">⟳</span> Continue ({remainingUnresearched})
              </button>
            )}
          </>
        )}
      </div>

      {/* The scanning line and the error share one live region so a screen reader hears
          progress without the whole capsule being re-announced on every tick. */}
      <p aria-live="polite" className="mono max-w-[280px] truncate text-right text-[11px] text-[var(--text-low)]">
        {error
          ? error
          : active && scanning
            ? `scanning · ${scanning}`
            : active
              ? "between organizations…"
              : ""}
      </p>
    </div>
  );
}

"use client";

import { Fragment, useState } from "react";
import type { RankedCandidate } from "@/lib/plan-rank";
import type { Excluded } from "@/lib/plan-retrieval";
import { ChevronIcon } from "./icons";

// T47.7 — the ranked shortlist.
//
// Below 1024 this was a seven-column numeric table pinned at min-w-[720px] inside an
// overflow-x-auto, so on a 390px phone the office name scrolled out of view before the
// user reached the numbers that name refers to. The table is now desktop-only and the
// same rows render as cards below it — name and score together, sub-scores as a labelled
// grid that wraps.
//
// Server order is preserved in both forms and there is no sort control, deliberately: the
// order IS the product, and a sortable header would let the UI flatter the ranking
// (settled at T44.4).

const SUBS = [
  { key: "fit", label: "Fit" },
  { key: "reach", label: "Reach" },
  { key: "recency", label: "Recency" },
  { key: "trust", label: "Trust" },
] as const;

export function RankedList({
  rows,
  excluded,
  truncated,
  sweptTotal,
  sweptConsidered,
  onOpen,
}: {
  rows: RankedCandidate[];
  excluded: Excluded[];
  truncated: boolean;
  sweptTotal: number | null;
  sweptConsidered: number | null;
  onOpen: (recordId: string) => void;
}) {
  const [excludedOpen, setExcludedOpen] = useState(false);

  if (rows.length === 0) {
    return <p className="py-8 text-center text-sm text-[var(--text-mid)]">No ranked records for this question.</p>;
  }

  return (
    <div className="space-y-4">
      {truncated && sweptTotal != null && sweptConsidered != null && (
        <div className="rounded-[var(--r-md)] border border-[var(--partial)]/40 bg-[var(--bg-glass)] px-4 py-3 text-sm text-[var(--text-mid)]">
          Ranked {sweptConsidered.toLocaleString()} of {sweptTotal.toLocaleString()} matching records — the sweep
          capped at the most the ranker could compare, so the rest were not considered.
        </div>
      )}

      {/* ---- cards: below 1024 ---- */}
      <ul className="space-y-2.5 lg:hidden">
        {rows.map((r, i) => (
          <li key={r.record_id}>
            <button
              onClick={() => onOpen(r.record_id)}
              className="glass block w-full p-4 text-left transition-colors hover:bg-[var(--bg-glass-hi)]"
            >
              <div className="flex items-baseline justify-between gap-3">
                <span className="flex min-w-0 items-baseline gap-2">
                  <span className="mono shrink-0 text-xs text-[var(--text-low)]">{i + 1}</span>
                  <span className="truncate text-[var(--text-hi)]">{r.entity_name}</span>
                </span>
                <span className="mono shrink-0 tabular-nums text-[var(--text-hi)]">{r.score.toFixed(2)}</span>
              </div>

              <dl className="mt-3 grid grid-cols-4 gap-2">
                {SUBS.map(({ key, label }) => (
                  <div key={key} className="min-w-0">
                    <dt className="mono text-[9px] uppercase tracking-wider text-[var(--text-low)]">{label}</dt>
                    <dd className="mono tabular-nums text-xs text-[var(--text-mid)]">{r.scores[key].toFixed(2)}</dd>
                  </div>
                ))}
              </dl>

              {r.why.length > 0 && (
                <ul className="mono mt-3 list-disc space-y-0.5 pl-4 text-[11px] text-[var(--text-low)]">
                  {r.why.map((w, j) => (
                    <li key={j}>{w}</li>
                  ))}
                </ul>
              )}
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
            </button>
          </li>
        ))}
      </ul>

      {/* ---- table: 1024 and up ---- */}
      <div className="glass hidden overflow-x-auto lg:block">
        <table className="w-full text-sm">
          <thead>
            <tr className="mono border-b border-[var(--edge)] text-left text-[10px] uppercase tracking-wider text-[var(--text-low)]">
              <th className="px-3 py-2 font-normal">#</th>
              <th className="px-3 py-2 font-normal">Office</th>
              <th className="px-3 py-2 text-right font-normal">Score</th>
              {SUBS.map(({ key, label }) => (
                <th key={key} className="px-3 py-2 text-right font-normal">
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <Fragment key={r.record_id}>
                <tr
                  onClick={() => onOpen(r.record_id)}
                  className="cursor-pointer border-b border-[var(--edge-soft)] hover:bg-[var(--bg-glass-hi)]"
                >
                  <td className="mono px-3 py-2 text-[var(--text-low)]">{i + 1}</td>
                  <td className="px-3 py-2 text-[var(--text-hi)]">{r.entity_name}</td>
                  <td className="mono px-3 py-2 text-right tabular-nums text-[var(--text-hi)]">{r.score.toFixed(2)}</td>
                  {SUBS.map(({ key }) => (
                    <td key={key} className="mono px-3 py-2 text-right tabular-nums text-[var(--text-mid)]">
                      {r.scores[key].toFixed(2)}
                    </td>
                  ))}
                </tr>
                <tr className="border-b border-[var(--edge)]">
                  <td colSpan={7} className="px-3 pb-3 pt-0">
                    {r.why.length > 0 && (
                      <ul className="mono list-disc pl-5 text-[11px] text-[var(--text-low)]">
                        {r.why.map((w, j) => (
                          <li key={j}>{w}</li>
                        ))}
                      </ul>
                    )}
                    {r.gaps.length > 0 && (
                      <div className="mt-1 flex flex-wrap gap-1.5">
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
                  </td>
                </tr>
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>

      {/* The excluded appendix — collapsed, labelled with its count. The honesty half of
          the deliverable, not an appendix to hide. */}
      {excluded.length > 0 && (
        <div className="rounded-[var(--r-md)] border border-[var(--edge)] bg-[var(--bg-glass)]">
          <button
            onClick={() => setExcludedOpen((v) => !v)}
            className="flex w-full items-center justify-between px-4 py-3 text-left text-sm text-[var(--text-mid)] hover:text-[var(--text-hi)]"
            aria-expanded={excludedOpen}
          >
            <span>
              <span className="mono text-[var(--text-hi)]">{excluded.length}</span> office
              {excluded.length === 1 ? "" : "s"} excluded
            </span>
            <ChevronIcon open={excludedOpen} />
          </button>
          {excludedOpen && (
            <ul className="border-t border-[var(--edge)] px-4 py-2">
              {excluded.map((x) => (
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
  );
}

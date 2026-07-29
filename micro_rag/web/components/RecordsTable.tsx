"use client";

import type { RecordRow } from "@/lib/types";
import { formatUsd, formatDate, statusGlyphClass } from "@/lib/format";

// ui_plan.md §4 — Records tab: "the full retrieved set as a table."
export function RecordsTable({ records, onOpen }: { records: RecordRow[]; onOpen: (recordId: string) => void }) {
  if (records.length === 0) {
    return <p className="mt-8 text-center text-sm text-[var(--text-mid)]">No records retrieved yet — ask a question first.</p>;
  }

  return (
    <div className="glass overflow-x-auto">
      <table className="w-full min-w-[720px] text-sm">
        <thead>
          <tr className="border-b border-[var(--edge)] text-left text-xs uppercase tracking-wide text-[var(--text-low)]">
            <th className="px-4 py-3 font-normal">Entity</th>
            <th className="px-4 py-3 font-normal">Type</th>
            <th className="px-4 py-3 font-normal">State</th>
            <th className="px-4 py-3 font-normal">AUM</th>
            <th className="px-4 py-3 font-normal">Principal</th>
            <th className="px-4 py-3 font-normal">Activity</th>
            <th className="px-4 py-3 font-normal">Confidence</th>
          </tr>
        </thead>
        <tbody>
          {records.map((r) => (
            <tr
              key={r.record_id}
              onClick={() => onOpen(r.record_id)}
              className="cursor-pointer border-b border-[var(--edge-soft)] last:border-0 hover:bg-[var(--bg-glass-hi)]"
            >
              <td className="px-4 py-3">{r.entity_name}</td>
              <td className="mono px-4 py-3 text-xs text-[var(--text-mid)]">{r.entity_type}</td>
              <td className="mono px-4 py-3 text-xs text-[var(--text-mid)]">{r.hq_state ?? "—"}</td>
              <td className="mono px-4 py-3 text-xs text-[var(--text-mid)]">{formatUsd(r.aum_usd) ?? "—"}</td>
              <td className="px-4 py-3 text-xs text-[var(--text-mid)]">{r.principal_name ?? "—"}</td>
              <td className="mono status-live px-4 py-3 text-xs">{formatDate(r.most_recent_signal_date) ?? "—"}</td>
              <td className="px-4 py-3 text-xs">
                <span className={statusGlyphClass(r.record_confidence)}>{r.record_confidence}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

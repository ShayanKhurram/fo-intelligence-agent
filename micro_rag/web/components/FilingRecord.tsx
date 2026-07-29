import type { ProvenanceRow } from "@/lib/types";
import { statusLabel } from "@/lib/types";
import { statusGlyphClass, formatDate } from "@/lib/format";

// The shared filing-record block — ui_plan.md §6's evidence format (CLAIM / FIELD /
// SOURCE / CONFIRM / CLASS / STATUS). Used by both the Evidence drawer (one record's
// fields) and the Evidence tab (every claim across the whole answer, flat) so the two
// views can never render the same provenance row differently.
export function FilingRecordBlock({ row, heading }: { row: ProvenanceRow; heading?: string }) {
  return (
    <div className="mono border-b border-[var(--edge-soft)] pb-4 text-xs leading-relaxed">
      {heading && <div className="display mb-2 font-sans text-sm text-[var(--text-hi)]">{heading}</div>}
      <Row label="CLAIM">
        <span className="font-sans text-sm text-[var(--text-hi)]">
          {row.value ?? <span className="text-[var(--unknown)]">(not confirmed)</span>}
        </span>
      </Row>
      <Row label="FIELD">{row.field_name}</Row>
      {row.source_url && (
        <Row label="SOURCE">
          {row.source_class ?? "unknown source"} ·{" "}
          <a href={row.source_url} target="_blank" rel="noreferrer" className="status-live underline decoration-dotted break-all">
            {row.source_url}
          </a>
        </Row>
      )}
      {row.confirming_url && (
        <Row label="CONFIRM">
          {row.confirming_class ?? "confirming source"} ·{" "}
          <a href={row.confirming_url} target="_blank" rel="noreferrer" className="status-live underline decoration-dotted break-all">
            {row.confirming_url}
          </a>
          {row.retrieved_at && <span className="text-[var(--text-low)]"> · retrieved {formatDate(row.retrieved_at)}</span>}
        </Row>
      )}
      {row.confirming_class && row.source_class && row.confirming_class !== row.source_class && (
        <Row label="CLASS">
          {row.source_class} → {row.confirming_class}
          <span className="text-[var(--text-low)]"> (confirming class ≠ originating class)</span>
        </Row>
      )}
      <Row label="STATUS">
        <span className={statusGlyphClass(row.status)}>{statusLabel(row.status)}</span>
      </Row>
    </div>
  );
}

export function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-1 flex gap-3">
      <span className="w-16 shrink-0 text-[var(--text-low)]">{label}</span>
      <span className="min-w-0 flex-1 text-[var(--text-mid)]">{children}</span>
    </div>
  );
}

"use client";

import { useEffect, useState } from "react";
import type { ProvenanceRow, RecordRow } from "@/lib/types";
import { statusClass, statusLabel } from "@/lib/types";

export function ProvenanceDrawer({ recordId, onClose }: { recordId: string; onClose: () => void }) {
  const [record, setRecord] = useState<RecordRow | null>(null);
  const [provenance, setProvenance] = useState<ProvenanceRow[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    fetch(`/api/record/${recordId}`)
      .then((r) => r.json())
      .then((data) => {
        setRecord(data.record ?? null);
        setProvenance(data.provenance ?? []);
      })
      .finally(() => setLoading(false));
  }, [recordId]);

  return (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-black/30"
      onClick={onClose}
      role="dialog"
      aria-label="Record provenance"
    >
      <div
        className="h-full w-full max-w-lg overflow-y-auto bg-[var(--paper)] p-6 shadow-xl border-l border-[var(--rule)]"
        onClick={(e) => e.stopPropagation()}
      >
        <button onClick={onClose} className="mb-4 text-sm text-[var(--ink-500)] hover:text-[var(--ink-900)]" aria-label="Close">
          Close ×
        </button>

        {loading && <p className="text-[var(--ink-500)]">Loading…</p>}

        {record && (
          <>
            <h2 className="mb-1 text-xl font-semibold">{record.entity_name}</h2>
            <p className="mono mb-6 text-xs text-[var(--ink-500)]">
              record_id: {record.record_id} · {record.entity_type}
            </p>

            <div className="space-y-4">
              {provenance.map((p) => (
                <div key={p.field_name} className="border-b border-[var(--rule)] pb-3">
                  <div className="mono text-xs uppercase tracking-wide text-[var(--ink-500)]">CLAIM</div>
                  <div className="mb-1">{p.value ?? <span className="text-[var(--unknown)]">(not confirmed)</span>}</div>
                  <div className="mono text-xs text-[var(--ink-500)]">FIELD &nbsp;{p.field_name}</div>
                  {p.source_url && (
                    <div className="mono text-xs text-[var(--ink-500)] break-all">
                      SOURCE &nbsp;
                      <a href={p.source_url} target="_blank" rel="noreferrer" className="underline">
                        {p.source_class} · {p.source_url}
                      </a>
                    </div>
                  )}
                  {p.confirming_url && (
                    <div className="mono text-xs text-[var(--ink-500)] break-all">
                      CONFIRM &nbsp;{p.confirming_class} · {p.confirming_url}
                    </div>
                  )}
                  {p.confirming_class && p.source_class && p.confirming_class !== p.source_class && (
                    <div className="mono text-xs text-[var(--ink-500)]">
                      CLASS &nbsp;{p.source_class} → {p.confirming_class} (confirming class ≠ originating class)
                    </div>
                  )}
                  <div className={`mono text-xs font-semibold ${statusClass(p.status)}`}>
                    STATUS &nbsp;{statusLabel(p.status)}
                  </div>
                </div>
              ))}
              {provenance.length === 0 && <p className="text-[var(--ink-500)]">No provenance recorded for this record.</p>}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

"use client";

import { useEffect, useRef, useState } from "react";
import type { ProvenanceRow, RecordRow } from "@/lib/types";
import { FilingRecordBlock } from "./FilingRecord";

// ui_plan.md §6 — "the signature." The contents render as a filing record in mono:
// CLAIM / FIELD / SOURCE / CONFIRM / CLASS / STATUS. The CLASS line (origin → confirming
// class) is the product's whole thesis in one row.
//
// T47.6 split this from its container: the body is identical in all three responsive
// forms (bottom sheet, side drawer, pinned panel), so only the surface around it changes.
export function EvidenceContent({ recordId, focusField }: { recordId: string; focusField?: string | null }) {
  const [record, setRecord] = useState<RecordRow | null>(null);
  const [provenance, setProvenance] = useState<ProvenanceRow[]>([]);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const loading = loadedFor !== recordId;
  const fieldRefs = useRef<Map<string, HTMLDivElement>>(new Map());

  useEffect(() => {
    let cancelled = false;
    fetch(`/api/record/${recordId}`)
      .then((r) => r.json())
      .then((data) => {
        if (cancelled) return;
        setRecord(data.record ?? null);
        setProvenance(data.provenance ?? []);
        setLoadedFor(recordId);
      })
      .catch(() => {
        if (!cancelled) setLoadedFor(recordId);
      });
    return () => {
      cancelled = true;
    };
  }, [recordId]);

  useEffect(() => {
    if (focusField && fieldRefs.current.has(focusField)) {
      fieldRefs.current.get(focusField)?.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }, [focusField, provenance]);

  if (loading) {
    return (
      <div className="space-y-3" aria-busy="true">
        <div className="skeleton h-6 w-1/2" />
        <div className="skeleton h-24 w-full" />
        <div className="skeleton h-24 w-full" />
      </div>
    );
  }

  if (!record) {
    return <p className="text-sm text-[var(--text-mid)]">That record could not be loaded.</p>;
  }

  return (
    <>
      <h2 className="display mb-1 text-xl">{record.entity_name}</h2>
      <p className="mono mb-6 text-xs text-[var(--text-low)]">
        {record.record_id} · {record.entity_type}
      </p>

      <div className="space-y-5">
        {provenance.map((p) => (
          <div
            key={p.field_name}
            ref={(el) => {
              if (el) fieldRefs.current.set(p.field_name, el);
            }}
            className={focusField === p.field_name ? "rounded-[var(--r-sm)] p-2 ring-1 ring-[var(--live)]" : ""}
          >
            <FilingRecordBlock row={p} />
          </div>
        ))}
        {provenance.length === 0 && (
          <p className="font-sans text-[var(--text-mid)]">No provenance recorded for this record.</p>
        )}
      </div>
    </>
  );
}

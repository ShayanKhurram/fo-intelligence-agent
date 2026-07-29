"use client";

import { useEffect, useRef, useState, type RefObject } from "react";
import type { ProvenanceRow, RecordRow } from "@/lib/types";
import { FilingRecordBlock } from "./FilingRecord";
import { CloseIcon } from "./icons";

// ui_plan.md §6 — "the signature." Slides from the right at 420px, glass-hi,
// shadow-drawer, 240ms cubic-bezier(0.32,0.72,0,1). Contents render as a filing record
// in mono: CLAIM / FIELD / SOURCE / CONFIRM / CLASS / STATUS. The CLASS line (origin →
// confirming class) is the product's whole thesis in one row.
export function EvidenceDrawer({
  recordId,
  focusField,
  onClose,
  returnFocusRef,
}: {
  recordId: string;
  focusField?: string | null;
  onClose: () => void;
  returnFocusRef: RefObject<HTMLElement | null>;
}) {
  const [record, setRecord] = useState<RecordRow | null>(null);
  const [provenance, setProvenance] = useState<ProvenanceRow[]>([]);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const loading = loadedFor !== recordId;
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
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
      });
    return () => {
      cancelled = true;
    };
  }, [recordId]);

  // Focus trap + Escape-to-close + return focus to whatever opened the drawer
  // (ui_plan.md §8's "drawer traps focus, closes on Escape, returns focus to the pill
  // that opened it").
  useEffect(() => {
    const returnTo = returnFocusRef.current;
    closeRef.current?.focus();
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key !== "Tab" || !panelRef.current) return;
      const focusable = panelRef.current.querySelectorAll<HTMLElement>(
        'button, [href], input, [tabindex]:not([tabindex="-1"])'
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      returnTo?.focus();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (focusField && fieldRefs.current.has(focusField)) {
      fieldRefs.current.get(focusField)?.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }, [focusField, provenance]);

  return (
    <div className="fixed inset-0 z-50 flex justify-end" role="presentation">
      <div className="scrim absolute inset-0" onClick={onClose} aria-hidden="true" />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={`Evidence for ${record?.entity_name ?? recordId}`}
        className="glass-hi drawer-enter relative flex h-full w-full max-w-[420px] flex-col overflow-y-auto p-6 shadow-[var(--shadow-drawer)]"
      >
        <button
          ref={closeRef}
          onClick={onClose}
          className="mb-4 flex w-fit items-center gap-1.5 text-sm text-[var(--text-mid)] hover:text-[var(--text-hi)]"
        >
          <CloseIcon /> Close
        </button>

        {loading && <p className="text-[var(--text-mid)]">Loading…</p>}

        {record && (
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
                  className={focusField === p.field_name ? "ring-1 ring-[var(--live)] rounded-[var(--r-sm)] p-2" : ""}
                >
                  <FilingRecordBlock row={p} />
                </div>
              ))}
              {provenance.length === 0 && <p className="font-sans text-[var(--text-mid)]">No provenance recorded for this record.</p>}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

"use client";

import { useEffect, useState } from "react";
import type { ClaimVerifiedPayload, ProvenanceRow } from "@/lib/types";
import { FilingRecordBlock } from "./FilingRecord";

// ui_plan.md §4 — the audit view. "Every claim in the answer with its full provenance
// chain, flat." Directly demonstrates the thing being graded, so it gets its own tab
// rather than being buried inside per-record drawers.
export function EvidenceTab({ claims, entityNames }: { claims: ClaimVerifiedPayload[]; entityNames: Map<string, string> }) {
  const [rowsByRecord, setRowsByRecord] = useState<Map<string, ProvenanceRow[]>>(new Map());

  const uniqueRecordIds = [...new Set(claims.map((c) => c.recordId))];
  const missingIds = uniqueRecordIds.filter((id) => !rowsByRecord.has(id));
  const loading = missingIds.length > 0;

  useEffect(() => {
    if (missingIds.length === 0) return;
    let cancelled = false;
    Promise.all(
      missingIds.map((id) =>
        fetch(`/api/record/${id}`)
          .then((r) => r.json())
          .then((data) => [id, (data.provenance ?? []) as ProvenanceRow[]] as const)
      )
    ).then((results) => {
      if (cancelled) return;
      setRowsByRecord((prev) => {
        const next = new Map(prev);
        for (const [id, rows] of results) next.set(id, rows);
        return next;
      });
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [missingIds.join(",")]);

  if (claims.length === 0) {
    return <p className="mt-8 text-center text-sm text-[var(--text-mid)]">No claims to audit yet — ask a question first.</p>;
  }

  return (
    <div className="space-y-6">
      {claims.map((claim, i) => {
        const rows = rowsByRecord.get(claim.recordId);
        const row = rows?.find((r) => r.field_name === claim.field);
        const entityName = entityNames.get(claim.recordId) ?? claim.recordId;
        if (!row) {
          return (
            <div key={i} className="glass p-4 text-xs text-[var(--text-low)]">
              {loading ? "Loading evidence…" : `No provenance row found for ${entityName} · ${claim.field}`}
            </div>
          );
        }
        return (
          <div key={i} className="glass p-4">
            <FilingRecordBlock row={row} heading={entityName} />
          </div>
        );
      })}
    </div>
  );
}

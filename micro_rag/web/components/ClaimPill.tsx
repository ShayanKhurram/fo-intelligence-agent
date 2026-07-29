"use client";

import type { ClaimVerifiedPayload } from "@/lib/types";
import { statusGlyphClass, statusTier } from "@/lib/format";

// ui_plan.md §6 — inline, pill radius, mono 11px, height 20px. Border in the status
// colour at 30% alpha, fill at 8%; hover raises fill to 14%. Confirmed pills carry
// colour; unknown pills read grey and slightly transparent — visibly weaker, on purpose.
export function ClaimPill({ claim, onOpen }: { claim: ClaimVerifiedPayload; entityName?: string; onOpen: (recordId: string, field: string) => void }) {
  const tier = statusTier(claim.status);
  const colorVar = tier === "confirmed" ? "var(--confirmed)" : tier === "partial" ? "var(--partial)" : "var(--unknown)";

  return (
    <button
      onClick={() => onOpen(claim.recordId, claim.field)}
      className={`enter-pill mono mx-0.5 inline-flex h-5 items-center gap-1 rounded-[var(--r-pill)] px-2 align-middle text-[11px] transition-colors hover:brightness-110 ${
        tier === "unknown" ? "opacity-70" : ""
      }`}
      style={{
        border: `1px solid color-mix(in srgb, ${colorVar} 30%, transparent)`,
        background: `color-mix(in srgb, ${colorVar} 8%, transparent)`,
        color: colorVar,
      }}
      title={`${claim.value} — click for evidence`}
    >
      <span className={statusGlyphClass(claim.status)} />
      <span className="max-w-[9rem] truncate">{claim.field}</span>
    </button>
  );
}

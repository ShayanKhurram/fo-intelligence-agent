"use client";

import type { FilterChip } from "@/lib/types";
import { CloseIcon } from "./icons";

// ui_plan.md §6 — "parsed from the query, removable, mono labels. Removing one re-runs
// the search." How a non-technical user loosens a query that returned nothing without
// rewriting it.
export function FilterChips({ chips, onRemove }: { chips: FilterChip[]; onRemove: (key: string) => void }) {
  if (chips.length === 0) return null;
  return (
    <div className="mb-3 flex flex-wrap gap-1.5" role="list" aria-label="Parsed filters">
      {chips.map((c) => (
        <span
          key={c.key}
          role="listitem"
          className="mono flex items-center gap-1.5 rounded-[var(--r-pill)] border border-[var(--edge)] bg-[var(--bg-raised)] py-1 pl-2.5 pr-1.5 text-xs text-[var(--text-mid)]"
        >
          {c.label}
          <button
            onClick={() => onRemove(c.key)}
            className="rounded-full p-0.5 text-[var(--text-low)] hover:bg-[var(--edge)] hover:text-[var(--text-hi)]"
            aria-label={`Remove filter ${c.label}`}
          >
            <CloseIcon />
          </button>
        </span>
      ))}
    </div>
  );
}

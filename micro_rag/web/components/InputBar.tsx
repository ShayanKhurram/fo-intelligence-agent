"use client";

import { forwardRef } from "react";
import { FilterIcon, SendIcon } from "./icons";

// ui_plan.md §6 — fixed, glass, --r-lg. Left slot for a filter toggle, right for
// submit. Focus ring in --live at 40%.
export const InputBar = forwardRef<HTMLInputElement, {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  loading: boolean;
  hasFilters: boolean;
}>(function InputBar({ value, onChange, onSubmit, loading, hasFilters }, ref) {
  return (
    <div className="sticky bottom-0 z-30 mx-auto w-full max-w-5xl px-4 pb-4 pt-2 sm:px-6">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (value.trim() && !loading) onSubmit();
        }}
        className="glass flex items-center gap-2 rounded-[var(--r-lg)] px-3 py-2 shadow-[var(--shadow-drawer)] focus-within:ring-2 focus-within:ring-[color-mix(in_srgb,var(--live)_40%,transparent)]"
      >
        <span className={hasFilters ? "status-live shrink-0" : "shrink-0 text-[var(--text-low)]"} title="Filters are parsed automatically from your question">
          <FilterIcon />
        </span>
        <input
          ref={ref}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            // Explicit handler rather than relying solely on native form-submit-on-Enter
            // — more robust across browsers/IME composition, and the one interaction a
            // search box absolutely cannot get wrong.
            if (e.key === "Enter" && !e.nativeEvent.isComposing && value.trim() && !loading) {
              e.preventDefault();
              onSubmit();
            }
          }}
          placeholder="Ask about a family office…"
          aria-label="Ask about a family office"
          className="min-w-0 flex-1 bg-transparent text-sm text-[var(--text-hi)] placeholder:text-[var(--text-low)] focus:outline-none"
        />
        <button
          type="submit"
          disabled={loading || !value.trim()}
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[var(--confirmed)] text-[var(--bg-base)] transition-opacity disabled:opacity-30"
          aria-label="Submit question"
        >
          <SendIcon />
        </button>
      </form>
    </div>
  );
});

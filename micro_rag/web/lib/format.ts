export function formatUsd(n: number | null | undefined): string | null {
  if (n == null) return null;
  if (n >= 1e9) return `$${(n / 1e9).toFixed(n % 1e9 === 0 ? 0 : 1)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(n % 1e6 === 0 ? 0 : 1)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(0)}K`;
  return `$${n}`;
}

export function formatDate(d: string | null | undefined): string | null {
  if (!d) return null;
  const date = new Date(d);
  if (Number.isNaN(date.getTime())) return d;
  return date.toISOString().slice(0, 10);
}

/** Status → one of the three verification tiers this whole UI is built around
 * (ui_plan.md §1/§8) — confirmed / partial / unknown, each with its own glyph. */
export function statusTier(status: string | null | undefined): "confirmed" | "partial" | "unknown" {
  if (!status) return "unknown";
  if (status === "verified" || status === "confirmed") return "confirmed";
  if (["single_source", "format_only", "pattern_inferred"].includes(status)) return "partial";
  return "unknown";
}

export function statusGlyphClass(status: string | null | undefined): string {
  return `status-dot status-dot--${statusTier(status)}`;
}

// T46.4 — presentation helpers for the Intent Watcher. Pure, client-safe (no `lib/db.ts`
// import, so this can be pulled into a "use client" component and unit-tested with
// `node --test`).
//
// Every function here has one job: never invent information the data does not carry. An
// absent date renders as "date unknown", not as today; an unknown ETA renders as
// "estimating…", not as a seeded guess.

export type KindMeta = { glyph: string; label: string; color: string };

// The five activity kinds, one per functional colour in `app/globals.css`. The rule that
// governs this file: colour NEVER carries meaning alone — every chip renders its glyph and
// its word alongside the colour, so the kind survives greyscale, colour-blindness, and a
// screen reader.
export const KIND_META: Record<string, KindMeta> = {
  capital_deployment: { glyph: "▲", label: "capital", color: "var(--confirmed)" },
  exit: { glyph: "▼", label: "exit", color: "var(--urgent)" },
  personnel: { glyph: "◆", label: "people", color: "var(--live)" },
  mandate_shift: { glyph: "◈", label: "mandate", color: "var(--partial)" },
  firm_news: { glyph: "○", label: "news", color: "var(--unknown)" },
};

export const KIND_ORDER = [
  "capital_deployment",
  "exit",
  "personnel",
  "mandate_shift",
  "firm_news",
] as const;

export function kindMeta(kind: string): KindMeta {
  return KIND_META[kind] ?? KIND_META.firm_news;
}

/** "2d" / "3w" / "5mo" / "date unknown". `null` is the NORMAL case here — 420 of the 478
 * orgs carry no dated signal at all, because `provenance.retrieved_at` is NULL on 805 of
 * 861 baseline rows. It must read as a deliberate state, never as a broken one. */
export function formatRelative(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "date unknown";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return "date unknown";
  const then = Date.UTC(+m[1], +m[2] - 1, +m[3]);
  const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const days = Math.round((today - then) / 86_400_000);
  if (days < 0) return "today"; // a future-dated source; don't render "-3d"
  if (days === 0) return "today";
  if (days === 1) return "1d";
  if (days < 7) return `${days}d`;
  if (days < 35) return `${Math.round(days / 7)}w`;
  if (days < 365) return `${Math.round(days / 30)}mo`;
  return `${Math.round(days / 365)}y`;
}

/** "Aug 15" for a signal's source line. Undated returns null so the caller can omit the
 * segment entirely rather than print a placeholder. */
export function formatShortDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return null;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const mo = MONTHS[+m[2] - 1];
  if (!mo) return null;
  return `${mo} ${+m[3]}`;
}

/** "≈ 45s" / "≈ 3 min" / "estimating…". A null ETA means fewer than 3 orgs have completed,
 * so no median exists yet — and a countdown invented from a constant would be worse than
 * admitting we don't know yet. */
export function formatEta(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return "estimating…";
  if (ms < 1000) return "almost done";
  if (ms < 60_000) return `≈ ${Math.max(1, Math.round(ms / 1000))}s`;
  return `≈ ${Math.ceil(ms / 60_000)} min`;
}

/** "$412M" / "$2.0B". Returns null for a missing AUM so the caller drops the segment
 * instead of rendering an em-dash nobody can read a number out of. */
export function formatAum(aum: number | null | undefined): string | null {
  if (aum === null || aum === undefined || !Number.isFinite(aum) || aum <= 0) return null;
  if (aum >= 1_000_000_000) {
    const b = aum / 1_000_000_000;
    return `$${b >= 10 ? Math.round(b) : b.toFixed(1)}B`;
  }
  if (aum >= 1_000_000) return `$${Math.round(aum / 1_000_000)}M`;
  if (aum >= 1_000) return `$${Math.round(aum / 1_000)}K`;
  return `$${aum}`;
}

/** The meta line under an entity name: "MFO · TX · $412M". Any missing part is dropped
 * rather than printed as a dash, so the line never advertises an absence. */
export function metaLine(parts: (string | null | undefined)[]): string {
  return parts.filter((p): p is string => !!p && p !== "type_unconfirmed").join(" · ");
}

/** "478 organizations · 845 signals · freshest 2 days ago". The third clause is omitted
 * entirely when nothing in the corpus carries a date. */
export function boardSummary(orgCount: number, signalCount: number, freshest: string | null): string {
  const parts = [
    `${orgCount.toLocaleString()} organization${orgCount === 1 ? "" : "s"}`,
    `${signalCount.toLocaleString()} signal${signalCount === 1 ? "" : "s"}`,
  ];
  if (freshest) {
    const rel = formatRelative(freshest);
    if (rel !== "date unknown") parts.push(`freshest ${rel === "today" ? "today" : rel + " ago"}`);
  }
  return parts.join(" · ");
}

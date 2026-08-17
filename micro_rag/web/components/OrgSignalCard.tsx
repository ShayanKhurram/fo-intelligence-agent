"use client";

import type { WatchSignal } from "@/lib/watch";
import { formatAum, formatRelative, formatShortDate, kindMeta, metaLine } from "@/lib/watch-format";

// T46.4 — one organization and its activity signals.
//
// Deliberately NOT a glass surface. `app/globals.css` allows at most three on screen and
// the header plus the run control already take two; a list of 478 blurred cards would also
// be a real compositing cost. Cards are flat `--bg-raised` with a hairline border.

function KindChip({ kind }: { kind: string }) {
  const meta = kindMeta(kind);
  // Glyph AND word, always together — colour never carries the meaning alone.
  return (
    <span className="mono inline-flex w-[96px] shrink-0 items-center gap-1.5 text-[11px]" style={{ color: meta.color }}>
      <span aria-hidden="true">{meta.glyph}</span>
      <span>{meta.label}</span>
    </span>
  );
}

function SignalRow({ signal }: { signal: WatchSignal }) {
  const date = formatShortDate(signal.observed_at);
  const source = signal.source_name || signal.source_domain;

  return (
    <li className="flex gap-3 py-2 first:pt-0 last:pb-0">
      <KindChip kind={signal.kind} />
      <div className="min-w-0 flex-1">
        <p className="text-sm leading-snug text-[var(--text-hi)]">
          {signal.headline}
          {signal.origin === "research" && (
            <span className="mono ml-2 rounded-[var(--r-pill)] border border-[var(--live)]/40 px-1.5 py-0.5 align-middle text-[9px] uppercase tracking-wider text-[var(--live)]">
              new
            </span>
          )}
        </p>

        {/* When there is no interpretation the line is ABSENT — no placeholder, no dash,
            no spinner that never resolves. A blank is the honest state and has to read as
            deliberate, because it is the common one. */}
        {signal.meaning && (
          <p className="mt-1 text-[13px] leading-snug text-[var(--text-mid)]">
            <span className="text-[var(--text-low)]" aria-hidden="true">
              →{" "}
            </span>
            {signal.meaning}
          </p>
        )}

        {(source || date) && (
          <p className="mono mt-1 text-[11px] text-[var(--text-low)]">
            {signal.source_url && source ? (
              <a
                href={signal.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="underline decoration-[var(--edge-lit)] underline-offset-2 hover:text-[var(--text-mid)]"
              >
                {source}
              </a>
            ) : (
              source
            )}
            {source && date ? " · " : ""}
            {date}
          </p>
        )}
      </div>
    </li>
  );
}

export function OrgSignalCard({
  org,
  onOpenEvidence,
}: {
  org: {
    record_id: string;
    entity_name: string;
    entity_type?: string;
    hq_state?: string | null;
    aum_usd?: number | null;
    signals: WatchSignal[];
    newestAt: string | null;
  };
  onOpenEvidence: (recordId: string) => void;
}) {
  const meta = metaLine([org.entity_type, org.hq_state, formatAum(org.aum_usd)]);
  const freshness = formatRelative(org.newestAt);
  const undated = freshness === "date unknown";

  return (
    <article className="rounded-[var(--r-md)] border border-[var(--edge)] bg-[var(--bg-raised)] px-4 py-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <button
          onClick={() => onOpenEvidence(org.record_id)}
          className="text-left text-[15px] font-medium text-[var(--text-hi)] hover:underline decoration-[var(--edge-lit)] underline-offset-2"
        >
          {org.entity_name}
        </button>
        <span className="flex-1" />
        <span className="mono text-[11px] text-[var(--text-mid)]">
          {org.signals.length} signal{org.signals.length === 1 ? "" : "s"}
        </span>
        <span className={`mono text-[11px] ${undated ? "text-[var(--text-low)]" : "text-[var(--text-mid)]"}`}>
          {freshness}
        </span>
      </div>

      {meta && <p className="mono mt-0.5 text-[11px] text-[var(--text-mid)]">{meta}</p>}

      <ul className="mt-3 divide-y divide-[var(--edge-soft)] border-t border-[var(--edge-soft)] pt-2">
        {org.signals.map((s) => (
          <SignalRow key={String(s.id)} signal={s} />
        ))}
      </ul>
    </article>
  );
}

"use client";

import { useEffect, useState } from "react";

// T37 — the agent's run log, hosted.
//
// The agent itself runs on a workstation and cannot run here: its state is a 145MB
// SQLite file, one lead takes minutes against Vercel's function timeouts, its scheduler
// is a background loop that outlives any request, and its fetch tier drives a headless
// Chromium. What this page serves is the *record* it pushes up after each run.
//
// Three levels, each fetched on expand, mirroring the local Log tab exactly:
//   runs -> the leads that run touched -> that lead's per-field provenance.

type Run = {
  run_id: string;
  kind: string;
  status: string;
  entity_count: number;
  started_at: string | null;
  ended_at: string | null;
  lead_count: number;
  notes: Record<string, unknown>;
};

type Lead = {
  entity_id: string;
  canonical_name: string | null;
  field_count: number;
  shipped_count: number;
  blank_count: number;
};

type FieldRecord = {
  field: string;
  value: unknown;
  status: string;
  how?: {
    summary?: string;
    wave?: string | null;
    question_id?: string | null;
    extraction_method?: string | null;
    source_class?: string | null;
    source_url?: string | null;
    also_produced_by?: { source_url?: string; source_class?: string; summary?: string }[];
  };
  blank_reason?: { code: string; detail?: string } | null;
  tool_calls?: { tool: string; result_summary?: string; ok?: boolean; cache_hit?: boolean }[];
  alternatives?: { value: unknown; why_not_used: string; source_class?: string }[];
  checks?: { check_id: string; severity: string; detail?: string }[];
};

function fmt(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

const pill =
  "mono rounded-[var(--r-pill)] border border-[var(--edge)] px-2 py-0.5 text-[10px] uppercase tracking-wider";

function FieldLog({ rec }: { rec: FieldRecord }) {
  const shipped = rec.value !== null && rec.value !== undefined && rec.value !== "";
  const how = rec.how ?? {};

  // Layer V records one finding per claim examined, so a field backed by six claims shows
  // the same check six times — six identical lines that read as six distinct problems.
  // Collapse to one line per (check_id, severity, detail) with a count.
  const dedupedChecks = Object.values(
    (rec.checks ?? []).reduce<Record<string, { check_id: string; severity: string; detail?: string; count: number }>>(
      (acc, c) => {
        const key = `${c.check_id}|${c.severity}|${c.detail ?? ""}`;
        acc[key] = acc[key] ? { ...acc[key], count: acc[key].count + 1 } : { ...c, count: 1 };
        return acc;
      },
      {}
    )
  );
  const meta = [how.wave && `wave ${how.wave}`, how.question_id, how.extraction_method, how.source_class]
    .filter(Boolean)
    .join(" · ");
  return (
    <div
      className="border-l-2 py-2 pl-3"
      style={{ borderColor: shipped ? "var(--confirmed)" : "var(--edge-lit)" }}
    >
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="mono font-semibold text-[var(--text-hi)]">{rec.field}</span>
        <span className={shipped ? "text-[var(--text-hi)]" : "italic text-[var(--text-low)]"}>
          {shipped ? String(rec.value) : "(blank)"}
        </span>
        <span className={pill} style={{ color: shipped ? "var(--confirmed)" : "var(--text-low)" }}>
          {rec.status}
        </span>
      </div>

      {shipped ? (
        <>
          <p className="mt-1 text-xs text-[var(--text-mid)]">{how.summary}</p>
          <p className="mono mt-0.5 text-[11px] text-[var(--text-low)]">
            {meta}
            {how.source_url ? (
              <>
                {" · "}
                <a
                  href={how.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="underline hover:text-[var(--text-mid)]"
                >
                  source
                </a>
              </>
            ) : null}
          </p>
          {(how.also_produced_by ?? []).map((p, i) => (
            <p key={i} className="mono mt-0.5 text-[11px] text-[var(--text-low)]">
              also from {p.source_class ?? p.source_url}
            </p>
          ))}
        </>
      ) : rec.blank_reason ? (
        // Why a cell is empty is the half of the story a spreadsheet cannot tell.
        <p className="mt-1 text-xs text-[var(--text-mid)]">
          <span className="mono text-[var(--partial)]">{rec.blank_reason.code}</span>
          {rec.blank_reason.detail ? ` — ${rec.blank_reason.detail}` : null}
        </p>
      ) : null}

      {(rec.tool_calls ?? []).length > 0 && (
        <ul className="mono mt-1 list-disc pl-4 text-[11px] text-[var(--text-low)]">
          {rec.tool_calls!.slice(0, 6).map((tc, i) => (
            <li key={i}>
              {tc.tool} → {tc.result_summary}
              {tc.ok === false ? " [failed]" : ""}
              {tc.cache_hit ? " [cached]" : ""}
            </li>
          ))}
        </ul>
      )}
      {(rec.alternatives ?? []).length > 0 && (
        <ul className="mono mt-1 list-disc pl-4 text-[11px] text-[var(--text-low)]">
          {rec.alternatives!.slice(0, 6).map((a, i) => (
            <li key={i}>
              rejected: {String(a.value)} — {a.why_not_used}
              {a.source_class ? ` (${a.source_class})` : ""}
            </li>
          ))}
        </ul>
      )}
      {dedupedChecks.length > 0 && (
        <ul className="mono mt-1 list-disc pl-4 text-[11px] text-[var(--text-low)]">
          {dedupedChecks.slice(0, 4).map((c, i) => (
            <li key={i}>
              check {c.check_id}: {c.severity}
              {c.detail ? ` — ${c.detail}` : ""}
              {c.count > 1 ? ` (×${c.count})` : ""}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function LeadRow({ runId, lead }: { runId: string; lead: Lead }) {
  const [open, setOpen] = useState(false);
  const [records, setRecords] = useState<FieldRecord[] | null>(null);

  useEffect(() => {
    if (!open || records) return;
    fetch(`/api/log/runs?run_id=${encodeURIComponent(runId)}&entity_id=${encodeURIComponent(lead.entity_id)}`)
      .then((r) => r.json())
      .then((d) => setRecords(d.records ?? []))
      .catch(() => setRecords([]));
  }, [open, records, runId, lead.entity_id]);

  return (
    <div className="border-b border-[var(--edge-soft)] last:border-0">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 py-2 text-left text-sm hover:text-[var(--text-hi)]"
      >
        <span className="text-[var(--text-hi)]">{lead.canonical_name ?? lead.entity_id}</span>
        <span className="mono text-[11px] text-[var(--text-low)]">{lead.entity_id}</span>
        <span className="flex-1" />
        <span className="mono text-[11px] text-[var(--text-mid)]">
          {lead.shipped_count}/{lead.field_count} fields populated
        </span>
      </button>
      {open && (
        <div className="pb-3 pl-3">
          {records === null ? (
            <p className="text-xs text-[var(--text-low)]">loading field log…</p>
          ) : records.length === 0 ? (
            <p className="text-xs text-[var(--text-low)]">No field log for this lead in this run.</p>
          ) : (
            records.map((rec) => <FieldLog key={rec.field} rec={rec} />)
          )}
        </div>
      )}
    </div>
  );
}

function RunRow({ run }: { run: Run }) {
  const [open, setOpen] = useState(false);
  const [leads, setLeads] = useState<Lead[] | null>(null);
  const notes = (run.notes ?? {}) as Record<string, unknown>;
  const termination = notes.termination as string | undefined;

  useEffect(() => {
    if (!open || leads) return;
    fetch(`/api/log/runs?run_id=${encodeURIComponent(run.run_id)}`)
      .then((r) => r.json())
      .then((d) => setLeads(d.leads ?? []))
      .catch(() => setLeads([]));
  }, [open, leads, run.run_id]);

  return (
    <div className="border-b border-[var(--edge)] last:border-0">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full flex-wrap items-center gap-3 py-3 text-left text-sm hover:text-[var(--text-hi)]"
      >
        <span className="mono tabular-nums text-[var(--text-hi)]">{fmt(run.started_at)}</span>
        <span className="mono text-[11px] text-[var(--text-low)]">
          {run.kind}
          {notes.schedule_name ? ` · ${notes.schedule_name}` : ""}
        </span>
        <span className={pill} style={{ color: run.status === "done" ? "var(--confirmed)" : "var(--urgent)" }}>
          {run.status}
        </span>
        {termination && (
          <span className={pill} style={{ color: "var(--text-mid)" }}>
            {termination}
          </span>
        )}
        <span className="flex-1" />
        <span className="mono text-[11px] text-[var(--text-mid)]">
          {notes.processed != null ? `${notes.processed} processed` : `${run.entity_count} queued`}
          {notes.confirmed != null ? ` · ${notes.confirmed} confirmed` : ""}
        </span>
      </button>
      {open && (
        <div className="pb-3 pl-3">
          {leads === null ? (
            <p className="text-xs text-[var(--text-low)]">loading leads…</p>
          ) : leads.length === 0 ? (
            <p className="text-xs text-[var(--text-low)]">No lead provenance recorded for this run.</p>
          ) : (
            leads.map((lead) => <LeadRow key={lead.entity_id} runId={run.run_id} lead={lead} />)
          )}
        </div>
      )}
    </div>
  );
}

function LiveBanner({ run }: { run: Run }) {
  const n = (run.notes ?? {}) as Record<string, unknown>;
  const target = n.target_confirmed as number | null;
  const confirmed = (n.confirmed as number) ?? 0;
  const inFlight = (n.in_flight as string[]) ?? [];
  const pct = target ? Math.min(100, Math.round((confirmed / target) * 100)) : null;
  return (
    <div className="mb-5 rounded-[var(--r-md)] border border-[var(--live)] bg-[var(--bg-glass-hi)] p-4">
      <div className="mono mb-2 flex flex-wrap items-center gap-3 text-xs">
        <span className="inline-flex items-center gap-1.5 text-[var(--live)]">
          <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-[var(--live)]" />
          RUNNING NOW
        </span>
        <span className="text-[var(--text-mid)]">{(n.schedule_name as string) ?? run.kind}</span>
      </div>
      <div className="mono flex flex-wrap gap-4 text-sm text-[var(--text-hi)]">
        <span>{(n.processed as number) ?? 0} processed</span>
        <span>
          {confirmed}
          {target ? `/${target}` : ""} confirmed
        </span>
        <span>{(n.failed as number) ?? 0} failed</span>
        <span>${((n.usd_spent as number) ?? 0).toFixed(4)}</span>
      </div>
      {/* A bar only means something against a target; without one the run goes until the
          queue empties and a filling bar would invent a finish line. */}
      {pct !== null && (
        <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-[var(--bg-base)]">
          <div className="h-full bg-[var(--confirmed)] transition-all" style={{ width: `${pct}%` }} />
        </div>
      )}
      <p className="mono mt-2 text-[11px] text-[var(--text-low)]">
        {inFlight.length ? `researching: ${inFlight.join(" · ")}` : "between leads…"}
      </p>
    </div>
  );
}

export default function LogPage() {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [notSynced, setNotSynced] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      fetch("/api/log/runs")
        .then((r) => r.json())
        .then((d) => {
          if (cancelled) return;
          setRuns(d.runs ?? []);
          setNotSynced(Boolean(d.not_synced_yet));
        })
        .catch(() => !cancelled && setRuns([]));
    load();
    // Poll while a run is in flight so the banner tracks it. The agent pushes progress
    // once per completed lead, and a lead takes minutes — 15s is well inside that.
    const t = setInterval(load, 15000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  const liveRun = (runs ?? []).find((r) => r.status === "running");

  return (
    <div className="mx-auto flex min-h-screen w-full max-w-5xl flex-col px-4 sm:px-6">
      <header className="sticky top-0 z-20 flex items-center justify-between border-b border-[var(--edge)] bg-[var(--bg-base)]/90 py-3 backdrop-blur">
        <div>
          <p className="mono text-[10px] uppercase tracking-[0.2em] text-[var(--text-low)]">
            FO Intelligence Agent
          </p>
          <h1 className="mono text-lg font-semibold text-[var(--text-hi)]">Run log</h1>
        </div>
        <a
          href="/"
          className="mono rounded-[var(--r-sm)] border border-[var(--edge)] px-3 py-1.5 text-xs text-[var(--text-mid)] hover:text-[var(--text-hi)]"
        >
          Search
        </a>
      </header>

      <main className="flex-1 py-6">
        <p className="mb-5 max-w-2xl text-sm text-[var(--text-mid)]">
          Every run the agent has made, and for each lead it touched, how every field was
          obtained — the tool call behind it, the source it came from, the values that lost, and
          for a blank cell, why it is blank.
        </p>

        {liveRun && <LiveBanner run={liveRun} />}

        {runs === null ? (
          <p className="text-sm text-[var(--text-low)]">loading…</p>
        ) : notSynced || runs.length === 0 ? (
          <div className="rounded-[var(--r-md)] border border-[var(--edge)] bg-[var(--bg-glass)] p-4">
            <p className="text-sm text-[var(--text-mid)]">No runs have been pushed yet.</p>
            <p className="mono mt-2 text-[11px] text-[var(--text-low)]">
              The agent runs locally and mirrors its log here after each run. Push manually with:
              POST /api/log/sync on the local service.
            </p>
          </div>
        ) : (
          <div className="rounded-[var(--r-md)] border border-[var(--edge)] bg-[var(--bg-glass)] px-4">
            {runs.map((run) => (
              <RunRow key={run.run_id} run={run} />
            ))}
          </div>
        )}
      </main>
    </div>
  );
}

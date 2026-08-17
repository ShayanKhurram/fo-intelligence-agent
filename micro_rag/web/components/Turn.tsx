"use client";

import { useState } from "react";
import type { Turn as TurnData, TurnView } from "@/lib/thread";
import { answerText, viewCounts } from "@/lib/thread";
import { removeFilterKey } from "@/lib/query-understanding";
import { useThread } from "./ThreadProvider";
import { StageStrip } from "./StageStrip";
import { FilterChips } from "./FilterChips";
import { RecordCard, RecordCardSkeleton } from "./RecordCard";
import { RankedList } from "./RankedList";
import { ClaimPill } from "./ClaimPill";
import { EvidenceList } from "./EvidenceList";
import { ShareIcon } from "./icons";

// T47.5 — one turn, and the views of it.
//
// Answer / Records / Evidence used to be tabs in the APPLICATION header, live and
// selectable before a question had been asked, where they selected nothing. They describe
// views of a single result, so they belong to that result. Two turns in one thread can now
// sit on different views at once — impossible under a global mode.
//
// The control is rendered only for views that have content, and each carries its count, so
// it never advertises an empty tab and the user can tell whether a view is worth opening.

const VIEW_LABEL: Record<Exclude<TurnView, "answer">, string> = {
  records: "Records",
  ranked: "Ranked",
  evidence: "Evidence",
};

function availableViews(turn: TurnData): Exclude<TurnView, "answer">[] {
  const c = viewCounts(turn);
  const out: Exclude<TurnView, "answer">[] = [];
  if (c.records > 0) out.push("records");
  if (c.ranked > 0) out.push("ranked");
  if (c.evidence > 0) out.push("evidence");
  return out;
}

export function Turn({ turn, focusIndex }: { turn: TurnData; focusIndex?: number }) {
  const { setView, toggleStages, rerunWithFilters, openEvidence } = useThread();
  const [copied, setCopied] = useState(false);

  const streaming = turn.status === "streaming";
  const counts = viewCounts(turn);
  const views = availableViews(turn);
  const active = views.includes(turn.view as Exclude<TurnView, "answer">)
    ? (turn.view as Exclude<TurnView, "answer">)
    : views[0];

  const stageSummary =
    turn.stages.length > 0
      ? `${turn.records.length} record${turn.records.length === 1 ? "" : "s"} · ${turn.claims.length} field${
          turn.claims.length === 1 ? "" : "s"
        }`
      : null;

  async function share() {
    try {
      await navigator.clipboard.writeText(`${turn.query}\n\n${answerText(turn)}`);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      // clipboard permission denied — nothing else to fall back to
    }
  }

  function removeFilter(key: string) {
    rerunWithFilters(turn.id, removeFilterKey(turn.activeFilters, key));
  }

  return (
    <article id={`turn-${turn.id}`} className="turn-enter scroll-mt-4 py-6 first:pt-2">
      {/* ---- the question ---- */}
      <div className="mb-4 flex justify-end">
        <h2 className="mono max-w-[85%] rounded-[var(--r-pill)] bg-[var(--bg-raised)] px-3.5 py-1.5 text-right text-xs leading-relaxed text-[var(--text-mid)]">
          {turn.query}
        </h2>
      </div>

      {/* ---- the answer. No bubble: a hairline rule makes the turn one unit while its
              contents stay free to use the whole measure. ---- */}
      <div className="border-l border-[var(--edge)] pl-4 sm:pl-5">
        <StageStrip
          stages={turn.stages}
          collapsed={turn.stagesCollapsed}
          summary={stageSummary}
          onToggle={() => toggleStages(turn.id)}
        />

        <FilterChips chips={turn.filterChips} onRemove={removeFilter} />

        {turn.relaxedFilters.length > 0 && (
          <div className="mb-4 flex flex-wrap gap-2">
            {turn.relaxedFilters.map((f) => (
              <span
                key={f}
                className="status-partial mono rounded-[var(--r-pill)] border border-[var(--partial)]/30 px-3 py-1 text-xs"
              >
                relaxed: {f}
              </span>
            ))}
          </div>
        )}

        {/* The prose. It is always visible — the view control below switches what
            accompanies it, never whether the answer itself is on screen. */}
        <div className="leading-relaxed">
          {turn.discarded ? (
            <p className="text-[var(--text-mid)]">{turn.fallbackMessage}</p>
          ) : (
            <p>
              {turn.segments.map((seg, i) =>
                seg.claim ? (
                  <span key={i}>
                    {seg.text} <ClaimPill claim={seg.claim} onOpen={openEvidence} />
                  </span>
                ) : (
                  <span key={i}>{seg.text} </span>
                )
              )}
              {streaming && <span className="stream-cursor" aria-hidden="true" />}
              {!streaming && turn.fallbackMessage && !turn.discarded && turn.segments.length === 0 && (
                <span className={turn.status === "error" || turn.declined ? "text-[var(--text-mid)]" : "display text-lg"}>
                  {turn.fallbackMessage}
                </span>
              )}
            </p>
          )}
        </div>

        {turn.status === "stopped" && (
          <p className="mono mt-3 text-xs text-[var(--partial)]">
            ◐ Stopped — this answer is incomplete.
          </p>
        )}

        {/* T47.7 (finding 09) — the announcement is the OUTCOME, not the prose.
            `aria-live` used to wrap the whole streaming paragraph, so assistive tech
            re-announced the accumulated text on every token. Stage transitions are
            announced by StageStrip's own live region; this one fires once, at the end. */}
        <p className="sr-only" role="status" aria-live="polite">
          {turn.status === "done"
            ? `Answer complete. ${turn.records.length} records, ${turn.claims.length} verified fields.`
            : turn.status === "stopped"
              ? "Generation stopped. The partial answer is shown."
              : turn.status === "error"
                ? "The search could not be completed."
                : ""}
        </p>

        {/* ---- the view control + utilities ---- */}
        {(views.length > 0 || turn.status !== "streaming") && (
          <div className="mt-5 flex flex-wrap items-center gap-1.5">
            {views.map((v) => {
              const isActive = v === active;
              const n = v === "records" ? counts.records : v === "ranked" ? counts.ranked : counts.evidence;
              return (
                <button
                  key={v}
                  onClick={() => setView(turn.id, v)}
                  aria-pressed={isActive}
                  className={`mono rounded-[var(--r-pill)] px-3 py-1 text-[11px] transition-colors ${
                    isActive
                      ? "bg-[var(--bg-raised)] text-[var(--text-hi)]"
                      : "text-[var(--text-low)] hover:text-[var(--text-mid)]"
                  }`}
                >
                  {VIEW_LABEL[v]} <span className="tabular-nums">· {n}</span>
                </button>
              );
            })}
            {turn.candidateCount != null && (
              <span className="mono ml-auto text-[11px] text-[var(--text-low)]">
                {turn.candidateCount} candidate{turn.candidateCount === 1 ? "" : "s"}
              </span>
            )}
            {turn.status !== "streaming" && (
              <button
                onClick={share}
                className="mono flex items-center gap-1.5 rounded-[var(--r-sm)] px-2 py-1 text-[11px] text-[var(--text-low)] hover:text-[var(--text-hi)]"
              >
                <ShareIcon /> {copied ? "Copied" : "Copy"}
              </button>
            )}
          </div>
        )}

        {/* ---- the active view ---- */}
        <div className="mt-4">
          {turn.recordsLoading && turn.records.length === 0 && (
            <div className="space-y-3">
              {Array.from({ length: 3 }).map((_, i) => (
                <RecordCardSkeleton key={i} variant="main" />
              ))}
            </div>
          )}

          {active === "records" && (
            <div className="space-y-3">
              {turn.records.map((r, i) => (
                <RecordCard
                  key={r.record_id}
                  record={r}
                  variant="main"
                  onOpen={openEvidence}
                  focused={focusIndex === i}
                />
              ))}
            </div>
          )}

          {active === "ranked" && (
            <RankedList
              rows={turn.planRows}
              excluded={turn.excluded}
              truncated={turn.truncated}
              sweptTotal={turn.sweptTotal}
              sweptConsidered={turn.sweptConsidered}
              onOpen={openEvidence}
            />
          )}

          {active === "evidence" && (
            <EvidenceList
              claims={turn.claims}
              entityNames={new Map(turn.records.map((r) => [r.record_id, r.entity_name]))}
            />
          )}
        </div>
      </div>
    </article>
  );
}

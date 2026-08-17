"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useThread } from "./ThreadProvider";
import { EvidenceContent } from "./EvidenceContent";
import { useMediaQuery } from "@/lib/use-media-query";
import { CloseIcon } from "./icons";

// T47.6 — the evidence surface, in the three forms the width calls for.
//
//   <640     bottom sheet, drag handle, 55% / 92% snap points
//   640-1439 right drawer, 420px, overlaying the thread (unchanged behaviour)
//   ≥1440    a PINNED panel that pushes the thread instead of covering it
//
// The pinned form is the point. Finding 04 of the review was that the drawer scrims the
// answer, so the one moment a user needs the claim and its provenance side by side is the
// moment the design hides the claim. Above 1440 there is room for both, so it takes it.
//
// Focus trap, Escape-to-close and focus-return are shared by all three forms — they were
// correct before T47 and are not re-implemented per form.
//
// All three are OPAQUE (--bg-raised), not glass, which departs from ui_plan.md §3's list
// of three blurred surfaces. Glass composites to ~94% opacity here, and the remaining 6%
// of the page ghosting through was legible behind the panel — tolerable on a records rail,
// wrong on the one surface whose whole job is reading dense provenance and source URLs.
// The rail and the composer keep their glass. Being opaque also makes the three forms
// visually identical, which is correct: they are one surface, not three.

const SNAP_HALF = 55;
const SNAP_FULL = 92;
const DISMISS_PX = 110;

export function EvidenceSurface() {
  const { evidence, closeEvidence, returnFocusRef } = useThread();
  const isWide = useMediaQuery("(min-width: 1440px)");
  const isTabletUp = useMediaQuery("(min-width: 640px)");

  if (!evidence) return null;
  const mode = isWide ? "pinned" : isTabletUp ? "drawer" : "sheet";

  return (
    <Surface
      key={evidence.recordId}
      mode={mode}
      recordId={evidence.recordId}
      focusField={evidence.field}
      onClose={closeEvidence}
      returnFocusRef={returnFocusRef}
    />
  );
}

function Surface({
  mode,
  recordId,
  focusField,
  onClose,
  returnFocusRef,
}: {
  mode: "pinned" | "drawer" | "sheet";
  recordId: string;
  focusField?: string | null;
  onClose: () => void;
  returnFocusRef: React.RefObject<HTMLElement | null>;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [snap, setSnap] = useState<number>(SNAP_HALF);
  const [dragY, setDragY] = useState(0);
  // `dragging` is state, not a ref, because the render below reads it to decide whether
  // the sheet animates: a transition during a drag fights the finger.
  const [dragging, setDragging] = useState(false);
  const dragStart = useRef<number | null>(null);

  // Focus trap + Escape + return focus. The pinned panel does not trap Tab — it is not a
  // modal, it sits beside the thread and the user must be able to tab back into the
  // answer it is explaining — but it still closes on Escape and still returns focus.
  useEffect(() => {
    const returnTo = returnFocusRef.current;
    closeRef.current?.focus();

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (mode === "pinned" || e.key !== "Tab" || !panelRef.current) return;
      const focusable = panelRef.current.querySelectorAll<HTMLElement>(
        'button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])'
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
  }, [mode]);

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    dragStart.current = e.clientY;
    setDragging(true);
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  }, []);

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    if (dragStart.current === null) return;
    setDragY(Math.max(-160, e.clientY - dragStart.current));
  }, []);

  const onPointerUp = useCallback(() => {
    if (dragStart.current === null) return;
    const dy = dragY;
    dragStart.current = null;
    setDragging(false);
    setDragY(0);
    if (dy > DISMISS_PX) {
      // From full height a downward drag steps to half rather than dismissing outright —
      // one gesture, one step, so the sheet is never lost by accident.
      if (snap === SNAP_FULL) setSnap(SNAP_HALF);
      else onClose();
    } else if (dy < -60) {
      setSnap(SNAP_FULL);
    }
  }, [dragY, snap, onClose]);

  const header = (
    <div className="mb-4 flex items-center justify-between gap-2">
      <span className="mono text-[10px] uppercase tracking-[0.16em] text-[var(--text-low)]">Evidence</span>
      <button
        ref={closeRef}
        onClick={onClose}
        className="flex items-center gap-1.5 rounded-[var(--r-sm)] px-2 py-1 text-sm text-[var(--text-mid)] hover:text-[var(--text-hi)]"
      >
        <CloseIcon /> Close
      </button>
    </div>
  );

  // ---- pinned: in flow, pushes the thread, no scrim, not modal ----
  if (mode === "pinned") {
    return (
      <aside
        ref={panelRef}
        aria-label={`Evidence for record ${recordId}`}
        className="drawer-enter sticky top-0 h-[100dvh] w-[420px] shrink-0 overflow-y-auto border-l border-[var(--edge)] bg-[var(--bg-raised)] p-6"
      >
        {header}
        <EvidenceContent recordId={recordId} focusField={focusField} />
      </aside>
    );
  }

  // ---- drawer: fixed right overlay ----
  if (mode === "drawer") {
    return (
      <div className="fixed inset-0 z-50 flex justify-end" role="presentation">
        <div className="scrim absolute inset-0" onClick={onClose} aria-hidden="true" />
        <div
          ref={panelRef}
          role="dialog"
          aria-modal="true"
          aria-label={`Evidence for record ${recordId}`}
          className="drawer-enter relative flex h-full w-full max-w-[420px] flex-col overflow-y-auto border-l border-[var(--edge)] bg-[var(--bg-raised)] p-6 shadow-[var(--shadow-drawer)]"
        >
          {header}
          <EvidenceContent recordId={recordId} focusField={focusField} />
        </div>
      </div>
    );
  }

  // ---- sheet: bottom, draggable ----
  return (
    <div className="fixed inset-0 z-50 flex items-end" role="presentation">
      <div className="scrim absolute inset-0" onClick={onClose} aria-hidden="true" />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={`Evidence for record ${recordId}`}
        style={{
          height: `${snap}dvh`,
          transform: dragY ? `translateY(${dragY}px)` : undefined,
          transition: dragging
            ? undefined
            : "height 200ms cubic-bezier(0.32,0.72,0,1), transform 200ms cubic-bezier(0.32,0.72,0,1)",
        }}
        className="sheet-enter relative flex w-full flex-col rounded-t-[var(--r-lg)] border-t border-[var(--edge-lit)] bg-[var(--bg-raised)] shadow-[var(--shadow-drawer)]"
      >
        <button
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          onClick={() => setSnap((s) => (s === SNAP_HALF ? SNAP_FULL : SNAP_HALF))}
          aria-label={snap === SNAP_HALF ? "Expand evidence" : "Collapse evidence"}
          className="flex w-full shrink-0 touch-none cursor-grab justify-center py-3 active:cursor-grabbing"
        >
          <span className="block h-1 w-10 rounded-full bg-[var(--text-low)]" aria-hidden="true" />
        </button>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-[max(1.25rem,env(safe-area-inset-bottom))]">
          {header}
          <EvidenceContent recordId={recordId} focusField={focusField} />
        </div>
      </div>
    </div>
  );
}

"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useThread } from "./ThreadProvider";
import { SendIcon, StopIcon } from "./icons";

// T47.3 — the composer.
//
// Three fixes over the single-line <input> it replaces:
//   1. It auto-grows to six lines, so a real question ("which offices took clean-energy
//      positions and have a confirmed principal email") can be re-read before sending.
//   2. Send becomes Stop while streaming, wired to a real AbortController. Before this
//      there was none — `loading` only disabled the button, and a wide sweep the user had
//      already decided against ran to completion.
//   3. It is DOCKED, not floating: it publishes its own height as --composer-h and the
//      thread pads by exactly that, so the last record card is never hidden underneath.
//
// The Enter/Shift+Enter split and the isComposing guard are carried over verbatim — the
// guard is what keeps Enter from submitting mid-composition in CJK input, and it is the
// one interaction a question box absolutely cannot get wrong.

const MAX_ROWS = 6;

export function Composer() {
  const { submit, stop, streamingId, turns } = useThread();
  const [value, setValue] = useState("");
  const taRef = useRef<HTMLTextAreaElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const streaming = streamingId !== null;

  // ---- auto-grow ----
  const resize = useCallback(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    const cs = window.getComputedStyle(ta);
    const line = parseFloat(cs.lineHeight) || 20;
    const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    const max = line * MAX_ROWS + pad;
    ta.style.height = `${Math.min(ta.scrollHeight, max)}px`;
    ta.style.overflowY = ta.scrollHeight > max ? "auto" : "hidden";
  }, []);

  useLayoutEffect(resize, [value, resize]);

  // ---- publish height so the thread can pad by it (never overlap the last turn) ----
  useEffect(() => {
    const el = formRef.current;
    if (!el) return;
    const apply = () => {
      document.documentElement.style.setProperty("--composer-h", `${el.offsetHeight + 24}px`);
    };
    apply();
    const ro = new ResizeObserver(apply);
    ro.observe(el);
    return () => {
      ro.disconnect();
      document.documentElement.style.removeProperty("--composer-h");
    };
  }, []);

  // "/" focuses the composer from anywhere — ui_plan.md §8, preserved.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      const target = e.target as HTMLElement;
      const typing = target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable;
      if (e.key === "/" && !typing) {
        e.preventDefault();
        taRef.current?.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  function send() {
    if (!value.trim() || streaming) return;
    submit(value);
    setValue("");
  }

  const first = turns.length === 0;

  return (
    <div
      className="sticky bottom-0 z-30 w-full px-4 pb-[max(1rem,env(safe-area-inset-bottom))] pt-2 sm:px-6"
      style={{ background: "linear-gradient(to top, var(--bg-base) 62%, transparent)" }}
    >
      <form
        ref={formRef}
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
        className="glass mx-auto flex w-full max-w-[42rem] items-end gap-2 rounded-[var(--r-lg)] px-3 py-2.5 shadow-[var(--shadow-drawer)] focus-within:ring-2 focus-within:ring-[color-mix(in_srgb,var(--live)_40%,transparent)] lg:max-w-[46rem]"
      >
        <textarea
          id="composer-input"
          ref={taRef}
          rows={1}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            // Explicit handler rather than relying on native submit — more robust across
            // browsers and IME composition.
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              send();
            }
          }}
          // The wording is load-bearing. `/api/query` is stateless, so this is a log of
          // independent questions, not a conversation — calling it "Reply" would promise
          // memory the backend does not have (T47.9).
          placeholder={first ? "Ask about a family office…" : "Ask a new question…"}
          aria-label="Ask a question about a family office"
          aria-describedby="composer-hint"
          className="mt-0.5 min-h-0 w-full flex-1 resize-none bg-transparent py-1 text-sm leading-6 text-[var(--text-hi)] placeholder:text-[var(--text-low)] focus:outline-none"
        />
        {streaming ? (
          <button
            type="button"
            onClick={stop}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[var(--urgent)] text-[var(--bg-base)] transition-opacity hover:opacity-90"
            aria-label="Stop generating"
            title="Stop generating"
          >
            <StopIcon />
          </button>
        ) : (
          <button
            type="submit"
            disabled={!value.trim()}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[var(--confirmed)] text-[var(--bg-base)] transition-opacity disabled:opacity-30"
            aria-label="Ask question"
          >
            <SendIcon />
          </button>
        )}
      </form>
      {/* The keyboard half of the hint is hidden below 640, where there is no physical
          Enter key to describe and it wrapped to a second line. */}
      <p id="composer-hint" className="mx-auto mt-1.5 max-w-[42rem] text-center text-[11px] text-[var(--text-low)] lg:max-w-[46rem]">
        <span className="hidden sm:inline">Enter to ask · Shift+Enter for a new line · </span>
        filters are read from your question
      </p>
    </div>
  );
}

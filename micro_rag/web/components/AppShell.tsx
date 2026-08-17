"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useThread } from "./ThreadProvider";
import { EvidenceSurface } from "./EvidenceSurface";
import { turnLabel } from "@/lib/thread";
import { CloseIcon, MenuIcon, PlusIcon, AskIcon, WatchIcon, LogIcon } from "./icons";

// T47.1 — the application shell.
//
// Before this, `/`, `/watch` and `/log` each rebuilt their own `mx-auto max-w-5xl`
// container and their own sticky header, and navigation between them was two small
// bordered mono links sitting among the utility actions — styled exactly like Share, so
// the product's three major surfaces read as secondary buttons. One shell now owns all
// three, and the destinations live in a rail where they read as destinations.
//
// Layout contract (T47.4):
//   rail      off-canvas <1024 · pinned 260px ≥1024
//   evidence  bottom sheet <640 · overlay 420px 640-1439 · pinned/pushes ≥1440

const NAV = [
  { href: "/", label: "Ask", Icon: AskIcon },
  { href: "/watch", label: "Watch", Icon: WatchIcon },
  { href: "/log", label: "Log", Icon: LogIcon },
] as const;

/**
 * `router.push` resolves before the destination has painted, so an element on the page
 * being navigated TO does not exist yet. Polls for it across a bounded number of frames
 * rather than guessing a timeout, and gives up silently if it never appears.
 */
function whenPresent(id: string, fn: (el: HTMLElement) => void, framesLeft = 60) {
  requestAnimationFrame(() => {
    const el = document.getElementById(id);
    if (el) fn(el);
    else if (framesLeft > 0) whenPresent(id, fn, framesLeft - 1);
  });
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const [railOpen, setRailOpen] = useState(false);
  const pathname = usePathname();
  const router = useRouter();
  const { turns, clear } = useThread();

  const closeRail = useCallback(() => setRailOpen(false), []);

  useEffect(() => {
    if (!railOpen) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setRailOpen(false);
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [railOpen]);

  // Navigation between routes MUST go through the router, never location.href: the thread
  // lives in a client provider above the router, so a full page load would silently
  // destroy the session history the rail is showing.
  function goToTurn(id: string) {
    closeRail();
    if (pathname !== "/") {
      router.push("/");
      whenPresent(`turn-${id}`, (el) => el.scrollIntoView({ behavior: "smooth", block: "start" }));
      return;
    }
    document.getElementById(`turn-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function newQuestion() {
    closeRail();
    clear();
    if (pathname !== "/") {
      router.push("/");
      whenPresent("composer-input", (el) => el.focus());
      return;
    }
    requestAnimationFrame(() => document.getElementById("composer-input")?.focus());
  }

  return (
    <div className="flex min-h-[100dvh]">
      {/* ---- off-canvas backdrop (below 1024 only) ---- */}
      {railOpen && (
        <div
          className="scrim fixed inset-0 z-40 lg:hidden"
          onClick={() => setRailOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* ---- rail ---- */}
      <aside
        id="app-rail"
        // `invisible` when closed, not just translated off-screen: a transform alone
        // leaves every control in the tab order, so a keyboard user would tab into a rail
        // they cannot see. It is restored unconditionally at ≥1024, where the rail is
        // pinned and `railOpen` no longer means anything.
        className={`fixed inset-y-0 left-0 z-50 flex w-[280px] flex-col border-r border-[var(--edge)] bg-[var(--bg-raised)] transition-transform duration-200 ease-out lg:static lg:z-auto lg:w-[260px] lg:shrink-0 lg:translate-x-0 lg:bg-[var(--bg-base)] ${
          railOpen ? "visible translate-x-0" : "invisible -translate-x-full lg:visible"
        }`}
        aria-label="Main navigation"
      >
        <div className="flex items-center justify-between gap-2 px-4 py-4">
          <Link href="/" onClick={closeRail} className="min-w-0">
            <p className="mono text-[10px] uppercase tracking-[0.2em] text-[var(--text-low)]">FO Intelligence</p>
            <p className="display truncate text-sm text-[var(--text-hi)]">Family office records</p>
          </Link>
          <button
            onClick={() => setRailOpen(false)}
            className="shrink-0 rounded-[var(--r-sm)] p-1.5 text-[var(--text-mid)] hover:text-[var(--text-hi)] lg:hidden"
            aria-label="Close navigation"
          >
            <CloseIcon />
          </button>
        </div>

        <div className="px-3 pb-3">
          <button
            onClick={newQuestion}
            className="flex w-full items-center gap-2 rounded-[var(--r-sm)] border border-[var(--edge)] px-3 py-2 text-sm text-[var(--text-mid)] transition-colors hover:border-[var(--edge-lit)] hover:text-[var(--text-hi)]"
          >
            <PlusIcon />
            New question
          </button>
        </div>

        <nav className="flex flex-col gap-0.5 px-3" aria-label="Sections">
          {NAV.map(({ href, label, Icon }) => {
            const current = pathname === href;
            return (
              <Link
                key={href}
                href={href}
                onClick={closeRail}
                aria-current={current ? "page" : undefined}
                className={`flex items-center gap-2.5 rounded-[var(--r-sm)] px-3 py-2 text-sm transition-colors ${
                  current
                    ? "bg-[var(--bg-glass-hi)] text-[var(--text-hi)]"
                    : "text-[var(--text-mid)] hover:text-[var(--text-hi)]"
                }`}
              >
                <Icon />
                {label}
              </Link>
            );
          })}
        </nav>

        {/* ---- thread history ---- */}
        {turns.length > 0 && (
          <div className="no-scrollbar mt-5 min-h-0 flex-1 overflow-y-auto px-3 pb-4">
            <p className="mono mb-2 px-3 text-[10px] uppercase tracking-[0.14em] text-[var(--text-low)]">
              This session
            </p>
            <ul className="flex flex-col gap-0.5">
              {turns.map((t) => (
                <li key={t.id}>
                  <button
                    onClick={() => goToTurn(t.id)}
                    className="w-full truncate rounded-[var(--r-sm)] px-3 py-1.5 text-left text-[13px] text-[var(--text-mid)] transition-colors hover:bg-[var(--bg-glass)] hover:text-[var(--text-hi)]"
                    title={t.query}
                  >
                    {turnLabel(t)}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        <p className="mono mt-auto px-6 py-4 text-[10px] leading-relaxed text-[var(--text-low)]">
          Assembled from public obligation filings — 13F, 990-PF, Form 5500, Form D.
        </p>
      </aside>

      {/* ---- main column + evidence ---- */}
      <div className="flex min-w-0 flex-1 flex-col">
        {/* The top bar exists only below 1024, where the rail is off-canvas and something
            has to hold the trigger. At ≥1024 the rail carries the wordmark itself. */}
        <div className="sticky top-0 z-30 flex items-center gap-2 border-b border-[var(--edge)] bg-[var(--bg-base)]/90 px-4 py-2.5 backdrop-blur lg:hidden">
          <button
            onClick={() => setRailOpen(true)}
            className="rounded-[var(--r-sm)] p-1.5 text-[var(--text-mid)] hover:text-[var(--text-hi)]"
            aria-label="Open navigation"
            aria-expanded={railOpen}
            aria-controls="app-rail"
          >
            <MenuIcon />
          </button>
          <p className="mono text-[11px] uppercase tracking-[0.18em] text-[var(--text-low)]">FO Intelligence</p>
        </div>

        <div className="flex min-h-0 min-w-0 flex-1">
          <main className="flex min-w-0 flex-1 flex-col">{children}</main>
          <EvidenceSurface />
        </div>
      </div>
    </div>
  );
}

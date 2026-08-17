import { IntentWatcher } from "@/components/IntentWatcher";
import { loadWatchBoard } from "@/lib/watch";
import type { WatchBoard } from "@/lib/watch";

// T46.4 — `/watch`. A server component that reads the board directly rather than having
// the client fetch `/api/watch`: `lib/db.ts` stays server-side (the UI never holds a DB
// credential), and the first paint carries the real 478 organizations instead of a
// skeleton that resolves a second later.
export const dynamic = "force-dynamic";

export default async function WatchPage() {
  let board: WatchBoard = { orgs: [] };
  let notReady = false;
  try {
    board = await loadWatchBoard();
  } catch (e) {
    // A missing relation means the watcher has never been initialised — a state, not a
    // fault, exactly as `app/api/log/runs/route.ts` treats an un-synced log.
    if (/relation .*(watch_|provenance|records).* does not exist/i.test(String(e))) {
      notReady = true;
    } else {
      throw e;
    }
  }

  if (notReady) {
    return (
      <div className="mx-auto w-full max-w-5xl px-4 py-16 sm:px-6">
        <h1 className="display mb-2 text-lg text-[var(--text-hi)]">Intent watcher</h1>
        <p className="text-sm text-[var(--text-mid)]">
          The corpus has not been ingested here yet, so there is no activity to watch.
        </p>
      </div>
    );
  }

  return <IntentWatcher board={board} />;
}

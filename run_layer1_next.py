"""Layer-1 triage over the next N un-triaged discovery leads (2026-07-29). Selects the
next N entity_ids in ingestion order that don't yet have a decision, runs the Layer-1
graph over them, and prints the verdict distribution. Throwaway driver."""
from __future__ import annotations

import asyncio
import logging
import sys
import time

from app.db import connection, get_entity, init_db
from app.ingest import ingest_discovery_file
from app.runner import run_batch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

JSONL_PATH = "data/fo_discovery_c1-c3-c4-c8_20260728T090511Z.jsonl"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 200


async def main() -> None:
    init_db()
    with connection() as conn:
        order = ingest_discovery_file(conn, JSONL_PATH)  # idempotent; returns full order
        done = {r[0] for r in conn.execute("SELECT DISTINCT entity_id FROM decisions").fetchall()}

    todo = [eid for eid in order if eid not in done][:N]
    print(f"Discovery pool: {len(order)} entities; {len(done)} already triaged; "
          f"running next {len(todo)}.")

    t0 = time.monotonic()
    result = await run_batch(entity_ids=todo, resume=False, skip_preflight=False)
    elapsed = time.monotonic() - t0

    print()
    print(f"=== LAYER-1 BATCH DONE in {elapsed:.1f}s ===")
    print(f"processed: {len(result.processed)}  failed: {len(result.failed)}  "
          f"total_cost_usd: {result.total_cost_usd:.4f}  budget_aborted: {result.budget_aborted}")

    with connection() as conn:
        rows = conn.execute(
            "SELECT verdict, COUNT(*) n FROM (SELECT entity_id, verdict, MAX(id) FROM decisions "
            "WHERE entity_id IN (%s) GROUP BY entity_id) GROUP BY verdict" % ",".join("?" * len(todo)),
            todo,
        ).fetchall() if todo else []
    print("verdict distribution (this batch):", {r[0]: r[1] for r in rows})

    # show the promising ones
    with connection() as conn:
        for eid in todo:
            d = conn.execute(
                "SELECT verdict FROM decisions WHERE entity_id=? ORDER BY id DESC LIMIT 1", (eid,)
            ).fetchone()
            if d and d[0] in ("pursue", "pursue_low"):
                e = get_entity(conn, eid)
                print(f"  {d[0]:>11}  {e['canonical_name']!r}")


if __name__ == "__main__":
    asyncio.run(main())

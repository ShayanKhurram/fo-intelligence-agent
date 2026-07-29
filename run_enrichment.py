"""One-off driver: run the enrichment/validation/dataset pipeline (Layer E/V/D) against
the pursue/pursue_low decisions already in data/foia.db, and print the resulting ship-rate
breakdown. Throwaway script for the post-gate-relaxation live re-run (2026-07-29)."""
from __future__ import annotations

import asyncio
import logging
import time

from app.db import connection, get_entity
from app.enrichment import run_pipeline
from app.llm import get_model

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def main() -> None:
    model = get_model("mid")
    print(f"Model: {type(model).__name__}")

    t0 = time.monotonic()
    with connection() as conn:
        # force=True: gates were relaxed (2026-07-29), so existing pursue/pursue_low leads
        # must be re-judged against the new rules rather than replaying their cached
        # (old-strict) outcome. concurrency=3 to stay under Ollama Cloud's concurrent cap.
        result = await run_pipeline(conn, model, target_survivors=50, concurrency=3, force=True)
    elapsed = time.monotonic() - t0

    processed = result["processed"]
    by_outcome: dict[str, int] = {}
    for r in processed:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1

    print()
    print(f"=== ENRICHMENT DONE in {elapsed:.1f}s ===")
    print(f"processed: {len(processed)}  survivors(ship/ship_with_caveats): {result['survivors']}  "
          f"reserve_draws: {result['reserve_draws']}  usd_spent: {result['usd_spent']:.4f}")
    print(f"outcome breakdown: {by_outcome}")

    print()
    print("=== PER-ENTITY ===")
    with connection() as conn:
        for r in sorted(processed, key=lambda x: x["outcome"]):
            eid = r["entity_id"]
            e = get_entity(conn, eid)
            name = e["canonical_name"] if e else "?"
            print(f"  {r['outcome']:>18}  {name!r}  (calls={r['calls_spent']}, ${r['usd_spent']:.4f})")


if __name__ == "__main__":
    asyncio.run(main())

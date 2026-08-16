#!/usr/bin/env python
"""T41 — backfill lead provenance for runs that lost theirs.

The problem this exists for: until T40, provenance emission lived only in the
clean-completion block of `run_scheduled_job`. A run that was interrupted, killed or
errored before it reached that block left **no** `field_provenance` rows at all — and
on the live database that was 9 of the 10 scheduled runs, including one with 95
confirmed leads. The Log tab showed those runs with no leads under them.

This script reconstructs the entity list a run touched from the two tables that record
work independently of the run finishing cleanly:

    SELECT entity_id FROM rag_queue   WHERE run_id = ?   -- confirmed leads
    UNION
    SELECT DISTINCT entity_id FROM tool_calls WHERE run_id = ?  -- everything worked on

then runs the same `build_run_log` -> `write_field_provenance` path the scheduler now
uses per-lead, and writes `data/runs/<run_id>/field_provenance.json`. It never touches a
run that already has rows unless `--force`. `--dry-run` reports what it would do without
writing.

Usage:

    python scripts/backfill_provenance.py --all-missing
    python scripts/backfill_provenance.py --run-id 2172fa07... --run-id ee6e7646...
    python scripts/backfill_provenance.py --all-missing --dry-run
    python scripts/backfill_provenance.py --run-id 2172fa07... --force

Do NOT run it while a scheduled run is active — one SQLite writer at a time. The script
does not check that for you (the scheduler is on a separate process), so check
`GET /api/scheduler/status` first.

Exit code 0 always; a per-run failure is logged and skipped, never fatal — one bad run
must not stop the backfill of the rest, for the same reason one bad lead must not stop a
scheduled run.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `app.*` importable when run as a script from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.config import SETTINGS
from app.db import connection, get_field_provenance, init_db, write_field_provenance
from app.dataset import _atomic_write_json
from app.log_sync import sync_runs
from app.provenance_log import build_run_log
from app.scheduler import _provenance_rows_from_doc


def _run_entity_ids(conn, run_id: str) -> list[str]:
    """The entities a run touched, reconstructed from rag_queue (confirmed leads) UNION
    tool_calls (everything it worked on). Ordered by entity_id for a stable output."""
    rows = conn.execute(
        """
        SELECT entity_id FROM rag_queue WHERE run_id = ?
        UNION
        SELECT DISTINCT entity_id FROM tool_calls WHERE run_id = ?
        ORDER BY entity_id
        """,
        (run_id, run_id),
    ).fetchall()
    return [r["entity_id"] for r in rows]


def _backfill_one(db_path: str, run_id: str, *, force: bool, dry_run: bool) -> dict:
    """Reconstruct + emit provenance for one run. Returns a summary dict; never raises."""
    with connection(db_path) as conn:
        existing = get_field_provenance(conn, run_id=run_id)
    if existing and not force:
        # A run that already has rows is left alone unless --force. --force re-emits
        # over the existing rows (upsert); the count reported below is the reconstructed
        # one, not the pre-existing one.
        return {"run_id": run_id, "skipped": True, "reason": "already has rows",
                "leads": 0, "field_rows": len(existing)}

    with connection(db_path) as conn:
        entity_ids = _run_entity_ids(conn, run_id)
    if not entity_ids:
        return {"run_id": run_id, "skipped": True, "reason": "no entities found",
                "leads": 0, "field_rows": 0}

    if dry_run:
        # Count the rows that would be written without actually building the doc — a
        # dry-run must not write, and build_run_log reads the run row anyway.
        return {"run_id": run_id, "skipped": False, "dry_run": True,
                "leads": len(entity_ids), "field_rows": "(dry-run)"}

    try:
        with connection(db_path) as conn:
            doc = build_run_log(conn, run_id, entity_ids)
            rows = _provenance_rows_from_doc(doc, run_id)
            write_field_provenance(conn, rows)
        _atomic_write_json(
            Path(db_path).parent / "runs" / run_id / "field_provenance.json", doc)
    except Exception as exc:  # noqa: BLE001 — one bad run must not stop the backfill
        return {"run_id": run_id, "skipped": True, "reason": f"error: {exc}",
                "leads": len(entity_ids), "field_rows": 0}

    return {"run_id": run_id, "skipped": False, "leads": len(entity_ids),
            "field_rows": len(rows)}


def _target_run_ids(conn, args) -> list[str]:
    if args.run_id:
        return list(args.run_id)
    if args.all_missing:
        # Every run that has no field_provenance rows yet. A run with rows is left alone
        # unless --force (handled per-run), so --all-missing is safe to re-run.
        all_runs = [r["run_id"] for r in conn.execute(
            "SELECT run_id FROM runs ORDER BY started_at DESC").fetchall()]
        missing = []
        for run_id in all_runs:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM field_provenance WHERE run_id = ?",
                (run_id,)).fetchone()["n"]
            if n == 0:
                missing.append(run_id)
        return missing
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill lead provenance for runs that lost theirs (T41).")
    parser.add_argument("--db", default=SETTINGS.db_path,
                        help="Path to the SQLite database (default: data/foia.db).")
    parser.add_argument("--run-id", action="append", default=[],
                        help="A run_id to backfill. Repeatable.")
    parser.add_argument("--all-missing", action="store_true",
                        help="Backfill every run that currently has no provenance rows.")
    parser.add_argument("--force", action="store_true",
                        help="Re-emit even for runs that already have rows (upserts).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be done without writing anything.")
    args = parser.parse_args(argv)

    if not args.run_id and not args.all_missing:
        parser.error("specify at least one of --run-id or --all-missing")

    db_path = args.db
    init_db(db_path)

    with connection(db_path) as conn:
        run_ids = _target_run_ids(conn, args)

    if not run_ids:
        print("no runs to backfill")
        return 0

    print(f"backfilling {len(run_ids)} run(s)" +
          (" (dry-run)" if args.dry_run else "") +
          (" (force)" if args.force else ""))

    total_leads = 0
    total_field_rows = 0
    written = 0
    for run_id in run_ids:
        summary = _backfill_one(db_path, run_id,
                                force=args.force, dry_run=args.dry_run)
        leads = summary["leads"]
        field_rows = summary["field_rows"]
        if summary.get("skipped"):
            print(f"  {run_id}: skipped — {summary['reason']}")
            continue
        if isinstance(field_rows, int):
            total_leads += leads
            total_field_rows += field_rows
            written += 1
        print(f"  {run_id}: {leads} leads, {field_rows} field rows")

    print(f"total: {written} run(s) written, {total_leads} leads, "
          f"{total_field_rows} field rows")

    # Mirror to the hosted view so the Log tab picks the rows up. Best-effort: sync_runs
    # never raises and skips cleanly when no DSN is configured. Skipped on --dry-run.
    if not args.dry_run and written:
        push = sync_runs(db_path, limit=50)
        print(f"sync_runs: {push.get('status')} "
              f"({push.get('runs', 0)} runs, {push.get('fields', 0)} fields)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
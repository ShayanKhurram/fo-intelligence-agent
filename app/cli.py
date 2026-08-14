"""CLI entrypoint. `python -m app.cli init-db` bootstraps the schema; `python -m app.cli
run [entity_id ...]` runs the batch runner — with no ids, picks up unstarted + resumable
leads from the DB (plan §5, §8 step 7: run the 10-lead pilot before a full batch)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

# Trace/entity content can contain characters outside Windows consoles' default cp1252
# codepage (e.g. non-ASCII punctuation from scraped web content); reconfigure stdout to
# UTF-8 with replacement rather than crashing mid-print. No-op on platforms where stdout
# is already UTF-8 or lacks reconfigure() (e.g. when stdout is piped/redirected on some
# setups) — guarded defensively.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import SETTINGS
from app.db import connection, get_entity, get_lead_trace, init_db
from app.runner import BatchResult, run_batch
from app.trace_viewer import format_trace


def _cmd_init_db(args: argparse.Namespace) -> None:
    init_db(args.db_path)
    print(f"Initialized schema at {args.db_path or SETTINGS.db_path}")


def _cmd_trace(args: argparse.Namespace) -> None:
    with connection(args.db_path) as conn:
        for entity_id in args.entity_ids:
            entity = get_entity(conn, entity_id)
            trace = get_lead_trace(conn, entity_id)
            if entity is None:
                print(f"===== {entity_id}: no such entity =====\n")
                continue
            if trace is None:
                print(f"===== {entity_id}  {entity['canonical_name']!r}: no trace recorded =====\n")
                continue
            print(format_trace(entity_id, entity["canonical_name"], trace))
            print()


def _cmd_run(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result: BatchResult = asyncio.run(
        run_batch(
            entity_ids=args.entity_ids or None,
            db_path=args.db_path,
            resume=not args.no_resume,
            skip_preflight=args.skip_preflight,
        )
    )
    print(f"processed={len(result.processed)} failed={len(result.failed)} "
          f"total_cost_usd={result.total_cost_usd:.4f} budget_aborted={result.budget_aborted}")
    if result.failed:
        print("failed entity_ids:", result.failed)


def _cmd_backfill_log(args: argparse.Namespace) -> None:
    """Write a provenance log for leads that were enriched BEFORE the log existed.

    The claim ledger already holds everything the log is composed from — this makes no
    tool calls, no LLM calls, and spends nothing; it is a read of work already done.
    Without it, every lead enriched before T35 is invisible in the Log tab despite being
    fully documented in the ledger.

    Recorded as a run of kind='backfill' rather than pretending to be a scheduled or
    layer1 run: the log must not claim these leads were processed at the moment the
    backfill happened to run.
    """
    import json

    from app.db import (
        connection,
        finish_run,
        get_claims,
        get_entity,
        start_run,
        write_field_provenance,
    )
    from app.provenance_log import build_run_log

    with connection(args.db_path) as conn:
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT entity_id, outcome,
                       ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY id DESC) AS rn
                FROM enrichment_runs
            )
            SELECT entity_id, outcome FROM latest WHERE rn = 1
            """
        ).fetchall()
        outcomes = [(r["entity_id"], r["outcome"]) for r in rows]
        if args.only_shipped:
            outcomes = [(e, o) for e, o in outcomes if o in ("ship", "ship_with_caveats")]
        if not outcomes:
            print("Nothing to backfill — no entity has an enrichment_runs row yet.")
            return

        run_id = start_run(conn, "backfill", entity_count=len(outcomes),
                           notes={"note": "provenance reconstructed from the existing claim "
                                          "ledger for leads enriched before the log existed"})
        doc = build_run_log(conn, run_id, outcomes)
        rows_out = []
        for lead in doc["leads"]:
            for rec in lead["fields"]:
                rows_out.append({
                    "run_id": run_id, "entity_id": lead["entity_id"], "field": rec["field"],
                    "value": rec["value"], "status": rec["status"], "shipped": rec["shipped"],
                    "source_class": (rec["how"] or {}).get("source_class"),
                    "extraction_method": (rec["how"] or {}).get("extraction_method"),
                    "record": json.dumps(rec, ensure_ascii=False, default=str),
                })
        write_field_provenance(conn, rows_out)
        shipped = sum(1 for _e, o in outcomes if o in ("ship", "ship_with_caveats"))
        finish_run(conn, run_id, status="done", notes={
            "note": "backfilled from the existing claim ledger; no tool or LLM calls were made",
            "processed": len(doc["leads"]), "confirmed": shipped,
            "termination": "backfill",
        })
    print(f"Backfilled run {run_id}: {len(doc['leads'])} leads, {len(rows_out)} field records "
          f"({shipped} confirmed).")


def _cmd_push_log(args: argparse.Namespace) -> None:
    """Mirror the local run log to Postgres so the hosted view shows it."""
    from app.log_sync import sync_runs

    print(sync_runs(args.db_path, limit=args.limit))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("--db-path", dest="db_path", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db")

    run_parser = sub.add_parser("run")
    run_parser.add_argument("entity_ids", nargs="*", help="Specific entity_ids to run (default: queue).")
    run_parser.add_argument("--no-resume", action="store_true", help="Skip picking up interrupted leads.")
    run_parser.add_argument("--skip-preflight", action="store_true", help="Skip the SERPER_API_KEY preflight check.")

    trace_parser = sub.add_parser("trace", help="Print the full reasoning/tool-call trace for one or more leads.")
    trace_parser.add_argument("entity_ids", nargs="+", help="entity_id(s) to show the trace for.")

    backfill_parser = sub.add_parser(
        "backfill-log",
        help="Write a provenance log for leads enriched before the log existed. Reads the "
             "claim ledger only — no tool calls, no LLM calls, no spend.")
    backfill_parser.add_argument("--only-shipped", action="store_true",
                                 help="Skip rejected leads (default: include them).")

    push_parser = sub.add_parser("push-log", help="Mirror the run log to Postgres for the hosted view.")
    push_parser.add_argument("--limit", type=int, default=50, help="How many recent runs to push.")

    args = parser.parse_args()
    if args.command == "init-db":
        _cmd_init_db(args)
    elif args.command == "run":
        _cmd_run(args)
    elif args.command == "trace":
        _cmd_trace(args)
    elif args.command == "backfill-log":
        _cmd_backfill_log(args)
    elif args.command == "push-log":
        _cmd_push_log(args)


if __name__ == "__main__":
    main()

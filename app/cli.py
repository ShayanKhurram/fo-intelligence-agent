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

    args = parser.parse_args()
    if args.command == "init-db":
        _cmd_init_db(args)
    elif args.command == "run":
        _cmd_run(args)
    elif args.command == "trace":
        _cmd_trace(args)


if __name__ == "__main__":
    main()

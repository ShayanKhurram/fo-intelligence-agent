#!/usr/bin/env python
"""T43.5 — corpus validator CLI.

Round 2 scope: --mode email runs the full backfill + durable write path
(backfill_principal_email -> record_verdict -> reproject_records), with per-row
checkpoints and a summary including the two T43.3 invariant checks. --mode type still
prints "lands in round 3" and writes nothing (classify_entity_type is round 3).
--dry-run makes NO external calls at all — no Serper, no Snov, no browser — and writes
nothing to either database. It prints the worklist and the plan (what the real run would
attempt, and the upper bound on spend). It is a costing tool, not a rehearsal: resolving
verdicts is the expensive part, so a dry run that resolved them would be the real run
minus the write.

Usage:

    python scripts/validate_corpus.py --mode type  --limit 10 --dry-run
    python scripts/validate_corpus.py --mode email --limit 10 --dry-run
    python scripts/validate_corpus.py --mode email --limit 2
    python scripts/validate_corpus.py --mode type  --record-id <id> --dry-run

Reads Supabase read-only for selection. The ONLY Postgres write is the targeted
re-project in reproject_records(), which goes through the normal ingest path.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from pathlib import Path

# Make `app.*` importable when run as a script from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.corpus_validator import (
    WorkItem,
    audit_existing_emails,
    backfill_principal_email,
    build_worklist,
    check_invariants,
    classify_entity_type,
    record_type_verdict,
    record_verdict,
    reproject_records,
)
from app.enrichment import resolve_domain
from app.db import init_db

logger = logging.getLogger("validate_corpus")


def _format_table(items: list[WorkItem]) -> str:
    header = ["record_id", "entity_name", "principal_name", "entity_type", "has_email"]
    rows = []
    for it in items:
        rows.append([
            it.record_id,
            it.entity_name,
            it.principal_name or "-",
            it.entity_type,
            "yes" if (it.principal_email and it.principal_email.strip()) else "no",
        ])
    widths = [len(h) for h in header]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    sep = "  "
    lines = [sep.join(h.ljust(widths[i]) for i, h in enumerate(header))]
    lines.append(sep.join("-" * widths[i] for i in range(len(header))))
    for r in rows:
        lines.append(sep.join(r[i].ljust(widths[i]) for i in range(len(r))))
    return "\n".join(lines)


def _print_invariants(label: str, inv: dict) -> None:
    if inv.get("status") == "skipped":
        print(f"  invariants {label}: SKIPPED ({inv.get('reason')})")
        return
    a = inv.get("A_role_principal_email")
    b = inv.get("B_nameless_principal_email")
    passed = inv.get("pass")
    tag = "PASS" if passed else "FAIL"
    print(f"  invariants {label}: {tag}  "
          f"(A role-principal_email={a}, B nameless-principal_email={b})")
    if not passed:
        print("    *** PRE-EXISTING CORRUPTION DETECTED — see invariant values above ***")


# A single row must never be able to hang the whole run. The site tier drives a headless
# browser (app/tools/crawl.py), and a page that neither loads nor errors will otherwise
# block forever — observed live 2026-08-16, when one row held the process for an hour with
# ~5s of CPU and left nine orphaned playwright chromium processes behind.
ROW_TIMEOUT_SECONDS = 120


async def _close_crawler_quietly() -> None:
    """Shut the shared headless browser down. Without this the playwright chromium
    outlives the process — the run "finishes" and leaves renderers resident."""
    try:
        from app.tools.crawl import close_crawler
    except Exception:  # noqa: BLE001 — crawl4ai is optional
        return
    try:
        await close_crawler()
    except Exception:  # noqa: BLE001 — a failed shutdown must not fail the run
        logger.warning("could not close crawler cleanly", exc_info=True)


async def _email_pass_async(items: list[WorkItem], *, timeout: float) -> dict:
    attempted = 0
    by_field: Counter = Counter()
    dropped: Counter = Counter()
    failed: list[str] = []
    touched: list[str] = []

    for item in items:
        attempted += 1
        try:
            result = await asyncio.wait_for(
                backfill_principal_email(item), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning("email pass: %s timed out after %ss", item.record_id, timeout)
            failed.append(f"{item.record_id} (timeout)")
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("email pass: %s failed: %s", item.record_id, exc, exc_info=True)
            failed.append(item.record_id)
            continue

        if result.field is None:
            dropped[result.reason or "not_found"] += 1
        else:
            by_field[result.field] += 1
            touched.append(item.record_id)

        record_verdict(item, result, dry_run=False)

    return {
        "attempted": attempted,
        "by_field": dict(by_field),
        "dropped": dict(dropped),
        "failed": failed,
        "touched": touched,
    }


def _run_email_pass(items: list[WorkItem], *, timeout: float = ROW_TIMEOUT_SECONDS) -> dict:
    """Run the email backfill over `items` and write the results. Returns a summary dict.
    One row failing or hanging never kills the run.

    NOT called in --dry-run: the backfill makes live Serper/Snov/browser calls, so a
    "dry" run that invoked it would spend credits and drive a browser. Dry-run prints the
    plan instead (see `_print_email_plan`).

    One event loop for the whole pass, and the crawler is closed in a `finally` — a fresh
    `asyncio.new_event_loop()` per row leaks a loop each time and never shuts the shared
    browser down."""
    async def _main() -> dict:
        try:
            return await _email_pass_async(items, timeout=timeout)
        finally:
            await _close_crawler_quietly()

    summary = asyncio.run(_main())
    summary["reproject"] = reproject_records(summary["touched"]) if summary["touched"] else None
    return summary


async def _type_pass_async(items: list[WorkItem], *, timeout: float) -> dict:
    counts = {"SFO": 0, "MFO": 0, "type_unconfirmed": 0}
    own_site = third_party = quote_rejected = 0
    failed: list[str] = []
    touched: list[str] = []

    for item in items:
        try:
            domain = await asyncio.wait_for(resolve_domain(item.entity_name), timeout=60)
        except Exception:  # noqa: BLE001 — no domain is not fatal; Serper evidence remains
            domain = None
        try:
            verdict = await asyncio.wait_for(
                classify_entity_type(item.entity_name, domain, item.aliases), timeout=timeout
            )
        except asyncio.TimeoutError:
            failed.append(f"{item.record_id} (timeout)")
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("type pass: %s failed: %s", item.record_id, exc, exc_info=True)
            failed.append(item.record_id)
            continue

        counts[verdict.verdict] = counts.get(verdict.verdict, 0) + 1
        if "quote_not_grounded" in (verdict.basis or "") or "quote_is_only_the_name" in (verdict.basis or ""):
            quote_rejected += 1
        if verdict.verdict != "type_unconfirmed":
            own_site += 1 if "own_site" in verdict.basis else 0
            third_party += 1 if "third_party" in verdict.basis else 0
            # Scraped quotes carry whatever punctuation the site used (bullets, curly
            # quotes, "○"). On Windows stdout defaults to cp1252, so printing one raised
            # UnicodeEncodeError and killed a whole run mid-pass. Never let a progress
            # line be able to end the job — degrade the character, keep the work.
            _line = (f"  {verdict.verdict:4} {item.entity_name[:42]:42} "
                     f"[{verdict.confidence}] {(verdict.snippet or '')[:70]!r}")
            enc = sys.stdout.encoding or "utf-8"
            print(_line.encode(enc, errors="replace").decode(enc, errors="replace"))
        if record_type_verdict(item, verdict):
            touched.append(item.record_id)

    return {"attempted": len(items), **counts, "own_site": own_site,
            "third_party": third_party, "quote_rejected": quote_rejected,
            "failed": failed, "touched": touched}


def _run_type_pass(items: list[WorkItem], *, timeout: float = ROW_TIMEOUT_SECONDS) -> dict:
    async def _main() -> dict:
        try:
            return await _type_pass_async(items, timeout=timeout)
        finally:
            await _close_crawler_quietly()

    summary = asyncio.run(_main())
    summary["reproject"] = reproject_records(summary["touched"]) if summary["touched"] else None
    return summary


def _print_email_plan(items: list[WorkItem]) -> None:
    """--dry-run output: what the real run WOULD do, at zero cost. Makes no network call.

    The tier order shown is the one `backfill_principal_email` will actually take, which
    is `wave_1`'s: Snov first when a principal is named (the lookup is attributable to a
    person), site-scrape first when not (any address found there is firm_email anyway)."""
    named = sum(1 for it in items if it.principal_name and it.principal_name.strip())
    print(f"\n--- email pass PLAN (dry run — no network calls, nothing written) ---")
    print(f"rows that would be attempted: {len(items)}")
    print(f"  with a principal named:     {named}  -> resolve_domain, Snov, site fallback")
    print(f"  with no principal named:    {len(items) - named}  -> resolve_domain, site, "
          f"Snov fallback; result can ONLY be firm_email")
    print(f"per-row timeout: {ROW_TIMEOUT_SECONDS}s")
    print("upper bound on spend: 1 domain resolution + <=1 Snov lookup per row above.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Corpus validator (T43)")
    parser.add_argument("--mode", required=True, choices=["type", "email", "audit"])
    parser.add_argument("--apply", action="store_true",
                        help="audit mode only: actually demote misattributed rows. "
                             "Without it, audit reports and writes nothing.")
    parser.add_argument("--limit", type=int, default=50,
                        help="Hard cap on processable rows (default 50). No 'unlimited'.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve and print verdicts; write nothing anywhere.")
    parser.add_argument("--record-id", dest="record_id", default=None,
                        help="Process exactly one record (may combine with --dry-run).")
    args = parser.parse_args(argv)

    if args.limit is None or args.limit <= 0:
        parser.error("--limit must be a positive integer (there is no 'unlimited' option)")

    # Ensure the validator_checkpoints migration is applied to data/foia.db before we
    # read from it. Idempotent.
    init_db()

    if args.mode == "audit":
        # Re-check every EXISTING principal_email and demote the misattributed ones.
        # --dry-run and the absence of --apply both mean "report only".
        apply = args.apply and not args.dry_run
        summary = asyncio.run(
            audit_existing_emails(limit=None if args.record_id else args.limit, apply=apply)
        )
        if summary.get("status") == "skipped":
            print(f"audit skipped: {summary.get('reason')}")
            return 1
        print(f"checked {summary['checked']} rows with a principal_email")
        print(f"attribution counts: {summary['counts']}")
        findings = summary["findings"]
        print(f"\nmisattributed: {len(findings)}")
        for f in findings:
            print(f"  {f['attribution']:13} {f['email']:45} "
                  f"principal={f['principal_name'] or '(none)'}  [{f['basis']}]")
        if apply:
            print(f"\ndemoted to firm_email: {len(summary['demoted'])}")
            print(f"re-project: {summary['reproject']}")
            print("\n--- invariant checks after demotion ---")
            _print_invariants("AFTER", check_invariants())
        else:
            print("\n(report only — pass --apply to demote these to firm_email)")
        return 0

    if args.record_id:
        # Single-row targeting: fetch the one row if it matches the mode's predicate.
        items = build_worklist(args.mode, limit=None)
        items = [it for it in items if it.record_id == args.record_id]
        if not items:
            print(f"No row found for --record-id {args.record_id} under mode={args.mode}",
                  file=sys.stderr)
            return 1
    else:
        items = build_worklist(args.mode, limit=args.limit)

    print(f"mode={args.mode} limit={args.limit} dry_run={args.dry_run} "
          f"processable={len(items)}")
    print(_format_table(items))

    if args.mode == "type":
        if args.dry_run:
            print(f"\n--- type pass PLAN (dry run — no network calls, nothing written) ---")
            print(f"rows that would be classified: {len(items)}")
            print("per row: 1 ADV lookup + up to 7 site pages + 2 Serper searches + 1 LLM call")
            print("`type_unconfirmed` is a legitimate outcome and writes no claim.")
            return 0
        summary = _run_type_pass(items)
        print(f"\nrows attempted:  {summary['attempted']}")
        print(f"verdicts:        SFO={summary['SFO']}, MFO={summary['MFO']}, "
              f"type_unconfirmed={summary['type_unconfirmed']}")
        print(f"  of the decided: own_site={summary['own_site']}, third_party={summary['third_party']}")
        print(f"ungrounded quotes rejected: {summary['quote_rejected']}")
        print(f"rows failed:     {len(summary['failed'])}  {summary['failed']}")
        rp = summary["reproject"]
        print(f"re-project:      {rp if rp else '(no verdicts to project)'}")
        return 0

    # --- email mode ---
    if args.dry_run:
        # Zero external calls. Print the plan and the current invariant state, then stop.
        _print_email_plan(items)
        print("\n--- invariant checks (live Supabase, read-only) ---")
        _print_invariants("NOW", check_invariants())
        return 0

    # Capture invariant state BEFORE any write, so the before/after comparison is honest.
    print("\n--- email pass ---")
    before = check_invariants()
    _print_invariants("BEFORE", before)
    summary = _run_email_pass(items)

    print(f"rows attempted:     {summary['attempted']}")
    bf = summary["by_field"]
    print(f"addresses by field: principal_email={bf.get('principal_email', 0)}, "
          f"firm_email={bf.get('firm_email', 0)}")
    dr = summary["dropped"]
    print(f"rows dropped:       no_domain={dr.get('no_domain', 0)}, "
          f"off_domain={dr.get('off_domain', 0)}, not_found={dr.get('not_found', 0)}, "
          f"tool_unavailable={dr.get('tool_unavailable', 0)}")
    if dr.get("tool_unavailable"):
        print(f"  *** {dr['tool_unavailable']} row(s) could not be looked up at all — a tool "
              f"was down or out of credits. These are NOT 'no email exists'; they are "
              f"unanswered and should be re-run once the tool is restored. ***")
    print(f"rows failed:        {len(summary['failed'])}  {summary['failed']}")
    rp = summary["reproject"]
    if rp is None:
        print("re-project:         (no touched rows)")
    elif rp.get("status") == "ok":
        print(f"re-project:         ok reprojected={rp.get('reprojected')} "
              f"skipped={rp.get('skipped')} build_hash={rp.get('build_hash')}")
    else:
        print(f"re-project:         {rp.get('status')} {rp.get('reason', rp.get('error', ''))}")

    # --- invariant checks against live Supabase, after the writes ---
    print("\n--- invariant checks (live Supabase, read-only) ---")
    after = check_invariants()
    _print_invariants("AFTER", after)
    if before.get("pass") is False or after.get("pass") is False:
        return 2  # corruption detected — non-fatal but signalled
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
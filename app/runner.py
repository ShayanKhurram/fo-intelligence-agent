"""Batch runner — plan §5. Runs the graph across many leads with bounded concurrency, a
SQLite checkpoint ledger (queued -> running -> verdict_done -> failed) that survives a
restart, a global USD budget with a 75%-warn threshold, and a preflight check that
SERPER_API_KEY is set (the only hard external dependency — web_search and fetch_page both
run on Serper, a cloud API). Per-tool rate limiting (SEC/LinkedIn) is enforced globally
via the module-level buckets in app/tools/ratelimit.py, not here — this module just needs
to not defeat that by capping overall concurrency sanely."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.config import SETTINGS
from app.db import (
    connection,
    get_checkpoint,
    get_resumable_leads,
    get_unstarted_entities,
    init_db,
    start_run,
    finish_run,
    upsert_checkpoint,
)
from app.graph import run_lead

logger = logging.getLogger(__name__)


class PreflightError(RuntimeError):
    pass


@dataclass
class BatchResult:
    processed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    # Leads whose research never completed (all lanes timed out, zero claims) and which
    # were therefore re-queued rather than judged. Kept apart from `processed` — nothing
    # was assessed — and from `failed`, which means the lead itself errored.
    retried: list[str] = field(default_factory=list)
    total_cost_usd: float = 0.0
    budget_aborted: bool = False
    # T35.1: the durable run identity for this batch, so a provenance log can attribute
    # every tool call / output field back to the exact execution that produced it. None
    # only if start_run failed to write — the run is opened before any lead runs.
    run_id: str | None = None


async def preflight(fail_fast: bool = True) -> bool:
    """Verify the search/scrape backend is configured before the batch starts. web_search
    and fetch_page both run on Serper (a cloud API) — the only hard precondition is that
    SERPER_API_KEY is set. A missing key means every search/scrape returns nothing, which
    would silently burn the LLM budget on could_not_verify claims, so fail fast."""
    ok = bool(SETTINGS.tools.serper_api_key)
    if not ok and fail_fast:
        raise PreflightError(
            "SERPER_API_KEY not set — web_search/fetch_page will return nothing. "
            "Set it in .env (get a key at https://serper.dev)."
        )
    return ok


def _resolve_queue(db_path: str | None, entity_ids: list[str] | None, resume: bool) -> list[str]:
    with connection(db_path) as conn:
        if entity_ids is not None:
            queue = list(entity_ids)
        else:
            queue = get_unstarted_entities(conn)
        if resume:
            resumable = [e for e in get_resumable_leads(conn) if e not in queue]
            queue = resumable + queue
    # de-dupe, preserve order (resumable leads first so an interrupted run finishes before
    # starting fresh work)
    seen: set[str] = set()
    ordered: list[str] = []
    for eid in queue:
        if eid not in seen:
            seen.add(eid)
            ordered.append(eid)
    return ordered


async def run_batch(
    entity_ids: list[str] | None = None,
    db_path: str | None = None,
    resume: bool = True,
    skip_preflight: bool = False,
) -> BatchResult:
    db_path = db_path or SETTINGS.db_path
    init_db(db_path)

    if not skip_preflight:
        await preflight(fail_fast=True)

    queue = _resolve_queue(db_path, entity_ids, resume)
    result = BatchResult()
    # T35.1: open a `layer1` run and close it in a finally so a raised exception never
    # leaves a run stuck at status='running'. entity_count is the size of the queue we
    # are about to process; the outcome counters (processed/failed/retried + spend) are
    # stamped on close. The run_id is exposed on BatchResult for downstream attribution.
    with connection(db_path) as conn:
        run_id = start_run(conn, "layer1", entity_count=len(queue))
    result.run_id = run_id
    semaphore = asyncio.Semaphore(SETTINGS.runner.concurrency)
    lock = asyncio.Lock()
    warned = False

    async def process_one(entity_id: str) -> None:
        nonlocal warned
        async with semaphore:
            with connection(db_path) as conn:
                checkpoint = get_checkpoint(conn, entity_id)
                attempts = checkpoint["attempts"] if checkpoint else 0
                if attempts >= SETTINGS.runner.max_lead_attempts:
                    upsert_checkpoint(
                        conn, entity_id, status="failed", last_error="max attempts exhausted"
                    )
                    result.failed.append(entity_id)
                    return

            async with lock:
                if result.total_cost_usd >= SETTINGS.runner.global_budget_usd:
                    with connection(db_path) as conn:
                        upsert_checkpoint(
                            conn, entity_id, status="failed", last_error="global budget exhausted"
                        )
                    result.failed.append(entity_id)
                    result.budget_aborted = True
                    return

            with connection(db_path) as conn:
                upsert_checkpoint(conn, entity_id, status="running", attempts=attempts + 1)

            try:
                final_state = await run_lead(entity_id, db_path=db_path)
                lead_cost = final_state.get("cost_usd", 0.0)
            except Exception as exc:  # noqa: BLE001 — plan §7: one bad lead never stalls the batch
                logger.exception("Lead %s failed", entity_id)
                with connection(db_path) as conn:
                    upsert_checkpoint(conn, entity_id, status="failed", last_error=f"{type(exc).__name__}: {exc}"[:500])
                result.failed.append(entity_id)
                return

            async with lock:
                result.total_cost_usd += lead_cost
                fraction = result.total_cost_usd / SETTINGS.runner.global_budget_usd
                if not warned and fraction >= SETTINGS.runner.warn_at_fraction:
                    warned = True
                    logger.warning(
                        "Global budget at %.0f%% ($%.2f / $%.2f)",
                        fraction * 100,
                        result.total_cost_usd,
                        SETTINGS.runner.global_budget_usd,
                    )

            retry_reason = final_state.get("retry_reason")
            with connection(db_path) as conn:
                if retry_reason:
                    # Research never completed (all lanes timed out, zero claims). Leave the
                    # lead re-queueable instead of marking it done — `get_resumable_leads`
                    # picks up 'retry', and the max_lead_attempts cap above stops it looping
                    # forever. Counted separately from `processed`: nothing was assessed.
                    upsert_checkpoint(
                        conn, entity_id, status="retry", last_error=f"research incomplete: {retry_reason}"
                    )
                    result.retried.append(entity_id)
                    return
                upsert_checkpoint(conn, entity_id, status="verdict_done", cost_usd=lead_cost)
            result.processed.append(entity_id)

    try:
        await asyncio.gather(*(process_one(eid) for eid in queue))
        notes = {
            "processed": len(result.processed),
            "failed": len(result.failed),
            "retried": len(result.retried),
            "total_cost_usd": result.total_cost_usd,
            "budget_aborted": result.budget_aborted,
        }
        with connection(db_path) as conn:
            finish_run(conn, run_id, status="done", notes=notes)
        return result
    except Exception:
        # Any unhandled exception (not a per-lead crash — those are caught inside
        # process_one — but e.g. KeyboardInterrupt, preflight re-raising, gather
        # cancellation) must still close the run as failed, never leave it running.
        with connection(db_path) as conn:
            finish_run(conn, run_id, status="failed")
        raise

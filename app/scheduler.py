"""T36 — scheduled agent runs.

Three things this module owns:

1. **Standing orders** (`schedules`): run daily at HH:MM UTC, or every N minutes. Each
   carries `target_confirmed` ("stop once this many leads confirm") and `max_leads`
   ("never process more than this many").
2. **A run with real termination rules.** The batch runner (`app.runner.run_batch`)
   processes a fixed queue to exhaustion; a scheduled run has to be able to stop *early*,
   the moment it has what it was asked for. So it walks the queue in chunks and checks
   its stop conditions after every chunk, in this precedence:

       target_reached  > max_leads_reached > leads_exhausted

   The first is the point of the feature; the last is the automatic termination the user
   asked for — a run that runs out of leads ends cleanly, it does not hang waiting for
   work that will never arrive.
3. **The full agent per lead, not just Layer 1.** "Confirmed" only exists downstream of
   enrichment (`ship` / `ship_with_caveats`), so a scheduled run takes each lead through
   Layer 1 (`run_lead`) and then, when the verdict is pursue/pursue_low, through
   enrichment (`process_entity`). A lead that confirms is queued for the micro-RAG
   immediately (see app/rag_sync.py).

`run_lead`, `process_entity` and `get_model` are imported as module-level names so tests
can monkeypatch them — the same pattern `app/api.py` and `tests/test_runner.py` already
use. Nothing here needs network or an API key to be exercised.

The loop is deliberately a plain asyncio task polling a SQLite table once a minute rather
than a cron daemon or APScheduler: the schedule state has to survive a restart (Render
restarts services freely), and a table the API can read and write is the simplest thing
that gives both persistence and a live view. `next_run_at` is computed and stored, so a
restart mid-window does not lose or double-fire a schedule.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import SETTINGS
from app.db import (
    connection,
    finish_run,
    get_resumable_leads,
    get_unstarted_entities,
    init_db,
    start_run,
    upsert_checkpoint,
)
from app.enrichment import process_entity
from app.graph import run_lead
from app.llm import get_model
from app.log_sync import sync_runs
from app.rag_sync import drain_queue, enqueue_entity, is_confirmed

logger = logging.getLogger(__name__)

VALID_KINDS = ("daily", "interval")

# How many leads a scheduled run takes through the agent at once. Kept at the runner's
# own concurrency so a scheduled run puts no more load on the paid tools than a manual
# batch does — and small enough that an early stop wastes at most this many leads of work
# past the target.
DEFAULT_CHUNK = 3


# ---------------------------------------------------------------------------
# Schedule CRUD
# ---------------------------------------------------------------------------


def _validate(kind: str, time_utc: str | None, interval_minutes: int | None) -> None:
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")
    if kind == "daily":
        if not time_utc:
            raise ValueError("kind='daily' requires time_utc as 'HH:MM'")
        try:
            hh, mm = time_utc.split(":")
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                raise ValueError
        except (ValueError, AttributeError):
            raise ValueError(f"time_utc must be 'HH:MM' (24h, UTC), got {time_utc!r}") from None
    if kind == "interval" and (not interval_minutes or interval_minutes < 1):
        raise ValueError("kind='interval' requires interval_minutes >= 1")


def compute_next_run(
    kind: str,
    *,
    time_utc: str | None = None,
    interval_minutes: int | None = None,
    now: datetime | None = None,
) -> datetime:
    """The next firing strictly after `now`. Daily schedules fire at the next occurrence
    of HH:MM UTC (today if still ahead, else tomorrow); interval schedules fire
    `interval_minutes` from now. Strictly after, never equal, so a schedule cannot fire
    twice on the same tick."""
    now = now or datetime.now(timezone.utc)
    if kind == "interval":
        return now + timedelta(minutes=int(interval_minutes or 1))
    hh, mm = (time_utc or "00:00").split(":")
    candidate = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def create_schedule(
    conn: sqlite3.Connection,
    name: str,
    kind: str = "daily",
    *,
    time_utc: str | None = None,
    interval_minutes: int | None = None,
    target_confirmed: int | None = None,
    max_leads: int | None = None,
    enabled: bool = True,
    now: datetime | None = None,
) -> str:
    _validate(kind, time_utc, interval_minutes)
    schedule_id = uuid.uuid4().hex
    next_run = compute_next_run(kind, time_utc=time_utc, interval_minutes=interval_minutes, now=now)
    conn.execute(
        """
        INSERT INTO schedules (schedule_id, name, kind, time_utc, interval_minutes,
                               target_confirmed, max_leads, enabled, next_run_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (schedule_id, name, kind, time_utc, interval_minutes, target_confirmed,
         max_leads, int(enabled), next_run.isoformat()),
    )
    return schedule_id


def get_schedule(conn: sqlite3.Connection, schedule_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM schedules WHERE schedule_id = ?", (schedule_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    return d


def list_schedules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM schedules ORDER BY created_at DESC").fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        out.append(d)
    return out


_UPDATABLE = ("name", "kind", "time_utc", "interval_minutes", "target_confirmed",
              "max_leads", "enabled")


def update_schedule(
    conn: sqlite3.Connection, schedule_id: str, *, now: datetime | None = None, **fields: Any
) -> dict[str, Any] | None:
    """Patch a schedule. Any change to the timing fields recomputes `next_run_at` —
    otherwise a schedule edited from 'daily 09:00' to 'every 15 minutes' would keep
    waiting for tomorrow morning."""
    current = get_schedule(conn, schedule_id)
    if current is None:
        return None
    updates = {k: v for k, v in fields.items() if k in _UPDATABLE}
    merged = {**current, **updates}
    _validate(merged["kind"], merged["time_utc"], merged["interval_minutes"])
    sets, params = [], []
    for key, value in updates.items():
        sets.append(f"{key} = ?")
        params.append(int(value) if key == "enabled" else value)
    if any(k in updates for k in ("kind", "time_utc", "interval_minutes", "enabled")):
        next_run = compute_next_run(merged["kind"], time_utc=merged["time_utc"],
                                    interval_minutes=merged["interval_minutes"], now=now)
        sets.append("next_run_at = ?")
        params.append(next_run.isoformat())
    if not sets:
        return current
    params.append(schedule_id)
    conn.execute(f"UPDATE schedules SET {', '.join(sets)} WHERE schedule_id = ?", params)
    return get_schedule(conn, schedule_id)


def delete_schedule(conn: sqlite3.Connection, schedule_id: str) -> bool:
    cur = conn.execute("DELETE FROM schedules WHERE schedule_id = ?", (schedule_id,))
    return cur.rowcount > 0


def due_schedules(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Enabled schedules whose `next_run_at` has passed. A NULL next_run_at counts as due
    — that is a schedule created before its first firing was computed, and never firing
    at all is a worse failure than firing once immediately."""
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT * FROM schedules WHERE enabled = 1 AND (next_run_at IS NULL OR next_run_at <= ?) "
        "ORDER BY next_run_at",
        (now.isoformat(),),
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# The scheduled run
# ---------------------------------------------------------------------------


def _queue(conn: sqlite3.Connection) -> list[str]:
    """Leads available to a scheduled run: anything never started, plus anything an
    interrupted run left resumable. De-duplicated, resumable first (finish what was
    started before starting more)."""
    unstarted = get_unstarted_entities(conn)
    resumable = [e for e in get_resumable_leads(conn) if e not in unstarted]
    seen: set[str] = set()
    ordered: list[str] = []
    for eid in resumable + unstarted:
        if eid not in seen:
            seen.add(eid)
            ordered.append(eid)
    return ordered


async def _process_one(entity_id: str, model: Any, db_path: str) -> dict[str, Any]:
    """One lead, all the way through: Layer 1, then enrichment when the verdict warrants
    it. Returns {"entity_id", "verdict", "outcome", "confirmed", "cost_usd", "error"}.
    Never raises — one bad lead must not end a scheduled run (plan §7's rule for the
    batch runner, which applies at least as strongly to an unattended one)."""
    result: dict[str, Any] = {"entity_id": entity_id, "verdict": None, "outcome": None,
                              "confirmed": False, "cost_usd": 0.0, "error": None}
    try:
        with connection(db_path) as conn:
            upsert_checkpoint(conn, entity_id, status="running")
        state = await run_lead(entity_id, db_path=db_path)
        result["cost_usd"] = state.get("cost_usd", 0.0) or 0.0
        verdict = None
        with connection(db_path) as conn:
            row = conn.execute(
                "SELECT verdict FROM decisions WHERE entity_id = ? ORDER BY id DESC LIMIT 1",
                (entity_id,),
            ).fetchone()
            verdict = row["verdict"] if row else None
            # A lead whose research never ran is re-queued rather than judged — the same
            # rule app/runner.py follows (commit 4863055). Nothing was assessed, so it is
            # not a processed lead and certainly not a rejection.
            if state.get("retry_reason"):
                upsert_checkpoint(conn, entity_id, status="retry",
                                  last_error=f"research incomplete: {state['retry_reason']}")
                result["error"] = f"retry: {state['retry_reason']}"
                return result
            upsert_checkpoint(conn, entity_id, status="verdict_done", cost_usd=result["cost_usd"])
        result["verdict"] = verdict
        if verdict not in ("pursue", "pursue_low"):
            return result
        with connection(db_path) as conn:
            enrich = await process_entity(conn, entity_id, model)
        result["outcome"] = enrich.get("outcome")
        result["cost_usd"] += enrich.get("usd_spent", 0.0) or 0.0
        result["confirmed"] = is_confirmed(result["outcome"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("scheduled run: lead %s failed", entity_id)
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        try:
            with connection(db_path) as conn:
                upsert_checkpoint(conn, entity_id, status="failed", last_error=result["error"])
        except Exception:  # noqa: BLE001
            pass
    return result


async def run_scheduled_job(
    schedule: dict[str, Any],
    *,
    db_path: str | None = None,
    model: Any = None,
    chunk_size: int = DEFAULT_CHUNK,
) -> dict[str, Any]:
    """Execute one scheduled run and return its summary.

    Stop conditions, checked after every chunk, in precedence order:
      1. `target_confirmed` reached          -> "target_reached"
      2. `max_leads` processed               -> "max_leads_reached"
      3. the lead queue is empty             -> "leads_exhausted"

    Every confirmed lead is queued for the micro-RAG as it confirms; the queue is drained
    once at the end (best-effort — see app/rag_sync.py; the RAG being unreachable delays
    ingestion and never fails the run).
    """
    db_path = db_path or SETTINGS.db_path
    init_db(db_path)
    model = model if model is not None else get_model("mid")
    target = schedule.get("target_confirmed")
    max_leads = schedule.get("max_leads")

    with connection(db_path) as conn:
        queue = _queue(conn)
        run_id = start_run(conn, "scheduled", entity_count=len(queue), notes={
            "schedule_id": schedule.get("schedule_id"),
            "schedule_name": schedule.get("name"),
            "target_confirmed": target,
            "max_leads": max_leads,
        })

    processed: list[dict[str, Any]] = []
    confirmed_ids: list[str] = []
    total_cost = 0.0
    termination = "leads_exhausted"
    index = 0
    try:
        while index < len(queue):
            if target is not None and len(confirmed_ids) >= target:
                termination = "target_reached"
                break
            if max_leads is not None and len(processed) >= max_leads:
                termination = "max_leads_reached"
                break
            remaining_caps = [len(queue) - index]
            if max_leads is not None:
                remaining_caps.append(max_leads - len(processed))
            take = max(1, min(chunk_size, *remaining_caps))
            chunk = queue[index:index + take]
            index += take
            results = await asyncio.gather(
                *(_process_one(eid, model, db_path) for eid in chunk)
            )
            for r in results:
                processed.append(r)
                total_cost += r["cost_usd"]
                if r["confirmed"]:
                    confirmed_ids.append(r["entity_id"])
                    with connection(db_path) as conn:
                        enqueue_entity(conn, r["entity_id"], run_id=run_id)
        else:
            # Loop fell off the end without breaking: the queue is exhausted. Re-check
            # the target so a run whose LAST chunk hit the target reports why it really
            # stopped rather than "ran out of leads".
            if target is not None and len(confirmed_ids) >= target:
                termination = "target_reached"
    except Exception as exc:  # noqa: BLE001
        logger.exception("scheduled run %s failed", run_id)
        termination = "error"
        with connection(db_path) as conn:
            finish_run(conn, run_id, status="failed", notes={
                "schedule_id": schedule.get("schedule_id"),
                "error": f"{type(exc).__name__}: {exc}"[:300],
                "processed": len(processed), "confirmed": len(confirmed_ids),
            })
        return {"run_id": run_id, "processed": len(processed), "confirmed": len(confirmed_ids),
                "termination": termination, "usd_spent": total_cost, "error": str(exc)[:300]}

    rag = drain_queue(db_path)
    # T37: mirror this run's log to Postgres so the hosted (Vercel) view shows it. Same
    # contract as the RAG drain — a `skipped`/`error` summary, never an exception, and
    # the local SQLite log stays the source of truth either way.
    log_push = sync_runs(db_path, limit=5)

    # The provenance log for what this run produced, under this run's id — so the Log tab
    # can go run -> leads -> field logs with no extra plumbing (PLAN.md T35.5).
    try:
        from app.db import write_field_provenance
        from app.provenance_log import build_run_log
        from app.dataset import _atomic_write_json
        import json as _json
        from pathlib import Path as _Path

        with connection(db_path) as conn:
            finish_run(conn, run_id, status="done", notes={
                "schedule_id": schedule.get("schedule_id"),
                "schedule_name": schedule.get("name"),
                "target_confirmed": target,
                "max_leads": max_leads,
                "processed": len(processed),
                "confirmed": len(confirmed_ids),
                "failed": sum(1 for r in processed if r["error"]),
                "usd_spent": total_cost,
                "termination": termination,
                "rag": rag,
                "log_push": log_push,
            })
            doc = build_run_log(conn, run_id, [(r["entity_id"], r["outcome"] or r["verdict"] or "processed")
                                               for r in processed])
            rows = []
            for lead in doc["leads"]:
                for rec in lead["fields"]:
                    rows.append({
                        "run_id": run_id, "entity_id": lead["entity_id"], "field": rec["field"],
                        "value": rec["value"], "status": rec["status"], "shipped": rec["shipped"],
                        "source_class": (rec["how"] or {}).get("source_class"),
                        "extraction_method": (rec["how"] or {}).get("extraction_method"),
                        "record": _json.dumps(rec, ensure_ascii=False, default=str),
                    })
            write_field_provenance(conn, rows)
        _atomic_write_json(_Path(db_path).parent / "runs" / run_id / "field_provenance.json", doc)
    except Exception:  # noqa: BLE001 — the leads are the deliverable, the log is its record
        logger.error("scheduled run %s: provenance emission failed", run_id, exc_info=True)
        with connection(db_path) as conn:
            finish_run(conn, run_id, status="done", notes={
                "schedule_id": schedule.get("schedule_id"), "processed": len(processed),
                "confirmed": len(confirmed_ids), "termination": termination,
                "usd_spent": total_cost, "rag": rag, "provenance_emission": "failed",
            })

    return {"run_id": run_id, "processed": len(processed), "confirmed": len(confirmed_ids),
            "confirmed_ids": confirmed_ids, "termination": termination,
            "usd_spent": total_cost, "rag": rag, "log_push": log_push}


def mark_schedule_fired(conn: sqlite3.Connection, schedule: dict[str, Any], *, now: datetime | None = None) -> None:
    """Stamp last_run_at and compute the next firing. Called BEFORE the run starts, not
    after: a long run must not be re-fired by the next poll while it is still going, and
    a crash mid-run must not leave the schedule permanently due."""
    now = now or datetime.now(timezone.utc)
    next_run = compute_next_run(schedule["kind"], time_utc=schedule.get("time_utc"),
                                interval_minutes=schedule.get("interval_minutes"), now=now)
    conn.execute(
        "UPDATE schedules SET last_run_at = ?, next_run_at = ? WHERE schedule_id = ?",
        (now.isoformat(), next_run.isoformat(), schedule["schedule_id"]),
    )


async def tick(
    *, db_path: str | None = None, now: datetime | None = None, model: Any = None
) -> list[dict[str, Any]]:
    """One poll: fire every due schedule, in order, one at a time. Returns the summaries.

    Sequential on purpose — two scheduled runs at once would double the load on the paid
    tools and race for the same lead queue. Each schedule is stamped fired before its run
    starts, so a run longer than the poll interval is not started twice."""
    db_path = db_path or SETTINGS.db_path
    summaries: list[dict[str, Any]] = []
    with connection(db_path) as conn:
        due = due_schedules(conn, now=now)
        for schedule in due:
            mark_schedule_fired(conn, schedule, now=now)
    for schedule in due:
        summaries.append(await run_scheduled_job(schedule, db_path=db_path, model=model))
    return summaries


async def scheduler_loop(
    *,
    db_path: str | None = None,
    poll_seconds: int = 60,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Poll for due schedules until `stop_event` is set. Started by the API's lifespan so
    a single deployed service both serves the UI and runs the schedules — the SQLite file
    is a local file on one disk, so a separate worker process could not share it anyway
    (Render disks mount to exactly one service)."""
    db_path = db_path or SETTINGS.db_path
    logger.info("scheduler loop started (poll=%ss, db=%s)", poll_seconds, db_path)
    while stop_event is None or not stop_event.is_set():
        try:
            await tick(db_path=db_path)
        except Exception:  # noqa: BLE001 — a bad tick must never kill the loop
            logger.exception("scheduler tick failed")
        try:
            if stop_event is None:
                await asyncio.sleep(poll_seconds)
            else:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            raise
    logger.info("scheduler loop stopped")

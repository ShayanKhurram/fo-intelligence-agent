"""FastAPI web layer for the research-layer pipeline. This is an interactive session
tool, not a durable job queue: in-flight runs live in a module-level dict and are lost on
server restart (acceptable — re-running a batch is idempotent via the checkpoint ledger;
`run_batch(resume=True)` would pick up anything interrupted). All DB access goes through
`app.db.connection()` one short-lived connection per request, the same pattern the rest of
the codebase uses — no connection is held open across requests.

The only things this module does itself are HTTP plumbing and run tracking; every piece of
real work is delegated to the existing `ingest_discovery_file` / `run_batch` / `app.db`
read functions, which are imported as module-level names so tests can monkeypatch them
(the same pattern `tests/test_runner.py` uses for `run_lead`)."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.db import (
    connection,
    get_entity,
    get_field_provenance,
    get_lead_trace,
    get_run,
    init_db,
    list_runs,
    start_run,
    finish_run,
)
from app.ingest import ingest_discovery_file
from app.log_sync import sync_runs
from app.rag_sync import drain_queue, queue_counts
from app.runner import run_batch
from app.scheduler import (
    create_schedule,
    delete_schedule,
    get_schedule,
    list_schedules,
    run_scheduled_job,
    scheduler_loop,
    update_schedule,
)

DISCOVERY_JSONL_PATH = "data/fo_discovery_c1-c3-c4-c8_20260728T090511Z.jsonl"
STATIC_DIR = Path(__file__).parent / "static"

# run_id -> {"entity_ids", "status", "started_at", "result", "task"}. Module-level,
# in-memory: lost on restart. See module docstring for why that's acceptable here.
RUNS: dict[str, dict] = {}


# The scheduler's background loop and its stop signal. Module-level so /api/scheduler/status
# can report whether it is actually running rather than whether it was meant to be.
_SCHEDULER: dict[str, Any] = {"task": None, "stop": None, "enabled": False}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Create the schema once at startup, before any request is served. The first call
    # the frontend makes (GET /api/leads/available) never goes through run_batch, which
    # is the only other place init_db() gets called — so without this a fresh checkout
    # 500's on page load with "no such table: entities".
    init_db()
    # T36: the scheduler runs INSIDE this service, not as a separate worker. The pipeline
    # state is a SQLite file on one disk, and a Render disk mounts to exactly one service
    # — a second process could not share it. Off by default for local/dev runs; set
    # FOIA_SCHEDULER=1 (render.yaml does) to arm it.
    if os.environ.get("FOIA_SCHEDULER", "").strip() in ("1", "true", "yes", "on"):
        stop = asyncio.Event()
        poll = int(os.environ.get("FOIA_SCHEDULER_POLL_SECONDS", "60"))
        _SCHEDULER["stop"] = stop
        _SCHEDULER["enabled"] = True
        _SCHEDULER["task"] = asyncio.create_task(
            scheduler_loop(poll_seconds=poll, stop_event=stop)
        )
    try:
        yield
    finally:
        stop = _SCHEDULER.get("stop")
        task = _SCHEDULER.get("task")
        if stop is not None:
            stop.set()
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        _SCHEDULER.update({"task": None, "stop": None, "enabled": False})


app = FastAPI(title="FO Intelligence Agent — Research Layer", lifespan=_lifespan)


class RunBody(BaseModel):
    count: int | None = None


class ScheduleBody(BaseModel):
    """Create/patch payload for a schedule. Every field optional on PATCH; `name` and the
    timing fields are validated by app.scheduler._validate, which raises ValueError — the
    handlers translate that into a 400 rather than letting it 500."""
    name: str | None = None
    kind: str | None = None
    time_utc: str | None = None
    interval_minutes: int | None = None
    target_confirmed: int | None = None
    max_leads: int | None = None
    enabled: bool | None = None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_order() -> list[str]:
    """Ingest (idempotent) and return the ordered entity_id list. Safe to call every
    request — `ingest_discovery_file` upserts, so a warm DB is a no-op read."""
    with connection() as conn:
        return ingest_discovery_file(conn, DISCOVERY_JSONL_PATH)


async def _track_run(run_id: str, entity_ids: list[str]) -> None:
    """Background task: runs the batch, then flips the run's status to done. The POST
    handler returns immediately without awaiting this. T35.1: also persists the run
    through start_run/finish_run so it has a durable row in `runs` (the in-memory RUNS
    dict holds the asyncio task and is lost on restart; the DB row survives)."""
    try:
        with connection() as conn:
            start_run(conn, "api", run_id=run_id, entity_count=len(entity_ids))
        result = await run_batch(
            entity_ids=entity_ids, resume=False, skip_preflight=False
        )
        with connection() as conn:
            finish_run(conn, run_id, status="done", notes={
                "processed": len(result.processed),
                "failed": len(result.failed),
                "retried": len(result.retried),
                "total_cost_usd": result.total_cost_usd,
                "budget_aborted": result.budget_aborted,
            })
        RUNS[run_id]["status"] = "done"
        RUNS[run_id]["result"] = result
    except Exception as exc:  # noqa: BLE001 — a failed batch shouldn't kill the task silently
        with connection() as conn:
            finish_run(conn, run_id, status="failed")
        RUNS[run_id]["status"] = "done"
        RUNS[run_id]["result"] = None
        RUNS[run_id]["error"] = str(exc)[:500]


@app.get("/api/leads/available")
def leads_available() -> dict:
    order = _resolve_order()
    sample_names: list[str] = []
    with connection() as conn:
        for eid in order[:5]:
            ent = get_entity(conn, eid)
            if ent:
                sample_names.append(ent["canonical_name"])
    return {"total": len(order), "sample_names": sample_names}


@app.post("/api/run")
async def start_run(body: RunBody | None = None) -> dict:
    count = body.count if body is not None else None
    order = _resolve_order()
    # `count is None` is the only thing that means "run all". `count == 0` is a real
    # request for zero leads — `if count` would treat it as falsy and run the entire
    # feed. Negative counts fall through to slice semantics (order[:-1] etc.).
    entity_ids = list(order) if count is None else order[:count]
    run_id = uuid4().hex
    RUNS[run_id] = {
        "entity_ids": entity_ids,
        "status": "running",
        "started_at": _iso_now(),
    }
    # Keep a strong reference so the loop's weak ref doesn't GC the task mid-run.
    task = asyncio.create_task(_track_run(run_id, entity_ids))
    RUNS[run_id]["task"] = task
    return {"run_id": run_id, "total": len(entity_ids), "entity_ids": entity_ids}


@app.get("/api/run/{run_id}/status")
def run_status(run_id: str) -> dict:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run_id")
    entity_ids = run["entity_ids"]
    leads: list[dict] = []
    total_cost = 0.0
    with connection() as conn:
        for eid in entity_ids:
            ent = get_entity(conn, eid)
            name = ent["canonical_name"] if ent else eid
            cp = conn.execute(
                "SELECT status, cost_usd FROM lead_checkpoints WHERE entity_id = ?",
                (eid,),
            ).fetchone()
            decision = conn.execute(
                "SELECT verdict, rationale FROM decisions WHERE entity_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (eid,),
            ).fetchone()
            rejection = conn.execute(
                "SELECT reason_code FROM rejections WHERE entity_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (eid,),
            ).fetchone()

            status = cp["status"] if cp else "queued"
            cost = cp["cost_usd"] if cp else 0.0
            total_cost += cost
            if decision:
                verdict = decision["verdict"]
                reason = decision["rationale"]
            elif rejection:
                verdict = "reject"
                reason = rejection["reason_code"]
            else:
                verdict = None
                reason = None
            leads.append(
                {
                    "entity_id": eid,
                    "name": name,
                    "status": status,
                    "verdict": verdict,
                    "reason": reason,
                    "cost_usd": cost,
                }
            )
    return {
        "run_id": run_id,
        "total": len(entity_ids),
        "status": run["status"],
        "total_cost_usd": total_cost,
        "leads": leads,
    }


@app.get("/api/lead/{entity_id}/trace")
def lead_trace(entity_id: str) -> dict:
    with connection() as conn:
        ent = get_entity(conn, entity_id)
        if ent is None:
            raise HTTPException(status_code=404, detail="unknown entity")
        trace = get_lead_trace(conn, entity_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="no trace yet")
        name = ent["canonical_name"]
        cp = conn.execute(
            "SELECT cost_usd FROM lead_checkpoints WHERE entity_id = ?", (entity_id,)
        ).fetchone()
        cost = cp["cost_usd"] if cp else 0.0
        decision = conn.execute(
            "SELECT verdict, rationale FROM decisions WHERE entity_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (entity_id,),
        ).fetchone()
        rejection = conn.execute(
            "SELECT reason_code FROM rejections WHERE entity_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (entity_id,),
        ).fetchone()
        if decision:
            verdict = decision["verdict"]
            reason_code = None
            rationale = decision["rationale"]
        elif rejection:
            verdict = "reject"
            reason_code = rejection["reason_code"]
            rationale = None
        else:
            verdict = None
            reason_code = None
            rationale = None
    return {
        "entity_id": entity_id,
        "name": name,
        "verdict": verdict,
        "reason_code": reason_code,
        "rationale": rationale,
        "cost_usd": cost,
        "trace": trace,
    }


# ============================================================================
# T35.6 - provenance log read endpoints. Every response carries schema_version=1.
# One short-lived connection() per request, real work delegated to app.db helpers
# imported as module-level names (same pattern as the rest of this module). The
# /api/runs/{run_id}/provenance route reads the STORED field_provenance rows, never
# rebuilds from the ledger -- the table is the snapshot of what that run shipped; the
# ledger has moved on since, and rebuilding would describe a value the run did not.
# ============================================================================

@app.get("/api/runs")
def list_runs_endpoint(limit: int = 50) -> dict:
    # Clamp 1..200 so a caller asking for 10000 cannot pull the whole table.
    limit = max(1, min(limit, 200))
    with connection() as conn:
        runs = list_runs(conn, limit=limit)
    return {"schema_version": 1, "runs": runs}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str) -> dict:
    with connection() as conn:
        run = get_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="unknown run_id")
        # Per-lead summary computed from field_provenance for this run -- counts only,
        # so the page can render a run overview without parsing every record blob.
        rows = get_field_provenance(conn, run_id=run_id)
    by_entity: dict[str, dict] = {}
    for r in rows:
        eid = r["entity_id"]
        bucket = by_entity.setdefault(eid, {"entity_id": eid, "field_count": 0,
                                            "shipped_count": 0, "blank_count": 0,
                                            "_canonical_name": None})
        bucket["field_count"] += 1
        if r["shipped"]:
            bucket["shipped_count"] += 1
        else:
            bucket["blank_count"] += 1
        # the stored record carries canonical_name; fall back to get_entity below
        if bucket["_canonical_name"] is None:
            rec = r.get("record") or {}
            bucket["_canonical_name"] = rec.get("canonical_name") if isinstance(rec, dict) else None
    with connection() as conn:
        leads = []
        for eid, bucket in by_entity.items():
            name = bucket["_canonical_name"]
            if name is None:
                ent = get_entity(conn, eid)
                name = ent["canonical_name"] if ent else eid
            leads.append({
                "entity_id": eid,
                "canonical_name": name,
                "field_count": bucket["field_count"],
                "shipped_count": bucket["shipped_count"],
                "blank_count": bucket["blank_count"],
            })
    leads.sort(key=lambda l: l["entity_id"])
    return {"schema_version": 1, "run": run, "leads": leads}


@app.get("/api/runs/{run_id}/provenance")
def run_provenance(run_id: str, entity_id: str | None = None, field: str | None = None) -> dict:
    with connection() as conn:
        if get_run(conn, run_id) is None:
            raise HTTPException(status_code=404, detail="unknown run_id")
        rows = get_field_provenance(conn, run_id=run_id, entity_id=entity_id)
    # field filter applied after the entity_id filter (the helper filters entity_id;
    # field is a column on the row, filtered here). Both absent = the whole run.
    if field is not None:
        rows = [r for r in rows if r.get("field") == field]
    records = [r["record"] for r in rows]
    return {"schema_version": 1, "run_id": run_id, "count": len(records),
            "records": records}


async def _lead_provenance(entity_id: str, run_id: str | None) -> dict:
    """Shared handler for the lead-provenance route, registered under both the plural
    /api/leads/{entity_id}/provenance (as PLAN.md T35.6 specifies) and the singular
    /api/lead/{entity_id}/provenance (so a caller following the existing
    /api/lead/{entity_id}/trace convention is not surprised). The singular/plural
    inconsistency is deliberate on the spec side only in that the existing trace route
    is NOT being renamed this round."""
    with connection() as conn:
        ent = get_entity(conn, entity_id)
        if ent is None:
            raise HTTPException(status_code=404, detail="unknown entity")
        canonical_name = ent["canonical_name"]
        if run_id is None:
            # Default to the newest run that has ANY records for this lead -- not merely
            # the newest run. A lead absent from the latest run must still return its
            # most recent log, so the join is on field_provenance for this entity, not
            # on list_runs().
            row = conn.execute(
                """
                SELECT fp.run_id
                FROM field_provenance fp
                JOIN runs r ON r.run_id = fp.run_id
                WHERE fp.entity_id = ?
                ORDER BY r.started_at DESC, fp.id DESC
                LIMIT 1
                """,
                (entity_id,),
            ).fetchone()
            used_run_id = row["run_id"] if row is not None else None
        else:
            used_run_id = run_id
        if used_run_id is None:
            # A known entity with no provenance records at all -> 200, empty list.
            return {"schema_version": 1, "entity_id": entity_id, "run_id": None,
                    "canonical_name": canonical_name, "count": 0, "records": []}
        rows = get_field_provenance(conn, run_id=used_run_id, entity_id=entity_id)
    records = [r["record"] for r in rows]
    return {"schema_version": 1, "entity_id": entity_id, "run_id": used_run_id,
            "canonical_name": canonical_name, "count": len(records), "records": records}


@app.get("/api/leads/{entity_id}/provenance")
async def lead_provenance_plural(entity_id: str, run_id: str | None = None) -> dict:
    return await _lead_provenance(entity_id, run_id)


@app.get("/api/lead/{entity_id}/provenance")
async def lead_provenance_singular(entity_id: str, run_id: str | None = None) -> dict:
    # Same handler as the plural route -- see _lead_provenance's docstring for why both
    # prefixes are registered.
    return await _lead_provenance(entity_id, run_id)


# ============================================================================
# T36 - scheduler. Standing orders live in the `schedules` table; a firing produces an
# ordinary `runs` row with kind='scheduled', so the Log tab reads scheduled runs through
# the same /api/runs endpoints as any other run. Every response carries schema_version=1.
# ============================================================================


@app.get("/api/schedules")
def schedules_list() -> dict:
    with connection() as conn:
        rows = list_schedules(conn)
    return {"schema_version": 1, "schedules": rows}


@app.post("/api/schedules")
def schedules_create(body: ScheduleBody) -> dict:
    if not body.name:
        raise HTTPException(status_code=400, detail="name is required")
    try:
        with connection() as conn:
            schedule_id = create_schedule(
                conn, body.name, body.kind or "daily",
                time_utc=body.time_utc, interval_minutes=body.interval_minutes,
                target_confirmed=body.target_confirmed, max_leads=body.max_leads,
                enabled=True if body.enabled is None else body.enabled,
            )
            created = get_schedule(conn, schedule_id)
    except ValueError as exc:
        # A malformed timing spec is the caller's mistake, not a server fault.
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"schema_version": 1, "schedule": created}


@app.patch("/api/schedules/{schedule_id}")
def schedules_update(schedule_id: str, body: ScheduleBody) -> dict:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        with connection() as conn:
            updated = update_schedule(conn, schedule_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail="unknown schedule_id")
    return {"schema_version": 1, "schedule": updated}


@app.delete("/api/schedules/{schedule_id}")
def schedules_delete(schedule_id: str) -> dict:
    with connection() as conn:
        deleted = delete_schedule(conn, schedule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="unknown schedule_id")
    return {"schema_version": 1, "deleted": schedule_id}


@app.post("/api/schedules/{schedule_id}/run-now")
async def schedules_run_now(schedule_id: str) -> dict:
    """Fire a schedule immediately, without waiting for its next window. The run happens
    in a background task (a full agent run takes minutes); the response returns as soon as
    it is started, and the Log tab shows it like any other run."""
    with connection() as conn:
        schedule = get_schedule(conn, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="unknown schedule_id")
    task = asyncio.create_task(run_scheduled_job(schedule))
    # Strong reference, same reason as _track_run's: the loop holds only a weak one.
    _SCHEDULER.setdefault("adhoc", set()).add(task)
    task.add_done_callback(lambda t: _SCHEDULER["adhoc"].discard(t))
    return {"schema_version": 1, "started": True, "schedule_id": schedule_id}


@app.get("/api/scheduler/status")
def scheduler_status() -> dict:
    """Whether the loop is actually running (not merely configured), the next schedule
    due, and the RAG queue depth — the three things you want when a scheduled run did not
    produce what you expected."""
    task = _SCHEDULER.get("task")
    running = bool(task is not None and not task.done())
    with connection() as conn:
        schedules = list_schedules(conn)
        rag = queue_counts(conn)
    upcoming = sorted(
        (s for s in schedules if s["enabled"] and s.get("next_run_at")),
        key=lambda s: s["next_run_at"],
    )
    return {
        "schema_version": 1,
        "loop_running": running,
        "enabled": bool(_SCHEDULER.get("enabled")),
        "schedule_count": len(schedules),
        "next_due": upcoming[0] if upcoming else None,
        "rag_queue": rag,
    }


@app.post("/api/rag/drain")
async def rag_drain(limit: int = 25) -> dict:
    """Drain the micro-RAG ingest queue on demand. Ingestion already happens at the end of
    every scheduled run; this exists for the case the RAG was down then and the rows are
    still pending. Never fails — see app/rag_sync.drain_queue's contract."""
    limit = max(1, min(limit, 200))
    result = await asyncio.to_thread(drain_queue, None, limit=limit)
    return {"schema_version": 1, **result}


@app.post("/api/log/sync")
async def log_sync_endpoint(limit: int = 50) -> dict:
    """Push the recent run log to Postgres so the hosted view shows it. Runs
    automatically at the end of every scheduled run; this is the manual trigger for a
    run done from the Run tab, or a re-push after the database was unreachable."""
    limit = max(1, min(limit, 200))
    result = await asyncio.to_thread(sync_runs, None, limit=limit)
    return {"schema_version": 1, **result}


@app.get("/")
def index() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not built")
    return FileResponse(index_path)
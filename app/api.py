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
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.db import connection, get_entity, get_lead_trace, init_db
from app.ingest import ingest_discovery_file
from app.runner import run_batch

DISCOVERY_JSONL_PATH = "data/fo_discovery_c1-c3-c4-c8_20260728T090511Z.jsonl"
STATIC_DIR = Path(__file__).parent / "static"

# run_id -> {"entity_ids", "status", "started_at", "result", "task"}. Module-level,
# in-memory: lost on restart. See module docstring for why that's acceptable here.
RUNS: dict[str, dict] = {}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Create the schema once at startup, before any request is served. The first call
    # the frontend makes (GET /api/leads/available) never goes through run_batch, which
    # is the only other place init_db() gets called — so without this a fresh checkout
    # 500's on page load with "no such table: entities".
    init_db()
    yield


app = FastAPI(title="FO Intelligence Agent — Research Layer", lifespan=_lifespan)


class RunBody(BaseModel):
    count: int | None = None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_order() -> list[str]:
    """Ingest (idempotent) and return the ordered entity_id list. Safe to call every
    request — `ingest_discovery_file` upserts, so a warm DB is a no-op read."""
    with connection() as conn:
        return ingest_discovery_file(conn, DISCOVERY_JSONL_PATH)


async def _track_run(run_id: str, entity_ids: list[str]) -> None:
    """Background task: runs the batch, then flips the run's status to done. The POST
    handler returns immediately without awaiting this."""
    try:
        result = await run_batch(
            entity_ids=entity_ids, resume=False, skip_preflight=False
        )
        RUNS[run_id]["status"] = "done"
        RUNS[run_id]["result"] = result
    except Exception as exc:  # noqa: BLE001 — a failed batch shouldn't kill the task silently
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


@app.get("/")
def index() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not built")
    return FileResponse(index_path)
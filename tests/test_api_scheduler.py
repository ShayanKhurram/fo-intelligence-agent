"""T36.4 — the scheduler's HTTP surface (app/api.py), which is what the Log tab drives.

Same ASGITransport client as tests/test_api_provenance.py: the installed starlette/httpx
combination breaks starlette's own TestClient, and the lifespan does not run under
ASGITransport — which is convenient here, since it means the background scheduler loop is
never armed by a test.
"""
from __future__ import annotations

import httpx
import pytest

import app.api as api_mod
import app.db as db_mod
import app.scheduler as sched_mod
from app.config import Settings
from app.db import connection, init_db, upsert_entity
from app.rag_sync import enqueue_entity


@pytest.fixture
async def client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "sched.db")
    init_db(db_path)
    monkeypatch.setattr(db_mod, "SETTINGS", Settings(db_path=db_path))
    transport = httpx.ASGITransport(app=api_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        c.db_path = db_path
        yield c


async def test_create_list_patch_delete_roundtrip(client):
    r = await client.post("/api/schedules", json={
        "name": "nightly", "kind": "daily", "time_utc": "03:00",
        "target_confirmed": 10, "max_leads": 50,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["schema_version"] == 1
    sid = body["schedule"]["schedule_id"]
    assert body["schedule"]["enabled"] is True
    assert body["schedule"]["next_run_at"]

    r = await client.get("/api/schedules")
    assert [s["schedule_id"] for s in r.json()["schedules"]] == [sid]

    r = await client.patch(f"/api/schedules/{sid}", json={"enabled": False, "target_confirmed": 3})
    assert r.status_code == 200
    assert r.json()["schedule"]["enabled"] is False
    assert r.json()["schedule"]["target_confirmed"] == 3

    r = await client.delete(f"/api/schedules/{sid}")
    assert r.status_code == 200
    assert (await client.get("/api/schedules")).json()["schedules"] == []


async def test_a_malformed_timing_spec_is_a_400_not_a_500(client):
    """The caller's mistake must read as the caller's mistake."""
    r = await client.post("/api/schedules", json={"name": "bad", "kind": "daily"})
    assert r.status_code == 400
    assert "time_utc" in r.json()["detail"]

    r = await client.post("/api/schedules", json={"name": "bad", "kind": "weekly", "time_utc": "03:00"})
    assert r.status_code == 400


async def test_name_is_required(client):
    r = await client.post("/api/schedules", json={"kind": "interval", "interval_minutes": 5})
    assert r.status_code == 400


async def test_unknown_schedule_is_404_on_patch_and_delete(client):
    assert (await client.patch("/api/schedules/nope", json={"enabled": False})).status_code == 404
    assert (await client.delete("/api/schedules/nope")).status_code == 404
    assert (await client.post("/api/schedules/nope/run-now")).status_code == 404


async def test_status_reports_loop_state_next_due_and_rag_depth(client):
    await client.post("/api/schedules", json={
        "name": "later", "kind": "daily", "time_utc": "23:59"})
    await client.post("/api/schedules", json={
        "name": "sooner", "kind": "interval", "interval_minutes": 1})
    with connection(client.db_path) as conn:
        enqueue_entity(conn, "e1")

    r = await client.get("/api/scheduler/status")
    body = r.json()
    assert body["schema_version"] == 1
    # The loop is not armed under ASGITransport (no lifespan) — and the endpoint reports
    # what IS, not what was configured.
    assert body["loop_running"] is False
    assert body["schedule_count"] == 2
    assert body["next_due"]["name"] == "sooner"     # 1 minute out beats 23:59
    assert body["rag_queue"] == {"pending": 1}


async def test_run_now_starts_a_run_without_waiting_for_the_window(client, monkeypatch):
    started: list[str] = []

    async def fake_job(schedule, **kwargs):
        started.append(schedule["schedule_id"])
        return {"run_id": "r1", "processed": 0, "confirmed": 0,
                "termination": "leads_exhausted", "usd_spent": 0.0, "rag": {}}

    monkeypatch.setattr(api_mod, "run_scheduled_job", fake_job)
    sid = (await client.post("/api/schedules", json={
        "name": "adhoc", "kind": "interval", "interval_minutes": 60})).json()["schedule"]["schedule_id"]

    r = await client.post(f"/api/schedules/{sid}/run-now")
    assert r.status_code == 200 and r.json()["started"] is True
    # It is a background task: give the loop a turn to actually run it.
    import asyncio
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert started == [sid]


async def test_rag_drain_endpoint_degrades_cleanly_without_a_database_url(client, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    with connection(client.db_path) as conn:
        enqueue_entity(conn, "e1")
    r = await client.post("/api/rag/drain")
    assert r.status_code == 200
    assert r.json()["status"] == "skipped"


async def test_a_scheduled_run_shows_up_in_the_log_tab_endpoints(client, monkeypatch):
    """The Log tab's contract, end to end over HTTP: a scheduled run appears in
    /api/runs, its leads in /api/runs/{id}, and a lead's field log in
    /api/runs/{id}/provenance?entity_id=."""
    async def fake_run_lead(entity_id, db_path=None):
        return {"cost_usd": 0.0}

    async def fake_process_entity(conn, entity_id, model, *, force=False):
        return {"entity_id": entity_id, "outcome": "ship", "calls_spent": 0, "usd_spent": 0.0}

    monkeypatch.setattr(sched_mod, "run_lead", fake_run_lead)
    monkeypatch.setattr(sched_mod, "process_entity", fake_process_entity)
    with connection(client.db_path) as conn:
        upsert_entity(conn, "e0", "Acme FO")
        conn.execute("INSERT INTO decisions (entity_id, verdict, rationale) VALUES ('e0','pursue','')")

    result = await sched_mod.run_scheduled_job(
        {"schedule_id": "s1", "name": "nightly", "target_confirmed": None, "max_leads": None},
        db_path=client.db_path, model=object(),
    )
    run_id = result["run_id"]

    runs = (await client.get("/api/runs")).json()["runs"]
    assert run_id in [r["run_id"] for r in runs]
    scheduled = next(r for r in runs if r["run_id"] == run_id)
    assert scheduled["kind"] == "scheduled"
    assert scheduled["notes"]["schedule_name"] == "nightly"
    assert scheduled["notes"]["termination"] == "leads_exhausted"

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    assert [l["entity_id"] for l in detail["leads"]] == ["e0"]

    prov = (await client.get(f"/api/runs/{run_id}/provenance", params={"entity_id": "e0"})).json()
    assert prov["count"] > 0
    assert {r["entity_id"] for r in prov["records"]} == {"e0"}
    # Every record explains itself: a value with a `how`, or a blank with a reason.
    for rec in prov["records"]:
        assert rec["value"] is not None or rec["blank_reason"] is not None

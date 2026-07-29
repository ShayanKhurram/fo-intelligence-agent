"""Tests for the FastAPI web layer (app/api.py). No real network or LLM calls: both
`ingest_discovery_file` and `run_batch` are monkeypatched on the `app.api` module (the
same pattern tests/test_runner.py uses for `run_lead`), and every DB access is pointed
at a temp SQLite DB by swapping `app.db.SETTINGS` for a Settings rooted at the test path.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api as api_mod
import app.db as db_mod
from app.config import Settings
from app.db import connection, init_db, upsert_entity
from app.runner import BatchResult


@pytest.fixture
def api_client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "api_test.db")
    init_db(db_path)
    # Point every connection() the API opens at the temp DB (app.db.connection reads
    # SETTINGS.db_path at call time via the module-level SETTINGS reference, so swapping
    # the whole object on app.db is what does it).
    monkeypatch.setattr(db_mod, "SETTINGS", Settings(db_path=db_path))

    with connection(db_path) as conn:
        for eid, name in [
            ("E1", "Acme Family Office"),
            ("E2", "Beta Capital Partners"),
            ("E3", "Gamma Wealth"),
            ("E4", "Delta Advisors"),
            ("E5", "Epsilon Trust"),
        ]:
            upsert_entity(conn, eid, name)

    order = ["E1", "E2", "E3", "E4", "E5"]

    def fake_ingest(conn, path):
        return list(order)

    async def fake_run_batch(entity_ids=None, db_path=None, resume=False, skip_preflight=False):
        return BatchResult()

    monkeypatch.setattr(api_mod, "ingest_discovery_file", fake_ingest)
    monkeypatch.setattr(api_mod, "run_batch", fake_run_batch)

    with TestClient(api_mod.app) as client:
        yield client


def test_leads_available_returns_mocked_total(api_client):
    r = api_client.get("/api/leads/available")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    assert len(body["sample_names"]) == 5
    assert body["sample_names"][0] == "Acme Family Office"


def test_run_with_count_returns_run_id_and_entity_ids(api_client):
    r = api_client.post("/api/run", json={"count": 2})
    assert r.status_code == 200
    body = r.json()
    assert "run_id" in body and body["run_id"]
    assert body["total"] == 2
    assert body["entity_ids"] == ["E1", "E2"]


def test_run_with_null_count_uses_full_list(api_client):
    r = api_client.post("/api/run", json={"count": None})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    assert body["entity_ids"] == ["E1", "E2", "E3", "E4", "E5"]


def test_run_with_no_body_uses_full_list(api_client):
    r = api_client.post("/api/run")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5


def test_run_status_unknown_run_returns_404(api_client):
    r = api_client.get("/api/run/nonexistent-id/status")
    assert r.status_code == 404


def test_run_status_known_run_returns_lead_status(api_client):
    r = api_client.post("/api/run", json={"count": 3})
    run_id = r.json()["run_id"]
    s = api_client.get("/api/run/" + run_id + "/status")
    assert s.status_code == 200
    body = s.json()
    assert body["total"] == 3
    assert len(body["leads"]) == 3
    # No checkpoint rows yet -> all queued.
    assert all(lead["status"] == "queued" for lead in body["leads"])
    assert all(lead["verdict"] is None for lead in body["leads"])


def test_lead_trace_no_trace_returns_404(api_client):
    # E1 exists in the DB (seeded) but has no lead_traces row -> 404.
    r = api_client.get("/api/lead/E1/trace")
    assert r.status_code == 404


def test_lead_trace_unknown_entity_returns_404(api_client):
    r = api_client.get("/api/lead/does-not-exist/trace")
    assert r.status_code == 404


def test_garbage_route_returns_404_not_500(api_client):
    r = api_client.get("/GARBAGE")
    assert r.status_code == 404


def test_run_with_count_zero_returns_empty_not_all(api_client):
    # Regression: `count == 0` is falsy, so a naive `if count` would run the entire feed.
    r = api_client.post("/api/run", json={"count": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["entity_ids"] == []


def test_leads_available_works_on_a_db_that_never_had_init_db_called(monkeypatch, tmp_path):
    # Regression for the first-run bug: app/api.py itself must create the schema at
    # startup, not rely on something else (run_batch / a test fixture) having done it.
    # This DB path did not exist and init_db() is deliberately NOT called here — the
    # lifespan handler is the only thing that should create it.
    fresh_path = str(tmp_path / "never_init.db")
    assert not Path(fresh_path).exists()
    monkeypatch.setattr(db_mod, "SETTINGS", Settings(db_path=fresh_path))

    order = ["E1", "E2", "E3"]
    monkeypatch.setattr(api_mod, "ingest_discovery_file", lambda conn, path: list(order))
    async def fake_run_batch(**kw):
        return BatchResult()
    monkeypatch.setattr(api_mod, "run_batch", fake_run_batch)

    with TestClient(api_mod.app) as client:
        r = client.get("/api/leads/available")
    assert r.status_code == 200
    assert r.json()["total"] == 3
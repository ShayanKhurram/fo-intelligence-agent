"""T35.6 - the provenance log read endpoints (app/api.py). Hit via httpx.AsyncClient
with an ASGITransport because the installed starlette 0.35 / httpx 0.28 combo breaks
starlette's TestClient (it passes `app=` to httpx.Client, which no longer accepts it --
the pre-existing tests/test_api.py failures). ASGITransport does not run the FastAPI
lifespan, so each test inits the schema itself and points `app.db.SETTINGS` at a tmp DB.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import app.api as api_mod
import app.db as db_mod
from app.config import Settings
from app.db import (
    connection,
    get_tool_calls,
    init_db,
    start_run,
    finish_run,
    upsert_claims,
    upsert_entity,
    write_field_provenance,
    write_tool_call,
)
from app.provenance_log import build_field_records


@pytest.fixture
async def client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "prov.db")
    init_db(db_path)
    monkeypatch.setattr(db_mod, "SETTINGS", Settings(db_path=db_path))
    transport = httpx.ASGITransport(app=api_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def _seed_run(db_path, run_id, *, entity_claims, started_at=None):
    """Create a run and persist provenance rows for each (entity_id, claims) pair, built
    via build_field_records so the stored records are realistic schema_version-1 blobs."""
    with connection(db_path) as conn:
        for eid, name, claims in entity_claims:
            upsert_entity(conn, eid, name)
            upsert_claims(conn, eid, claims)
        start_run(conn, "enrichment", run_id=run_id, entity_count=len(entity_claims))
        if started_at is not None:
            conn.execute(
                "UPDATE runs SET started_at = ? WHERE run_id = ?", (started_at, run_id)
            )
        rows = []
        for eid, _name, _claims in entity_claims:
            for rec in build_field_records(conn, eid, run_id=run_id):
                rows.append({
                    "run_id": run_id, "entity_id": eid, "field": rec["field"],
                    "value": rec["value"], "status": rec["status"], "shipped": rec["shipped"],
                    "source_class": (rec["how"] or {}).get("source_class"),
                    "extraction_method": (rec["how"] or {}).get("extraction_method"),
                    "record": json.dumps(rec, ensure_ascii=False, default=str),
                })
        write_field_provenance(conn, rows)
        finish_run(conn, run_id, status="done")
    return run_id


def _claim(field, answer, *, status="confirmed", extraction_method="projected_G2.Q1",
           source_class="research", confidence="high", claim_id="c", created_at="2026-08-01T00:00:00Z"):
    return {"field_name": field, "answer": answer, "status": status,
            "extraction_method": extraction_method, "source_class": source_class,
            "confidence": confidence, "claim_id": claim_id, "created_at": created_at}


# --- envelope + schema_version -------------------------------------------------

async def test_list_runs_envelope(client, tmp_path):
    db_path = str(tmp_path / "prov.db")
    _seed_run(db_path, "run-1", entity_claims=[
        ("e1", "Acme FO", [_claim("principal_name", "Matt", claim_id="c1")]),
    ])
    r = await client.get("/api/runs")
    assert r.status_code == 200
    body = r.json()
    assert body["schema_version"] == 1
    assert isinstance(body["runs"], list)
    assert body["runs"][0]["run_id"] == "run-1"
    for k in ("run_id", "kind", "status", "git_sha", "entity_count",
              "started_at", "ended_at", "notes"):
        assert k in body["runs"][0]


async def test_run_detail_envelope(client, tmp_path):
    db_path = str(tmp_path / "prov.db")
    _seed_run(db_path, "run-1", entity_claims=[
        ("e1", "Acme FO", [_claim("principal_name", "Matt", claim_id="c1")]),
        ("e2", "Beta FO", [_claim("principal_name", None, status="could_not_verify",
                                   extraction_method="snov_no_match", claim_id="c2")]),
    ])
    r = await client.get("/api/runs/run-1")
    assert r.status_code == 200
    body = r.json()
    assert body["schema_version"] == 1
    assert body["run"]["run_id"] == "run-1"
    leads = {l["entity_id"]: l for l in body["leads"]}
    assert leads["e1"]["shipped_count"] >= 1
    # e2 has a could_not_verify principal_name -> at least one blank row
    assert leads["e2"]["blank_count"] >= 1
    assert leads["e1"]["field_count"] == leads["e1"]["shipped_count"] + leads["e1"]["blank_count"]


async def test_run_provenance_envelope(client, tmp_path):
    db_path = str(tmp_path / "prov.db")
    _seed_run(db_path, "run-1", entity_claims=[
        ("e1", "Acme FO", [_claim("principal_name", "Matt", claim_id="c1")]),
    ])
    r = await client.get("/api/runs/run-1/provenance")
    assert r.status_code == 200
    body = r.json()
    assert body["schema_version"] == 1
    assert body["run_id"] == "run-1"
    assert body["count"] == len(body["records"])
    assert body["records"][0]["schema_version"] == 1


async def test_lead_provenance_envelope(client, tmp_path):
    db_path = str(tmp_path / "prov.db")
    _seed_run(db_path, "run-1", entity_claims=[
        ("e1", "Acme FO", [_claim("principal_name", "Matt", claim_id="c1")]),
    ])
    r = await client.get("/api/leads/e1/provenance")
    assert r.status_code == 200
    body = r.json()
    assert body["schema_version"] == 1
    assert body["entity_id"] == "e1"
    assert body["run_id"] == "run-1"
    assert body["canonical_name"] == "Acme FO"
    assert body["count"] == len(body["records"])


# --- limit + clamp -------------------------------------------------------------

async def test_list_runs_respects_limit_and_clamp(client, tmp_path):
    db_path = str(tmp_path / "prov.db")
    for i in range(5):
        _seed_run(db_path, f"run-{i}", entity_claims=[
            ("e1", "Acme FO", [_claim("principal_name", f"Name{i}", claim_id=f"c{i}")]),
        ])
    # limit=2 returns at most 2
    r = await client.get("/api/runs", params={"limit": 2})
    assert len(r.json()["runs"]) <= 2
    # limit=10000 is clamped to 200 (and there are only 5 runs, so 5 come back)
    r = await client.get("/api/runs", params={"limit": 10000})
    assert len(r.json()["runs"]) == 5
    # limit=0 is clamped up to 1
    r = await client.get("/api/runs", params={"limit": 0})
    assert len(r.json()["runs"]) == 1
    # negative is clamped up to 1
    r = await client.get("/api/runs", params={"limit": -5})
    assert len(r.json()["runs"]) == 1


# --- filtered provenance is a strict subset; non-matching filter is 200 [] -------

async def test_filtered_provenance_is_strict_subset(client, tmp_path):
    db_path = str(tmp_path / "prov.db")
    _seed_run(db_path, "run-1", entity_claims=[
        ("e1", "Acme FO", [_claim("principal_name", "Matt", claim_id="c1"),
                            _claim("aum_usd", 100, extraction_method="derived_13f",
                                   source_class="13f_filing", claim_id="c2")]),
        ("e2", "Beta FO", [_claim("principal_name", "Jane", claim_id="c3")]),
    ])
    full = (await client.get("/api/runs/run-1/provenance")).json()
    by_entity = (await client.get("/api/runs/run-1/provenance",
                                  params={"entity_id": "e1"})).json()
    by_field = (await client.get("/api/runs/run-1/provenance",
                                 params={"field": "principal_name"})).json()
    both = (await client.get("/api/runs/run-1/provenance",
                            params={"entity_id": "e1", "field": "principal_name"})).json()

    full_keys = {(r["entity_id"], r["field"]) for r in full["records"]}
    assert {("e1", "principal_name"), ("e1", "aum_usd"), ("e2", "principal_name")} <= full_keys
    assert {(r["entity_id"], r["field"]) for r in by_entity["records"]} <= full_keys
    assert all(r["entity_id"] == "e1" for r in by_entity["records"])
    assert {(r["entity_id"], r["field"]) for r in by_field["records"]} <= full_keys
    assert all(r["field"] == "principal_name" for r in by_field["records"])
    # both filters together -> strict subset of each single filter
    assert {(r["entity_id"], r["field"]) for r in both["records"]} <= \
           {(r["entity_id"], r["field"]) for r in by_entity["records"]}
    assert both["records"]
    assert all(r["entity_id"] == "e1" and r["field"] == "principal_name" for r in both["records"])

    # a non-matching filter is 200 with an empty list, NOT a 404
    nm = await client.get("/api/runs/run-1/provenance", params={"field": "nonexistent_field"})
    assert nm.status_code == 200
    assert nm.json()["records"] == []
    assert nm.json()["count"] == 0


# --- 404s -----------------------------------------------------------------------

async def test_unknown_run_id_404(client, tmp_path):
    r = await client.get("/api/runs/does-not-exist")
    assert r.status_code == 404
    assert "detail" in r.json()

    r2 = await client.get("/api/runs/does-not-exist/provenance")
    assert r2.status_code == 404
    assert "detail" in r2.json()


async def test_unknown_entity_id_404(client, tmp_path):
    r = await client.get("/api/leads/does-not-exist/provenance")
    assert r.status_code == 404
    assert "detail" in r.json()
    # the singular alias 404s too
    r2 = await client.get("/api/lead/does-not-exist/provenance")
    assert r2.status_code == 404


# --- newest run that has rows for the lead (not merely the newest run) -----------

async def test_lead_provenance_defaults_to_newest_run_with_rows(client, tmp_path):
    db_path = str(tmp_path / "prov.db")
    # run-old (earlier) has e1; run-new (later) has e2 only -- e1 is absent from the
    # newest run, so /api/leads/e1/provenance must pick run-old, not run-new.
    _seed_run(db_path, "run-old",
              entity_claims=[("e1", "Acme FO", [_claim("principal_name", "Matt", claim_id="c1")])],
              started_at="2026-08-01T00:00:00Z")
    _seed_run(db_path, "run-new",
              entity_claims=[("e2", "Beta FO", [_claim("principal_name", "Jane", claim_id="c2")])],
              started_at="2026-08-02T00:00:00Z")
    r = await client.get("/api/leads/e1/provenance")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == "run-old"
    assert body["count"] >= 1
    # e2 under the same default picks the newer run-new
    r2 = await client.get("/api/leads/e2/provenance")
    assert r2.json()["run_id"] == "run-new"


async def test_lead_provenance_known_entity_no_records(client, tmp_path):
    db_path = str(tmp_path / "prov.db")
    with connection(db_path) as conn:
        upsert_entity(conn, "e9", "Lonely FO")
    r = await client.get("/api/leads/e9/provenance")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] is None
    assert body["records"] == []
    assert body["count"] == 0


# --- the snapshot property: records served are the STORED ones ------------------

async def test_records_are_the_stored_snapshot_not_rebuilt(client, tmp_path):
    db_path = str(tmp_path / "prov.db")
    _seed_run(db_path, "run-1", entity_claims=[
        ("e1", "Acme FO", [_claim("principal_name", "Matt", claim_id="c1")]),
    ])
    # the stored record's value is "Matt"; mutate the underlying claim to "CHANGED"
    # and assert the endpoint still returns the run's snapshot value.
    with connection(db_path) as conn:
        upsert_claims(conn, "e1", [
            {"field_name": "principal_name", "answer": "CHANGED", "status": "confirmed",
             "extraction_method": "projected_G2.Q1", "source_class": "research",
             "confidence": "high", "claim_id": "c1", "created_at": "2026-09-01T00:00:00Z"},
        ])
    r = await client.get("/api/runs/run-1/provenance", params={"field": "principal_name"})
    assert r.status_code == 200
    rec = r.json()["records"][0]
    assert rec["value"] == "Matt", "endpoint rebuilt from the ledger instead of reading the snapshot"


# --- singular alias serves the same records as the plural route -----------------

async def test_singular_alias_matches_plural(client, tmp_path):
    db_path = str(tmp_path / "prov.db")
    _seed_run(db_path, "run-1", entity_claims=[
        ("e1", "Acme FO", [_claim("principal_name", "Matt", claim_id="c1")]),
    ])
    plural = await client.get("/api/leads/e1/provenance")
    singular = await client.get("/api/lead/e1/provenance")
    assert plural.json()["records"] == singular.json()["records"]


# --- Nit 1: write_tool_call defaults ok/cache_hit when omitted -------------------

def test_write_tool_call_defaults_ok_and_cache_hit(tmp_path):
    db_path = str(tmp_path / "nit.db")
    init_db(db_path)
    with connection(db_path) as conn:
        write_tool_call(conn, entity_id="e1", tool="t")  # neither ok nor cache_hit
        rows = get_tool_calls(conn, "e1")
    assert len(rows) == 1
    assert rows[0]["ok"] is True
    assert rows[0]["cache_hit"] is False
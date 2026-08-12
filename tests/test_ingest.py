from __future__ import annotations

import json

from app.db import connection, get_entity_sources
from app.ingest import ingest_discovery_file


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_ingest_maps_edgar_13f_to_13f_filing(db_path, tmp_path):
    jsonl = tmp_path / "discovery.jsonl"
    _write_jsonl(jsonl, [
        {
            "entity_name_raw": "Acme Family Office LLC",
            "discovery_class": "edgar_13f",
            "discovery_url": "http://sec.gov/x",
            "state": "NY",
            "signals": {"aum_13f": 1000.0, "position_count": 5},
            "raw_payload": {"year": 2026, "qtr": 1, "primary_doc": {"tableValueTotal": "1000"}},
        }
    ])
    with connection(db_path) as conn:
        order = ingest_discovery_file(conn, jsonl)
        assert len(order) == 1
        sources = get_entity_sources(conn, order[0])
        assert sources[0]["source_class"] == "13f_filing"
        # A 13F FILED in 2026Q1 reports holdings as of 31 Dec 2025 (2025Q4). The feed's
        # year/qtr is the filing bucket; `quarter` is now the holdings period.
        assert sources[0]["payload"]["quarter"] == "2025Q4"
        assert sources[0]["payload"]["filed_in_quarter"] == "2026Q1"
        assert sources[0]["payload"]["value_usd"] == 1000


def test_ingest_maps_dol_5500_to_5500_filing(db_path, tmp_path):
    jsonl = tmp_path / "discovery.jsonl"
    _write_jsonl(jsonl, [
        {
            "entity_name_raw": "Beta Family Office",
            "discovery_class": "dol_5500",
            "discovery_url": "http://dol.gov/x",
            "signals": {"plan_year": "2025", "participant_count": 6},
            "raw_payload": {},
        }
    ])
    with connection(db_path) as conn:
        order = ingest_discovery_file(conn, jsonl)
        sources = get_entity_sources(conn, order[0])
        assert sources[0]["source_class"] == "5500_filing"
        assert sources[0]["payload"]["plan_year"] == 2025
        assert sources[0]["payload"]["participant_count"] == 6


def test_ingest_passes_through_unrecognized_discovery_class(db_path, tmp_path):
    jsonl = tmp_path / "discovery.jsonl"
    _write_jsonl(jsonl, [
        {
            "entity_name_raw": "Gamma Family Office",
            "discovery_class": "fec_employer",
            "discovery_url": "http://fec.gov/x",
            "people": [{"name": "Jane Doe", "title": "Executive"}],
            "signals": {"contribution_date": "2026-01-01"},
            "raw_payload": {},
        }
    ])
    with connection(db_path) as conn:
        order = ingest_discovery_file(conn, jsonl)
        sources = get_entity_sources(conn, order[0])
        assert sources[0]["source_class"] == "fec_employer"
        assert sources[0]["payload"]["people"][0]["name"] == "Jane Doe"


def test_ingest_groups_records_by_normalized_name(db_path, tmp_path):
    jsonl = tmp_path / "discovery.jsonl"
    _write_jsonl(jsonl, [
        {"entity_name_raw": "Acme Family Office LLC", "discovery_class": "dol_5500",
         "signals": {}, "raw_payload": {}},
        {"entity_name_raw": "  acme family office llc  ", "discovery_class": "ppp_loans",
         "signals": {}, "raw_payload": {}},
    ])
    with connection(db_path) as conn:
        order = ingest_discovery_file(conn, jsonl)
        assert len(order) == 1  # same normalized name -> one entity
        sources = get_entity_sources(conn, order[0])
        assert len(sources) == 2
        assert {s["source_class"] for s in sources} == {"5500_filing", "ppp_loans"}


def test_ingest_preserves_file_order_for_first_n_slicing(db_path, tmp_path):
    jsonl = tmp_path / "discovery.jsonl"
    _write_jsonl(jsonl, [
        {"entity_name_raw": f"Entity {i}", "discovery_class": "edgar_13f",
         "signals": {}, "raw_payload": {}}
        for i in range(15)
    ])
    with connection(db_path) as conn:
        order = ingest_discovery_file(conn, jsonl)
    assert len(order) == 15
    first_ten = order[:10]
    assert len(first_ten) == 10
    assert len(set(first_ten)) == 10

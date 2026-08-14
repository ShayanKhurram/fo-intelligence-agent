"""T35.5 — the provenance log is persisted + emitted on every run (PLAN.md T35.5).

Runs a real `run_pipeline` over 2 stub entities (offline — every network tool
monkeypatched, the model faked) and asserts:
  * `field_provenance` rows exist for both entities under the one run_id;
  * a readable `data/runs/<run_id>/field_provenance.json` is written;
  * the drift test, asserted directly: for the same selection, every record with a
    non-null `value` equals the corresponding cell in `records.csv` produced by
    `write_workbook`, and every record whose `value` is null corresponds to a blank
    cell — both directions;
  * re-emitting with the same run_id upserts (row count unchanged), never duplicates.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import app.enrichment as enrichment_module
import app.validation as validation_module
from app.db import (
    connection,
    get_field_provenance,
    upsert_entity,
    write_field_provenance,
)
from app.enrichment import run_pipeline
from langchain_core.messages import AIMessage

from tests.test_enrichment_pipeline import _patch_all_network, _seed_pursue_entity


def _route_model(fake_model):
    """The validation LLM calls must return benign supported/unsupported verdicts so a
    stub entity ships rather than crashing the pipeline."""
    fake_model.route(
        lambda msgs: any("extracting structured facts" in str(m.content) for m in msgs),
        AIMessage(content="[]"),
    )
    fake_model.route(
        lambda msgs: any("checking whether a web page" in str(m.content) for m in msgs),
        *[AIMessage(content='{"supported": true, "reason": "ok"}') for _ in range(20)],
    )


async def test_run_pipeline_emits_field_provenance_for_every_processed_entity(
    db_path, fake_model, monkeypatch, tmp_path
):
    # Keep the run-scoped JSON file under tmp_path so the test never pollutes the repo.
    monkeypatch.chdir(tmp_path)

    with connection(db_path) as conn:
        _seed_pursue_entity(conn, "e1", "Acme Capital Partners")
        _seed_pursue_entity(conn, "e2", "Beta Family Office")
        # give each a real contact channel so wave 1 lands something
        from app.db import add_entity_source
        add_entity_source(conn, "e1", "domain_check", {"domain": "acme.com", "mx_present": True})

    _patch_all_network(
        monkeypatch,
        jsonld_html='<script type="application/ld+json">{"@type":"Person","name":"Jane Doe","jobTitle":"CIO"}</script>',
        page_content="Reach the team at jane@acme.com.",
        snov_emails=[{"email": "jane@acme.com", "first_name": "Jane", "last_name": "Doe"}],
    )
    _route_model(fake_model)

    with connection(db_path) as conn:
        summary = await run_pipeline(conn, fake_model, target_survivors=50)

    run_id = summary["run_id"]
    assert run_id is not None
    processed_ids = {p["entity_id"] for p in summary["processed"]}
    assert {"e1", "e2"} <= processed_ids

    # rows for both entities under the one run_id
    with connection(db_path) as conn:
        rows = get_field_provenance(conn, run_id=run_id)
    by_entity = {r["entity_id"] for r in rows}
    assert {"e1", "e2"} <= by_entity

    # The run-scoped JSON file is written next to the DB the run describes
    # (Path(db_path).parent / "runs" / run_id), not the CWD, so a run against a
    # tmp DB writes next to that tmp DB. Assert the derived path directly.
    provenance_file = Path(db_path).parent / "runs" / run_id / "field_provenance.json"
    assert provenance_file.exists(), f"expected {provenance_file} to exist"
    doc = json.loads(provenance_file.read_text(encoding="utf-8"))
    assert doc["schema_version"] == 1
    assert doc["run"]["run_id"] == run_id
    lead_ids = {l["entity_id"] for l in doc["leads"]}
    assert {"e1", "e2"} <= lead_ids


async def test_drift_record_value_matches_records_csv_cell(
    db_path, fake_model, monkeypatch, tmp_path
):
    """The drift test, asserted directly: every record with a non-null value equals the
    corresponding cell in records.csv; every record whose value is null corresponds to a
    blank cell. Both directions."""
    monkeypatch.chdir(tmp_path)

    with connection(db_path) as conn:
        _seed_pursue_entity(conn, "e1", "Acme Capital Partners")
        from app.db import add_entity_source
        add_entity_source(conn, "e1", "domain_check", {"domain": "acme.com", "mx_present": True})

    _patch_all_network(
        monkeypatch,
        jsonld_html='<script type="application/ld+json">{"@type":"Person","name":"Jane Doe","jobTitle":"CIO"}</script>',
        page_content="Reach the team at jane@acme.com.",
        snov_emails=[{"email": "jane@acme.com", "first_name": "Jane", "last_name": "Doe"}],
    )
    _route_model(fake_model)

    with connection(db_path) as conn:
        summary = await run_pipeline(conn, fake_model, target_survivors=50)
    run_id = summary["run_id"]

    # Build the SAME selection the sheet would ship, and emit records.csv via
    # write_workbook (with its own run_id — the drift test compares VALUES, not runs).
    from app.dataset import gather_survivors, select_50, write_workbook
    entity_outcomes = [(p["entity_id"], p["outcome"]) for p in summary["processed"]]
    with connection(db_path) as conn:
        candidates = gather_survivors(conn, entity_outcomes)
        selected, per_class, excluded = select_50(candidates, n=50)
        out_dir = tmp_path / "workbook"
        paths = write_workbook(conn, selected, per_class, excluded, out_dir, run_id="drift-test")

    with open(paths["csv"], newline="", encoding="utf-8") as f:
        csv_rows = {r["entity_id"]: r for r in csv.DictReader(f)}

    doc = json.loads((Path(db_path).parent / "runs" / run_id / "field_provenance.json").read_text(encoding="utf-8"))

    # every shipped (non-null) record value equals the corresponding csv cell, and every
    # null record value corresponds to a blank cell — both directions.
    for lead in doc["leads"]:
        eid = lead["entity_id"]
        if eid not in csv_rows:
            continue  # an entity processed by the pipeline but not in the sheet selection
        csv_row = csv_rows[eid]
        for rec in lead["fields"]:
            field = rec["field"]
            if field not in csv_row:
                # a companion-only or non-sheet field (e.g. a discovery_class_* field the
                # sheet pivots but the high-value-guaranteed set does not) — skip it.
                continue
            cell = csv_row[field]
            if rec["value"] is not None:
                assert str(rec["value"]) == cell, (
                    f"drift on {eid}.{field}: record={rec['value']!r} csv={cell!r}"
                )
            else:
                assert cell == "" or cell is None, (
                    f"drift on {eid}.{field}: record is null but csv={cell!r}"
                )


async def test_re_emitting_with_same_run_id_upserts_not_duplicates(
    db_path, fake_model, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)

    with connection(db_path) as conn:
        _seed_pursue_entity(conn, "e1", "Acme Capital Partners")
        from app.db import add_entity_source
        add_entity_source(conn, "e1", "domain_check", {"domain": "acme.com", "mx_present": True})

    _patch_all_network(
        monkeypatch,
        jsonld_html='<script type="application/ld+json">{"@type":"Person","name":"Jane Doe","jobTitle":"CIO"}</script>',
        page_content="Reach the team at jane@acme.com.",
        snov_emails=[{"email": "jane@acme.com", "first_name": "Jane", "last_name": "Doe"}],
    )
    _route_model(fake_model)

    with connection(db_path) as conn:
        summary = await run_pipeline(conn, fake_model, target_survivors=50)
    run_id = summary["run_id"]

    with connection(db_path) as conn:
        before = len(get_field_provenance(conn, run_id=run_id))
        # re-emit with the SAME run_id — rebuild the rows and write again
        from app.provenance_log import build_run_log
        entity_outcomes = [(p["entity_id"], p["outcome"]) for p in summary["processed"]]
        doc = build_run_log(conn, run_id, entity_outcomes)
        rows = []
        for lead in doc["leads"]:
            for rec in lead["fields"]:
                rows.append({
                    "run_id": run_id, "entity_id": lead["entity_id"], "field": rec["field"],
                    "value": rec["value"], "status": rec["status"], "shipped": rec["shipped"],
                    "source_class": (rec["how"] or {}).get("source_class"),
                    "extraction_method": (rec["how"] or {}).get("extraction_method"),
                    "record": json.dumps(rec, ensure_ascii=False, default=str),
                })
        write_field_provenance(conn, rows)
        after = len(get_field_provenance(conn, run_id=run_id))
    assert before > 0
    assert after == before, f"upsert duplicated rows: before={before} after={after}"
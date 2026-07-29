"""Seam integration tests — enrichment_validation_dataset_plan.md §9. These exercise
the real Enrichment -> Validation -> Dataset pipeline end to end against a seeded temp
DB (not just one layer's unit tests in isolation). All external tools are
monkeypatched so this stays fully offline like the rest of the project."""
from __future__ import annotations

from langchain_core.messages import AIMessage

import app.enrichment as enrichment_module
import app.validation as validation_module
from app.dataset import MAX_PER_CLASS, build_candidate, gather_survivors, persist_selection, select_50, write_workbook
from app.db import (
    add_entity_source,
    connection,
    get_audit_rejected_values,
    get_claims,
    get_enrichment_runs,
    upsert_claims,
    upsert_entity,
    write_decision,
)
from app.enrichment import process_entity, run_pipeline


def _patch_network(monkeypatch, *, edgar_hits=1, jsonld_html=None, page_content="",
                    search_results=None, hunter_emails=None, gdelt_results=None, counters=None):
    counters = counters if counters is not None else {}

    def _count(name):
        counters[name] = counters.get(name, 0) + 1

    async def _fake_edgar(query, forms=None):
        _count("edgar")
        return {"results": [{"url": "http://sec.gov/x"}] * edgar_hits, "query": query}

    async def _fake_mx(domain):
        _count("mx")
        return True

    async def _fake_raw_html(url):
        _count("raw_html")
        return jsonld_html

    async def _fake_free_fetch(url, paid_fallback):
        _count("free_fetch")
        return {"url": url, "content": page_content, "extraction_method": "httpx_trafilatura"}

    async def _fake_search(query, topic="general", max_results=5):
        _count("search")
        return {"results": search_results or [], "query": query}

    async def _fake_hunter(domain):
        _count("hunter")
        return {"domain": domain, "pattern": None, "emails": hunter_emails or []}

    async def _fake_gdelt(query, lookback_days=365, max_records=75):
        _count("gdelt")
        return {"results": gdelt_results or [], "query": query}

    monkeypatch.setattr(enrichment_module, "edgar_full_text_search_raw", _fake_edgar)
    monkeypatch.setattr(enrichment_module, "check_domain_mx_exists", _fake_mx)
    monkeypatch.setattr(enrichment_module, "fetch_raw_html", _fake_raw_html)
    monkeypatch.setattr(enrichment_module, "fetch_page_free_first", _fake_free_fetch)
    monkeypatch.setattr(enrichment_module, "serper_search_raw", _fake_search)
    monkeypatch.setattr(enrichment_module, "hunter_domain_search_raw", _fake_hunter)
    monkeypatch.setattr(enrichment_module, "news_search_raw", _fake_gdelt)
    monkeypatch.setattr(validation_module, "fetch_page_free_first", _fake_free_fetch)
    return counters


def _route_ship_responses(fake_model, n_v1=10):
    fake_model.route(
        lambda msgs: any("extracting structured facts" in str(m.content) for m in msgs),
        AIMessage(content="[]"),
    )
    fake_model.route(
        lambda msgs: any("checking whether a web page" in str(m.content) for m in msgs),
        *[AIMessage(content='{"supported": true, "reason": "ok"}') for _ in range(n_v1)],
    )


def _seed_shippable_entity(conn, entity_id, name, *, verdict="pursue", thin_reason=None):
    upsert_entity(conn, entity_id, name)
    # value_usd/prior_value_usd -> wave -1 derives a why_now_trigger (fresh_liquidity),
    # which is the "dated signal" leg of V6 completeness — without it every entity here
    # would reject on completeness regardless of decision-maker/contact, since this repo
    # has no other dated-signal source wired into these tests.
    add_entity_source(conn, entity_id, "13f_filing",
                       {"quarter": "2026Q2", "value_usd": 100_000_000, "prior_value_usd": 40_000_000},
                       url="http://sec.gov/13f")
    add_entity_source(conn, entity_id, "domain_check", {"domain": "acmecap.com", "mx_present": True})
    claims = [
        {"question_id": "G1.Q3", "answer": "yes, operates as a family office", "status": "confirmed",
         "source_url": "http://x", "confidence": "high"},
        {"question_id": "G1.Q5", "answer": "no", "status": "confirmed", "confidence": "high"},
    ]
    upsert_claims(conn, entity_id, claims)
    write_decision(conn, entity_id, verdict=verdict, rationale="r", gate_results={}, claim_ledger=claims,
                    dead_ends=[], thin_reason=thin_reason)


# --- 1. Provenance survival ---

async def test_provenance_survives_wave_minus_1_through_validation_and_assembly(db_path, fake_model, monkeypatch):
    with connection(db_path) as conn:
        _seed_shippable_entity(conn, "e1", "Acme Capital Partners")

    _patch_network(
        monkeypatch,
        jsonld_html='<script type="application/ld+json">{"@type":"Person","name":"Jane Doe","jobTitle":"CIO"}</script>',
        page_content="Contact us at jane@acmecap.com for inquiries.",
    )
    _route_ship_responses(fake_model)

    with connection(db_path) as conn:
        result = await process_entity(conn, "e1", fake_model)
        assert result["outcome"] in ("ship", "ship_with_caveats")
        entity = {"entity_id": "e1", "canonical_name": "Acme Capital Partners"}
        claims = get_claims(conn, "e1")

    candidate = build_candidate(entity, claims, result["outcome"], "type_unconfirmed")
    from app.dataset import _provenance_rows
    prov = _provenance_rows([candidate])
    aum_row = next(r for r in prov if r["field_name"] == "aum_usd")
    assert aum_row["source_url"] == "http://sec.gov/13f"  # wave -1's original URL, unchanged by validation/assembly
    assert aum_row["extraction_method"] == "derived_13f"


# --- 2. Release rule ---

async def test_release_rule_blanks_undeliverable_email_and_logs_audit(db_path, fake_model, monkeypatch):
    """The reachable path for the release rule in this pipeline: wave 0's own V4 check
    catches any contradiction that already exists BEFORE enrichment runs (rejecting the
    whole record cheaply, per plan §4) — so the release rule's real job is catching a
    claim V1 determines its cited source doesn't actually support, on a high-value
    field, without necessarily rejecting the whole record. Simulated here by routing
    the V1 judge to say "not supported" specifically for the principal_email claim."""
    with connection(db_path) as conn:
        _seed_shippable_entity(conn, "e1", "Acme Capital Partners")
        add_entity_source(conn, "e1", "fec_employer", {"signals": {}, "people": [{"name": "Jane Doe", "title": "CIO"}]})
        extra = [
            {"field_name": "principal_email", "answer": "jane@acme.com", "status": "confirmed",
             "confidence": "medium", "source_class": "site_scrape", "source_url": "http://acmecap.com/team"},
        ]
        upsert_claims(conn, "e1", extra)

    _patch_network(monkeypatch)  # decision-maker (fec_employer people) + email already settled; nothing new needed
    fake_model.route(
        lambda msgs: any("checking whether a web page" in str(m.content) and "principal_email" in str(m.content) for m in msgs),
        AIMessage(content='{"supported": false, "reason": "page never mentions this email"}'),
    )
    fake_model.route(
        lambda msgs: any("checking whether a web page" in str(m.content) for m in msgs),
        *[AIMessage(content='{"supported": true, "reason": "ok"}') for _ in range(10)],
    )

    with connection(db_path) as conn:
        result = await process_entity(conn, "e1", fake_model)
        audit = get_audit_rejected_values(conn, "e1")
        claims_after = get_claims(conn, "e1")

    assert len(audit) == 1
    assert audit[0]["reason_code"] == "verification_failed"
    assert audit[0]["rejected_value"] == "jane@acme.com"
    email_claim = next(c for c in claims_after if c["field_name"] == "principal_email")
    assert email_claim["status"] == "removed_failed_validation"
    assert email_claim["answer"] is None

    entity = {"entity_id": "e1", "canonical_name": "Acme Capital Partners"}
    candidate = build_candidate(entity, claims_after, result["outcome"], "type_unconfirmed")
    from app.dataset import _records_rows
    columns, rows = _records_rows([candidate])
    row = rows[0]
    assert row["principal_email"] is None  # never silently empty...
    assert row["principal_email_status"] == "removed_failed_validation"  # ...the status column says why


# --- 3. Cross-class ---

async def test_cross_class_same_class_confirmation_stays_single_source(db_path, fake_model, monkeypatch):
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Acme Capital Partners")
        claims = [
            {"question_id": "G1.Q3", "answer": "yes", "status": "confirmed", "source_url": "http://x", "confidence": "high"},
            {"question_id": "G1.Q5", "answer": "no", "status": "confirmed", "confidence": "high"},
            {"field_name": "principal_name", "answer": "Jane Doe", "status": "confirmed", "confidence": "medium",
             "source_class": "serper_organic", "source_url": "http://a"},
            {"field_name": "principal_name", "answer": "Jane Doe", "status": "confirmed", "confidence": "medium",
             "source_class": "serper_organic", "source_url": "http://b"},
        ]
        upsert_claims(conn, "e1", claims)
        write_decision(conn, "e1", verdict="pursue", rationale="r", gate_results={}, claim_ledger=claims, dead_ends=[])

    _patch_network(monkeypatch)
    _route_ship_responses(fake_model)

    with connection(db_path) as conn:
        await process_entity(conn, "e1", fake_model)
        claims_after = get_claims(conn, "e1")

    principal_claims = [c for c in claims_after if c["field_name"] == "principal_name"]
    # both from serper_organic (the same, non-confirming class) -> neither promotes to "verified"
    assert all(c["status"] != "verified" for c in principal_claims)


# --- 4. Quota ---

def test_quota_caps_and_logs_exclusions_end_to_end(db_path):
    from app.dataset import ProductionCandidate
    candidates = [
        ProductionCandidate(entity_id=f"e{i}", canonical_name=f"e{i}", type_final="SFO", outcome="ship",
                             claims=[], actionability_score=float(60 - i), verified_cell_count=0,
                             urgency_tier_rank=0, discovery_class_primary="edgar_13f", discovery_class_count=1)
        for i in range(60)
    ]
    with connection(db_path) as conn:
        for c in candidates:
            upsert_entity(conn, c.entity_id, c.canonical_name)
        selected, per_class, excluded = select_50(candidates, n=50)
        persist_selection(conn, selected, excluded)
        paths = write_workbook(conn, selected, per_class, excluded, db_path + "_out")

    assert per_class["edgar_13f"] == MAX_PER_CLASS
    assert len(excluded) == 60 - MAX_PER_CLASS

    import openpyxl
    wb = openpyxl.load_workbook(paths["xlsx"])
    rejected_rows = list(wb["rejected_records"].iter_rows(min_row=2, values_only=True))
    quota_rows = [r for r in rejected_rows if r[1] == "quota"]
    assert len(quota_rows) == len(excluded)
    assert all(r[2] == "excluded to preserve source diversity" for r in quota_rows)


# --- 5. Reserve loop ---

async def test_reserve_loop_draws_only_fixable_thin_never_structural(db_path, fake_model, monkeypatch):
    with connection(db_path) as conn:
        for i in range(45):
            _seed_shippable_entity(conn, f"pursue{i}", f"Pursue Co {i}")
        for i in range(10):
            _seed_shippable_entity(conn, f"fixable{i}", f"Fixable Co {i}", verdict="pursue_low", thin_reason="fixable")
        for i in range(3):
            _seed_shippable_entity(conn, f"structural{i}", f"Structural Co {i}", verdict="pursue_low", thin_reason="structural")

    _patch_network(
        monkeypatch,
        jsonld_html='<script type="application/ld+json">{"@type":"Person","name":"Jane Doe","jobTitle":"CIO"}</script>',
        page_content="Contact us at jane@acmecap.com for inquiries.",
    )
    _route_ship_responses(fake_model, n_v1=500)

    with connection(db_path) as conn:
        summary = await run_pipeline(conn, fake_model, target_survivors=50)

    processed_ids = {p["entity_id"] for p in summary["processed"]}
    assert all(f"pursue{i}" in processed_ids for i in range(45))
    assert not any(f"structural{i}" in processed_ids for i in range(3))  # never drawn
    # gap = 50 - 45 = 5 -> draw_n = int(5*1.5) = 7 fixable-thin records, one draw
    assert summary["reserve_draws"] == 7
    fixable_processed = [eid for eid in processed_ids if eid.startswith("fixable")]
    assert len(fixable_processed) == 7


# --- 6. Idempotency ---

async def test_idempotent_reprocessing_makes_zero_new_api_calls(db_path, fake_model, monkeypatch):
    with connection(db_path) as conn:
        _seed_shippable_entity(conn, "e1", "Acme Capital Partners")

    counters = _patch_network(
        monkeypatch,
        jsonld_html='<script type="application/ld+json">{"@type":"Person","name":"Jane Doe","jobTitle":"CIO"}</script>',
        page_content="Contact us at jane@acmecap.com for inquiries.",
    )
    _route_ship_responses(fake_model)

    with connection(db_path) as conn:
        first = await process_entity(conn, "e1", fake_model)
    assert first.get("skipped_already_processed") is not True
    calls_after_first_run = dict(counters)
    assert sum(calls_after_first_run.values()) > 0  # sanity: the first run really did call tools

    with connection(db_path) as conn:
        second = await process_entity(conn, "e1", fake_model)
        runs = get_enrichment_runs(conn, "e1")

    assert second.get("skipped_already_processed") is True
    assert second["outcome"] == first["outcome"]
    assert counters == calls_after_first_run  # not one new tool call on the re-run
    assert len(runs) == 1  # no duplicate enrichment_runs row either

"""process_entity / run_pipeline — the DB-wired orchestration
(enrichment_validation_dataset_plan.md §4/§8 build-order steps 3-10). All external
tools (EDGAR, Serper, Hunter, GDELT, DNS, the LLM) are monkeypatched so this suite stays
fully offline like the rest of the project."""
from __future__ import annotations

from langchain_core.messages import AIMessage

import app.enrichment as enrichment_module
import app.validation as validation_module
from app.db import (
    add_entity_source,
    connection,
    get_claims,
    get_findings,
    upsert_claims,
    upsert_entity,
    write_decision,
)
from app.enrichment import process_entity, run_pipeline


def _patch_all_network(monkeypatch, *, edgar_hits=1, jsonld_html=None, page_content="",
                        search_results=None, snov_emails=None, gdelt_results=None):
    async def _fake_edgar(query, forms=None):
        results = [{"url": "http://sec.gov/x"}] * edgar_hits
        return {"results": results, "query": query}

    async def _fake_mx(domain):
        return True

    async def _fake_raw_html(url):
        return jsonld_html

    async def _fake_free_fetch(url, fallback=None):
        return {"url": url, "content": page_content, "extraction_method": "httpx_trafilatura"}

    async def _fake_search(query, topic="general", max_results=5):
        return {"results": search_results or [], "query": query}

    async def _fake_snov_by_name(first_name, last_name, domain):
        return {"results": snov_emails or []}

    async def _fake_snov_domain(domain):
        return {"results": snov_emails or []}

    async def _fake_gdelt(query, lookback_days=365, max_records=75):
        return {"results": gdelt_results or [], "query": query}

    monkeypatch.setattr(enrichment_module, "edgar_full_text_search_raw", _fake_edgar)
    monkeypatch.setattr(enrichment_module, "check_domain_mx_exists", _fake_mx)
    monkeypatch.setattr(enrichment_module, "fetch_raw_html", _fake_raw_html)
    monkeypatch.setattr(enrichment_module, "fetch_page_free_first", _fake_free_fetch)
    monkeypatch.setattr(enrichment_module, "serper_search_raw", _fake_search)
    monkeypatch.setattr(enrichment_module, "snov_emails_by_name_domain_raw", _fake_snov_by_name)
    monkeypatch.setattr(enrichment_module, "snov_domain_search_raw", _fake_snov_domain)
    monkeypatch.setattr(enrichment_module, "news_search_raw", _fake_gdelt)
    monkeypatch.setattr(validation_module, "fetch_page_free_first", _fake_free_fetch)


def _seed_pursue_entity(conn, entity_id, name, *, verdict="pursue", thin_reason=None, with_domain=True):
    upsert_entity(conn, entity_id, name)
    add_entity_source(conn, entity_id, "13f_filing", {"quarter": "2026Q1", "value_usd": 100_000_000}, url="http://sec.gov/13f")
    if with_domain:
        add_entity_source(conn, entity_id, "domain_check", {"domain": "acmecap.com", "mx_present": True})
    claims = [
        {"question_id": "G1.Q3", "answer": "yes, operates as a family office", "status": "confirmed",
         "source_url": "http://x", "confidence": "high"},
        {"question_id": "G1.Q5", "answer": "no", "status": "confirmed", "confidence": "high"},
    ]
    upsert_claims(conn, entity_id, claims)  # mirrors app.verdict.persist_verdict's real write path
    write_decision(conn, entity_id, verdict=verdict, rationale="r", gate_results={}, claim_ledger=claims,
                    dead_ends=[], thin_reason=thin_reason)


async def test_process_entity_records_contradiction_as_caveat_not_reject(db_path, fake_model, monkeypatch):
    """A genuine family office (G1.Q5="no", not RIA-in-costume) with a real AUM
    contradiction ships WITH CAVEATS, not reject (relaxations 2026-07-29 — see
    PROJECT_LOG.md): a V4 contradiction is recorded and the claim flips to "contradicted",
    but a contradiction is a caveat (ship_with_caveats), not grounds to reject; and
    completeness (no dated signal here) is advisory now, not fatal. Only the V5 firm-is-FO
    identity gate rejects, and this entity passes it. The V4_contradiction finding must
    still be persisted either way."""
    _patch_all_network(monkeypatch)
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Acme Capital Partners")
        add_entity_source(conn, "e1", "13f_filing", {"quarter": "2026Q1", "value_usd": 100_000_000})
        claims = [
            {"question_id": "G1.Q3", "answer": "yes, operates as a family office", "status": "confirmed",
             "source_url": "http://x", "confidence": "high"},
            {"question_id": "G1.Q5", "answer": "no", "status": "confirmed", "confidence": "high"},
            {"field_name": "aum_usd", "answer": 100_000_000, "status": "confirmed",
             "source_class": "13f_filing", "confidence": "high"},
            {"field_name": "aum_usd", "answer": 5_000_000, "status": "confirmed",
             "source_class": "site_scrape", "confidence": "high"},
        ]
        upsert_claims(conn, "e1", claims)
        write_decision(conn, "e1", verdict="pursue", rationale="r", gate_results={}, claim_ledger=claims, dead_ends=[])

    with connection(db_path) as conn:
        result = await process_entity(conn, "e1", fake_model)

    assert result["outcome"] == "ship_with_caveats"
    with connection(db_path) as conn:
        findings = get_findings(conn, "e1")
    assert any(f["check_id"] == "V4_contradiction" for f in findings)


async def test_process_entity_ships_full_happy_path(db_path, fake_model, monkeypatch):
    with connection(db_path) as conn:
        _seed_pursue_entity(conn, "e1", "Acme Capital Partners")

    _patch_all_network(
        monkeypatch,
        jsonld_html='<script type="application/ld+json">{"@type":"Person","name":"Jane Doe","jobTitle":"CIO"}</script>',
        page_content="Contact us at jane@acmecap.com for inquiries.",
        gdelt_results=[{"url": "http://news.example/x", "title": "Acme raises fund", "seendate": "20260101000000"}],
    )
    # route() (content-addressed) rather than the flat FIFO queue: wave 2's narrative
    # extraction call needs a JSON ARRAY response, V1's per-claim checks need JSON
    # OBJECT responses, and the exact count/interleaving of V1 calls (one per settled,
    # sourced claim) isn't something a test should have to predict — see
    # app.llm.FakeChatModel.route()'s own docstring for why this exists.
    fake_model.route(
        lambda msgs: any("extracting structured facts" in str(m.content) for m in msgs),
        AIMessage(content="[]"),
    )
    fake_model.route(
        lambda msgs: any("checking whether a web page" in str(m.content) for m in msgs),
        *[AIMessage(content='{"supported": true, "reason": "confirmed on page"}') for _ in range(10)],
    )

    with connection(db_path) as conn:
        result = await process_entity(conn, "e1", fake_model)

    assert result["outcome"] in ("ship", "ship_with_caveats")
    with connection(db_path) as conn:
        claims = get_claims(conn, "e1")
    fields = {c["field_name"] for c in claims}
    assert "principal_name" in fields
    assert "principal_email" in fields
    assert "aum_usd" in fields  # wave -1 claim persisted too


async def test_run_pipeline_promotes_pursue_low_when_wave_minus_1_fills_gate(db_path, fake_model, monkeypatch):
    with connection(db_path) as conn:
        _seed_pursue_entity(conn, "e1", "Beta Family Office", verdict="pursue_low", thin_reason="fixable")
        # people list on a pass-through source -> wave -1 derives a principal_name
        add_entity_source(conn, "e1", "fec_employer", {"signals": {}, "people": [{"name": "Jane Doe", "title": "CIO"}]})

    _patch_all_network(monkeypatch, page_content="Reach the team at ops@acmecap.com.")
    fake_model.route(
        lambda msgs: any("extracting structured facts" in str(m.content) for m in msgs),
        AIMessage(content="[]"),
    )
    fake_model.route(
        lambda msgs: any("checking whether a web page" in str(m.content) for m in msgs),
        *[AIMessage(content='{"supported": true, "reason": "ok"}') for _ in range(10)],
    )

    with connection(db_path) as conn:
        summary = await run_pipeline(conn, fake_model, target_survivors=1)

    assert "e1" in {p["entity_id"] for p in summary["processed"]}  # promoted into the pursue queue, actually processed


async def test_run_pipeline_never_processes_structural_thin_reserve(db_path, fake_model, monkeypatch):
    with connection(db_path) as conn:
        _seed_pursue_entity(conn, "e1", "Structural Co", verdict="pursue_low", thin_reason="structural", with_domain=False)

    _patch_all_network(monkeypatch)
    with connection(db_path) as conn:
        summary = await run_pipeline(conn, fake_model, target_survivors=50)

    assert summary["processed"] == []  # structural-thin, not promotable, never drawn
    assert summary["reserve_draws"] == 0


# --- PLAN.md T16: contact tiers must actually fire for an entity with no domain_check ---


async def test_process_entity_resolves_domain_and_lands_a_contact_channel(db_path, fake_model, monkeypatch):
    """The regression this suite was missing, and the reason the enrichment layer produced
    no `principal_email` in its entire live history (2026-08-12 pilot, PLAN.md T16).

    Every entity in the real pool has NO `domain_check` source, so `injected_facts["domain"]`
    was None, so wave 1's four domain-gated tiers never ran, so `have_channel` was
    permanently False and wave 2 was permanently unreachable. Seeds exactly that shape —
    no domain_check — and asserts a contact channel now lands.
    """
    with connection(db_path) as conn:
        _seed_pursue_entity(conn, "e1", "Acme Capital Partners", with_domain=False)

    _patch_all_network(
        monkeypatch,
        jsonld_html='<script type="application/ld+json">{"@type":"Person","name":"Jane Doe","jobTitle":"CIO"}</script>',
        page_content="Contact us at jane@acmecap.com for inquiries.",
        # The SERP the domain resolver reads: the firm's own site, behind an aggregator.
        search_results=[
            {"title": "Acme Capital Partners | LinkedIn", "url": "https://www.linkedin.com/company/acme"},
            {"title": "Acme Capital Partners", "url": "https://acmecap.com/"},
        ],
    )
    fake_model.route(
        lambda msgs: any("extracting structured facts" in str(m.content) for m in msgs),
        AIMessage(content="[]"),
    )
    fake_model.route(
        lambda msgs: any("checking whether a web page" in str(m.content) for m in msgs),
        *[AIMessage(content='{"supported": true, "reason": "confirmed on page"}') for _ in range(20)],
    )

    with connection(db_path) as conn:
        result = await process_entity(conn, "e1", fake_model)
        claims = get_claims(conn, "e1")

    fields = {c["field_name"] for c in claims}
    assert "principal_email" in fields, (
        "no contact channel resolved — wave 1's domain-gated tiers did not run"
    )
    email = next(c for c in claims if c["field_name"] == "principal_email")
    assert email["answer"] == "jane@acmecap.com"
    # Channel + decision-maker settled means wave 1 did not gate, so wave 2 ran.
    assert result["outcome"] in ("ship", "ship_with_caveats")


async def test_process_entity_prefers_an_ingested_domain_over_resolving_one(db_path, fake_model, monkeypatch):
    """An entity that DOES carry a domain_check source must use it and spend no Serper call
    resolving one — the resolver is a fallback for missing ingestion, not a replacement."""
    with connection(db_path) as conn:
        _seed_pursue_entity(conn, "e1", "Acme Capital Partners", with_domain=True)

    searched: list[tuple[str, str]] = []

    _patch_all_network(monkeypatch, page_content="Reach us at ops@acmecap.com anytime.")

    async def _tracking_search(query, topic="general", max_results=5):
        searched.append((query, topic))
        return {"results": [], "query": query}

    monkeypatch.setattr(enrichment_module, "serper_search_raw", _tracking_search)
    fake_model.route(
        lambda msgs: any("extracting structured facts" in str(m.content) for m in msgs),
        AIMessage(content="[]"),
    )
    fake_model.route(
        lambda msgs: any("checking whether a web page" in str(m.content) for m in msgs),
        *[AIMessage(content='{"supported": true, "reason": "confirmed on page"}') for _ in range(20)],
    )

    with connection(db_path) as conn:
        await process_entity(conn, "e1", fake_model)

    # The resolver's signature is the bare quoted name on the GENERAL endpoint. Wave 1's
    # x-ray queries carry site:linkedin.com/in, and its dated-signal fallback issues the
    # same bare name against topic="news" — so the topic is what discriminates.
    assert ('"Acme Capital Partners"', "general") not in searched


async def test_process_entity_wave0_reject_costs_no_domain_resolution(db_path, fake_model, monkeypatch):
    """A wave-0 fatal must short-circuit before the resolver spends a Serper call."""
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Definitely Not A Family Office Bank")
        add_entity_source(conn, "e1", "13f_filing", {"quarter": "2026Q1", "value_usd": 1_000_000})
        claims = [
            {"question_id": "G1.Q3", "answer": "No affirmative family-office evidence found",
             "status": "confirmed", "source_url": "http://x", "confidence": "high"},
        ]
        upsert_claims(conn, "e1", claims)
        write_decision(conn, "e1", verdict="pursue", rationale="r", gate_results={},
                       claim_ledger=claims, dead_ends=[])

    searched: list[str] = []
    _patch_all_network(monkeypatch)

    async def _tracking_search(query, topic="general", max_results=5):
        searched.append(query)
        return {"results": [], "query": query}

    monkeypatch.setattr(enrichment_module, "serper_search_raw", _tracking_search)

    with connection(db_path) as conn:
        result = await process_entity(conn, "e1", fake_model)

    assert result["outcome"] == "reject"
    assert searched == []


# --- force=True supersedes stale derived claims (2026-08-12) ---


async def test_force_rerun_supersedes_stale_derived_claim_instead_of_colliding(db_path, fake_model, monkeypatch):
    """wave_minus_1 is a pure function of entity_sources. On a force re-run after the
    source data itself was corrected (e.g. the 13F filing-quarter -> holdings-quarter
    backfill), the freshly-derived value genuinely disagrees with the OLD derived claim
    still in the ledger. Without superseding, V4 flags the pipeline's own prior output as
    contradicting itself -- found live: every one of 38 rejects after a real backfill+
    force re-run carried this artifact."""
    with connection(db_path) as conn:
        upsert_entity(conn, "e1", "Acme Capital Partners")
        # Stale source data -> wave -1 will derive aum_as_of="2026Q3" the first time.
        add_entity_source(conn, "e1", "13f_filing", {"quarter": "2026Q3", "value_usd": 100_000_000})
        claims = [
            {"question_id": "G1.Q3", "answer": "yes, operates as a family office", "status": "confirmed",
             "source_url": "http://x", "confidence": "high"},
            {"question_id": "G1.Q5", "answer": "no", "status": "confirmed", "confidence": "high"},
        ]
        upsert_claims(conn, "e1", claims)
        write_decision(conn, "e1", verdict="pursue", rationale="r", gate_results={}, claim_ledger=claims, dead_ends=[])

    _patch_all_network(monkeypatch)
    fake_model.route(
        lambda msgs: any("checking whether a web page" in str(m.content) for m in msgs),
        *[AIMessage(content='{"supported": true, "reason": "ok"}') for _ in range(10)],
    )
    with connection(db_path) as conn:
        await process_entity(conn, "e1", fake_model)
        old_run_claims = get_claims(conn, "e1")
    old_aum_as_of = [c for c in old_run_claims if c["field_name"] == "aum_as_of"]
    assert len(old_aum_as_of) == 1
    assert old_aum_as_of[0]["answer"] == "2026Q3"
    assert old_aum_as_of[0]["status"] != "superseded", "nothing to supersede on the first pass"

    # The upstream source is now corrected (mirrors the real 13F-quarter backfill).
    with connection(db_path) as conn:
        conn.execute(
            "UPDATE entity_sources SET payload = ? WHERE entity_id = ? AND source_class = '13f_filing'",
            ('{"quarter": "2026Q2", "filed_in_quarter": "2026Q3", "value_usd": 100000000}', "e1"),
        )
        conn.commit()

    with connection(db_path) as conn:
        result = await process_entity(conn, "e1", fake_model, force=True)
        claims_after = get_claims(conn, "e1")

    aum_as_of_claims = [c for c in claims_after if c["field_name"] == "aum_as_of"]
    assert len(aum_as_of_claims) == 2, "both the old and the corrected claim must survive"
    by_answer = {c["answer"]: c["status"] for c in aum_as_of_claims}
    assert by_answer["2026Q3"] == "superseded", "the stale derivation must be retracted, not deleted"
    assert by_answer["2026Q2"] != "superseded", "the fresh derivation must be live"
    assert by_answer["2026Q2"] != "contradicted", "must not collide with its own superseded predecessor"

    with connection(db_path) as conn:
        v4_findings = [f for f in get_findings(conn, "e1") if f["check_id"] == "V4_contradiction" and f["field"] == "aum_as_of"]
    assert v4_findings == [], "V4 must not fire on a claim vs its own superseded predecessor"


async def test_supersede_only_touches_derived_claims(db_path, fake_model, monkeypatch):
    """Research- and enrichment-produced claims (e.g. a Snov.io-sourced email, a
    LinkedIn-x-ray'd principal name) must NOT be superseded by a force re-run -- only wave
    -1's own deterministic output, which is the only thing guaranteed to regenerate
    identically-or-correctly from source data."""
    with connection(db_path) as conn:
        _seed_pursue_entity(conn, "e1", "Acme Capital Partners")
        extra = [
            {"field_name": "principal_name", "answer": "Jane Doe", "status": "confirmed",
             "confidence": "high", "source_class": "site_scrape", "produced_by": "enrichment", "wave": "1"},
        ]
        upsert_claims(conn, "e1", extra)

    _patch_all_network(monkeypatch)
    fake_model.route(
        lambda msgs: any("checking whether a web page" in str(m.content) for m in msgs),
        *[AIMessage(content='{"supported": true, "reason": "ok"}') for _ in range(10)],
    )
    with connection(db_path) as conn:
        await process_entity(conn, "e1", fake_model, force=True)
        claims_after = get_claims(conn, "e1")

    jane = [c for c in claims_after if c["field_name"] == "principal_name" and c["answer"] == "Jane Doe"]
    assert jane, "the enrichment-produced claim must still be present"
    assert jane[0]["status"] != "superseded"

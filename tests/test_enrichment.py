"""Wave -1 derivation (enrichment_validation_dataset_plan.md §4). Fixtures mirror the
real payload shapes app/ingest.py produces from the discovery feed (13f_filing/
5500_filing/conference_sighting/structured pass-through with a "people" list) — as close
to "3 real entities" as this offline suite gets without a live discovery file."""
from __future__ import annotations

from datetime import date, timedelta

from langchain_core.messages import AIMessage

import app.enrichment as enrichment_module
from app.enrichment import (
    extract_jsonld_person,
    resolve_domain,
    wave_0,
    wave_1,
    wave_2,
    wave_minus_1,
)
from app.state import Claim


def _source(source_class: str, payload: dict, url: str | None = None, retrieved_at: str = "2026-07-01T00:00:00Z"):
    return {"source_class": source_class, "payload": payload, "url": url, "retrieved_at": retrieved_at}


# --- Entity 1: "Acme Capital Partners" — 13F + a named principal via fec_employer ---
ACME_SOURCES = [
    _source("13f_filing", {"quarter": "2026Q1", "value_usd": 150_000_000, "position_count": 40}, url="http://sec.gov/acme"),
    _source("fec_employer", {"signals": {}, "people": [{"name": "Jane Doe", "title": "Managing Principal"}]}, url="http://fec.gov/acme"),
    _source("domain_check", {"domain": "acmecap.com", "mx_present": True}),
]

# --- Entity 2: "Beta Family Office" — 13F QoQ jump (fresh_liquidity), 5500 headcount,
# a future-dated conference sighting (access_window) ---
BETA_SOURCES = [
    _source("13f_filing", {"quarter": "2026Q2", "value_usd": 200_000_000, "prior_value_usd": 100_000_000, "prior_quarter": "2026Q1"}, url="http://sec.gov/beta"),
    _source("5500_filing", {"plan_year": 2025, "participant_count": 12}, url="http://dol.gov/beta"),
    _source("conference_sighting", {"conference": "Family Office Forum", "date": (date.today() + timedelta(days=30)).isoformat(), "role": "attendee"}),
]

# --- Entity 3: "Gamma Wealth" — thin: only a ppp_loans pass-through with no people ---
GAMMA_SOURCES = [
    _source("ppp_loans", {"signals": {"loan_amount": 50000}, "people": []}, url="http://sba.gov/gamma"),
]


def test_acme_derives_aum_and_principal_from_people_list():
    claims = wave_minus_1(ACME_SOURCES)
    by_field = {c.field_name: c for c in claims}

    assert by_field["aum_usd"].answer == 150_000_000
    assert by_field["aum_usd"].source_class == "13f_filing"
    assert by_field["aum_usd"].produced_by == "derived"
    assert by_field["aum_usd"].wave == "-1"
    assert by_field["aum_usd"].extraction_method == "derived_13f"
    assert by_field["aum_basis"].answer == "13f_floor"
    assert by_field["aum_as_of"].answer == "2026Q1"

    assert by_field["principal_name"].answer == "Jane Doe"
    assert by_field["principal_name"].source_class == "fec_employer"
    assert by_field["principal_title"].answer == "Managing Principal"

    assert "important_insight" not in by_field  # no prior_value_usd on this fixture

    assert "discovery_class_13f_filing" in by_field
    assert "discovery_class_fec_employer" in by_field
    assert "discovery_class_domain_check" in by_field


def test_beta_derives_fresh_liquidity_headcount_and_access_window():
    claims = wave_minus_1(BETA_SOURCES)
    by_field = {c.field_name: [c2 for c2 in claims if c2.field_name == c.field_name] for c in claims}

    triggers = {c.answer for c in by_field["important_insight"]}
    assert "fresh_liquidity" in triggers  # +100% QoQ
    assert "access_window" in triggers  # future conference sighting

    headcount = [c for c in claims if c.field_name == "headcount"][0]
    assert headcount.answer == 12
    assert headcount.source_class == "5500_filing"
    assert headcount.extraction_method == "derived_5500"

    assert "principal_name" not in {c.field_name for c in claims}  # no people list on this fixture


def test_gamma_thin_entity_emits_only_discovery_class_no_fabrication():
    claims = wave_minus_1(GAMMA_SOURCES)
    fields = {c.field_name for c in claims}
    assert fields == {"discovery_class_ppp_loans"}  # empty people list -> no principal claim invented


def test_concentration_pain_on_large_qoq_drop():
    sources = [_source("13f_filing", {"quarter": "2026Q2", "value_usd": 50_000_000, "prior_value_usd": 100_000_000})]
    claims = wave_minus_1(sources)
    trigger = [c for c in claims if c.field_name == "important_insight"][0]
    assert trigger.answer == "concentration_pain"


def test_qoq_delta_below_threshold_emits_no_trigger():
    sources = [_source("13f_filing", {"quarter": "2026Q2", "value_usd": 105_000_000, "prior_value_usd": 100_000_000})]
    claims = wave_minus_1(sources)
    assert "important_insight" not in {c.field_name for c in claims}


def test_public_list_overlap_requires_caller_supplied_list():
    claims_without = wave_minus_1(ACME_SOURCES, canonical_name="Acme Capital Partners")
    assert "public_list_overlap" not in {c.field_name for c in claims_without}

    claims_with = wave_minus_1(
        ACME_SOURCES, canonical_name="Acme Capital Partners", public_list={"ACME CAPITAL PARTNERS", "OTHER CO"}
    )
    overlap = [c for c in claims_with if c.field_name == "public_list_overlap"][0]
    assert overlap.answer == ["ACME CAPITAL PARTNERS"]


def test_past_dated_conference_sighting_does_not_trigger_access_window():
    sources = [_source("conference_sighting", {"conference": "Old Forum", "date": "2020-01-01", "role": "speaker"})]
    claims = wave_minus_1(sources)
    assert "important_insight" not in {c.field_name for c in claims}


# --- wave 0 gates ---

async def _patch_edgar(monkeypatch, result: dict):
    async def _fake(query, forms=None):
        return result
    monkeypatch.setattr(enrichment_module, "edgar_full_text_search_raw", _fake)


async def test_wave_0_records_contradiction_but_is_not_fatal_alone(monkeypatch):
    """Relaxed 2026-07-29 (see PROJECT_LOG.md): a V4 contradiction is still recorded and
    the claim still flips to "contradicted", but it no longer rejects at wave 0 by
    itself — only the identity gate (V5_firm_is_fo) does. Here G1.Q3 is confirmed, so
    fatal must be False even though a real contradiction was found."""
    await _patch_edgar(monkeypatch, {"results": [{"url": "http://sec.gov/x"}], "query": "q"})
    claims = [
        Claim(field_name="aum_usd", answer=150_000_000, source_class="13f_filing", status="confirmed", confidence="high"),
        Claim(field_name="aum_usd", answer=5_000_000, source_class="site_scrape", status="confirmed", confidence="high"),
        Claim(question_id="G1.Q3", answer="yes, operates as a family office", status="confirmed", confidence="high"),
        Claim(question_id="G1.Q5", answer="no", status="confirmed", confidence="high"),
    ]
    out_claims, findings, fatal = await wave_0(claims, "Acme Capital Partners")
    assert fatal is False
    assert any(f.check_id == "V4_contradiction" for f in findings)
    assert all(c.status == "contradicted" for c in out_claims if c.field_name == "aum_usd")


async def test_wave_0_still_fatal_when_g1q5_ria_in_costume_confirmed(monkeypatch):
    """The one gate that stays fatal at wave 0 regardless: confirmed RIA-in-costume."""
    await _patch_edgar(monkeypatch, {"results": [{"url": "http://sec.gov/x"}], "query": "q"})
    claims = [
        Claim(question_id="G1.Q3", answer="yes, operates as a family office", status="confirmed", confidence="high"),
        Claim(question_id="G1.Q5", answer="yes, plain RIA", status="confirmed", confidence="high"),
    ]
    _, findings, fatal = await wave_0(claims, "Acme Capital Partners")
    assert fatal is True
    assert any(f.check_id == "V5_firm_is_fo" and f.field == "G1.Q5" for f in findings)


async def test_wave_0_fatal_when_g1q3_unresolved(monkeypatch):
    await _patch_edgar(monkeypatch, {"results": [], "query": "q"})
    out_claims, findings, fatal = await wave_0([], "Acme Capital Partners")
    assert fatal is True
    assert any(f.check_id == "V5_firm_is_fo" for f in findings)


async def test_wave_0_not_fatal_when_clean(monkeypatch):
    await _patch_edgar(monkeypatch, {"results": [{"url": "http://sec.gov/x"}], "query": "q"})
    claims = [
        Claim(question_id="G1.Q3", answer="yes, operates as a family office", status="confirmed", confidence="high"),
        Claim(question_id="G1.Q5", answer="no", status="confirmed", confidence="high"),
    ]
    _, findings, fatal = await wave_0(claims, "Acme Capital Partners")
    assert fatal is False
    assert any(f.check_id == "V5_adv_recheck" and f.severity == "info" for f in findings)


async def test_wave_0_registration_recheck_degrades_to_warn_on_edgar_error(monkeypatch):
    await _patch_edgar(monkeypatch, {"results": [], "query": "q", "error": "timeout"})
    claims = [
        Claim(question_id="G1.Q3", answer="yes", status="confirmed", confidence="high"),
        Claim(question_id="G1.Q5", answer="no", status="confirmed", confidence="high"),
    ]
    _, findings, fatal = await wave_0(claims, "Acme Capital Partners")
    recheck = [f for f in findings if f.check_id == "V5_adv_recheck"][0]
    assert recheck.severity == "warn"
    assert fatal is False  # a failed re-check alone must never be fatal


# --- wave 1: JSON-LD parsing (pure) ---

def test_extract_jsonld_person_top_level():
    html = """
    <html><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Person","name":"Jane Doe","jobTitle":"Managing Principal"}
    </script></html>
    """
    name, title = extract_jsonld_person(html)
    assert name == "Jane Doe"
    assert title == "Managing Principal"


def test_extract_jsonld_person_nested_under_organization_employee():
    html = """
    <script type="application/ld+json">
    {"@type":"Organization","name":"Acme Capital","employee":[{"@type":"Person","name":"Jane Doe","jobTitle":"CIO"}]}
    </script>
    """
    name, title = extract_jsonld_person(html)
    assert name == "Jane Doe"
    assert title == "CIO"


def test_extract_jsonld_person_malformed_json_does_not_raise():
    html = '<script type="application/ld+json">{not valid json</script>'
    assert extract_jsonld_person(html) == (None, None)


def test_extract_jsonld_person_no_blocks():
    assert extract_jsonld_person("<html><body>plain page</body></html>") == (None, None)


# --- wave 1: tiered resolution ---

def _patch_wave1(monkeypatch, *, raw_html=None, free_fetch_content="", search_results=None,
                  snov_emails=None, gdelt_results=None, news_results=None):
    async def _fake_raw_html(url):
        return raw_html

    async def _fake_free_fetch(url, fallback=None):
        return {"url": url, "content": free_fetch_content, "extraction_method": "httpx_trafilatura"}

    async def _fake_search(query, topic="general", max_results=5):
        return {"results": search_results or [], "query": query}

    async def _fake_snov_by_name(first_name, last_name, domain):
        return {"results": snov_emails or []}

    async def _fake_snov_domain(domain):
        return {"results": snov_emails or []}

    async def _fake_gdelt(query, lookback_days=365, max_records=75):
        return {"results": gdelt_results or [], "query": query}

    monkeypatch.setattr(enrichment_module, "fetch_raw_html", _fake_raw_html)
    monkeypatch.setattr(enrichment_module, "fetch_page_free_first", _fake_free_fetch)
    monkeypatch.setattr(enrichment_module, "serper_search_raw", _fake_search)
    monkeypatch.setattr(enrichment_module, "snov_emails_by_name_domain_raw", _fake_snov_by_name)
    monkeypatch.setattr(enrichment_module, "snov_domain_search_raw", _fake_snov_domain)
    monkeypatch.setattr(enrichment_module, "news_search_raw", _fake_gdelt)


async def test_wave_1_resolves_via_jsonld_and_site_email_ungated(monkeypatch):
    _patch_wave1(
        monkeypatch,
        raw_html='<script type="application/ld+json">{"@type":"Person","name":"Jane Doe","jobTitle":"CIO"}</script>',
        free_fetch_content="Contact us at jane@acmecap.com for more information.",
        gdelt_results=[{"url": "http://news.example/x", "title": "Acme Capital Partners raises new fund", "seendate": "20260101000000"}],
    )
    new_claims, gated = await wave_1([], "Acme Capital Partners", domain="acmecap.com")
    fields = {c.field_name: c for c in new_claims}
    assert fields["principal_name"].answer == "Jane Doe"
    assert fields["principal_name"].extraction_method == "jsonld"
    assert fields["principal_email"].answer == "jane@acmecap.com"
    assert "recent_news" in fields
    assert "recent_investments" not in fields
    assert gated is False


async def test_wave_1_gated_when_nothing_resolves(monkeypatch):
    _patch_wave1(monkeypatch)  # everything empty
    new_claims, gated = await wave_1([], "Nobody Capital", domain="nobody.example")
    assert gated is True


async def test_wave_1_skips_already_settled_decision_maker_from_wave_minus_1(monkeypatch):
    calls = {"xray": 0}

    async def _fake_search(query, topic="general", max_results=5):
        calls["xray"] += 1
        return {"results": [], "query": query}

    _patch_wave1(monkeypatch, free_fetch_content="Reach us at ops@acmecap.com anytime.")
    monkeypatch.setattr(enrichment_module, "serper_search_raw", _fake_search)

    existing = [Claim(field_name="principal_name", answer="Jane Doe", status="confirmed",
                       confidence="high", produced_by="derived", wave="-1")]
    new_claims, gated = await wave_1(existing, "Acme Capital Partners", domain="acmecap.com")
    assert "principal_name" not in {c.field_name for c in new_claims}  # not re-derived
    assert gated is False  # existing decision-maker + newly found email channel


async def test_wave_1_email_falls_back_to_snov_when_site_scrape_empty(monkeypatch):
    """Tier 3 is Snov.io (it replaced Hunter.io). A name-targeted hit with a confidence
    score at/above 70 is 'medium'; the source_class must be "snov" so
    app/validation.py's cross-class rule scores it as a confirming vendor assertion."""
    _patch_wave1(
        monkeypatch,
        free_fetch_content="No email visible on this page at all.",
        snov_emails=[
            {"first_name": "Jane", "last_name": "Doe", "domain": "acmecap.com",
             "email": "jane@acmecap.com", "confidence": 85, "status": "valid"}
        ],
    )
    existing = [Claim(field_name="principal_name", answer="Jane Doe", status="confirmed", confidence="high")]
    new_claims, gated = await wave_1(existing, "Acme Capital Partners", domain="acmecap.com")
    email_claim = next(c for c in new_claims if c.field_name == "principal_email")
    assert email_claim.answer == "jane@acmecap.com"
    assert email_claim.source_class == "snov"
    assert email_claim.extraction_method == "snov_emails_by_name_domain"
    assert email_claim.confidence == "medium"


async def test_wave_1_snov_low_confidence_score_downgrades_claim(monkeypatch):
    """A weak Snov.io score must not be presented as a solid contact — anything under 70
    stays "low" so a pattern-guessed address can't masquerade as verified."""
    _patch_wave1(
        monkeypatch,
        free_fetch_content="No email visible on this page at all.",
        snov_emails=[
            {"first_name": "Jane", "last_name": "Doe", "domain": "acmecap.com",
             "email": "jane@acmecap.com", "confidence": 20, "status": "unknown"}
        ],
    )
    existing = [Claim(field_name="principal_name", answer="Jane Doe", status="confirmed", confidence="high")]
    new_claims, _ = await wave_1(existing, "Acme Capital Partners", domain="acmecap.com")
    email_claim = next(c for c in new_claims if c.field_name == "principal_email")
    assert email_claim.confidence == "low"


# --- wave 2: depth enrichment (survivors only) ---

def _patch_wave2(monkeypatch, *, free_fetch_content="", search_results=None, gdelt_results=None):
    async def _fake_free_fetch(url, fallback=None):
        return {"url": url, "content": free_fetch_content, "extraction_method": "httpx_trafilatura"}

    async def _fake_search(query, topic="general", max_results=5):
        return {"results": search_results or [], "query": query}

    async def _fake_gdelt(query, lookback_days=365, max_records=75):
        return {"results": gdelt_results or [], "query": query}

    monkeypatch.setattr(enrichment_module, "fetch_page_free_first", _fake_free_fetch)
    monkeypatch.setattr(enrichment_module, "serper_search_raw", _fake_search)
    monkeypatch.setattr(enrichment_module, "news_search_raw", _fake_gdelt)


async def test_wave_2_narrative_extraction_validates_source_index(monkeypatch, fake_model):
    _patch_wave2(
        monkeypatch,
        free_fetch_content="Acme Capital invests in growth-stage fintech companies across North America.",
    )
    fake_model.queue(AIMessage(content=(
        '[{"field_name": "investing_thesis", "answer": "growth-stage fintech", "source_index": 1}, '
        '{"field_name": "not_a_real_field", "answer": "x", "source_index": 1}, '
        '{"field_name": "background", "answer": "y", "source_index": 99}]'
    )))
    new_claims, cost = await wave_2([], "Acme Capital Partners", fake_model, domain="acmecap.com")
    fields = {c.field_name for c in new_claims}
    assert "investing_thesis" in fields
    assert "not_a_real_field" not in fields  # not an allowed field -> discarded
    assert "background" not in fields  # source_index 99 doesn't exist -> discarded (no fabricated citation)
    thesis = next(c for c in new_claims if c.field_name == "investing_thesis")
    assert thesis.source_url == "https://acmecap.com"
    assert thesis.wave == "2"


async def test_wave_2_skips_narrative_call_when_no_documents_gathered(monkeypatch, fake_model):
    _patch_wave2(monkeypatch, free_fetch_content="")  # nothing gathered -> no domain, no search results
    new_claims, cost = await wave_2([], "Nobody Capital", fake_model, domain=None)
    assert fake_model.calls == []  # no documents -> LLM never called
    assert cost == 0.0


async def test_wave_2_no_longer_authors_an_outreach_hook(monkeypatch, fake_model):
    # T34.3 removed the wave-2 outreach_hook authoring pass (a field that never ships
    # is pure spend). This test pins the removal: even with a settled important_insight
    # trigger claim in hand, wave_2 must (a) emit no outreach_hook claim and (b) make
    # no LLM call to author one. The queued AIMessage is deliberately left in place so
    # that a silent reintroduction of _author_outreach_hook would consume it, produce
    # the claim, and flip both assertions red — without it the test would pass vacuously.
    _patch_wave2(monkeypatch)
    fake_model.queue(AIMessage(content="Saw your recent $50M capital raise — congrats."))
    trigger = Claim(field_name="important_insight", answer="fresh_liquidity", status="confirmed",
                     confidence="medium", source_url="http://sec.gov/x", source_class="13f_filing")
    new_claims, cost = await wave_2([trigger], "Acme Capital Partners", fake_model, domain=None)
    assert "outreach_hook" not in {c.field_name for c in new_claims}
    assert fake_model.calls == []  # no LLM call made to author an outreach hook


async def test_wave_2_no_outreach_hook_when_no_trigger(monkeypatch, fake_model):
    _patch_wave2(monkeypatch)
    new_claims, cost = await wave_2([], "Acme Capital Partners", fake_model, domain=None)
    assert "outreach_hook" not in {c.field_name for c in new_claims}
    assert fake_model.calls == []  # never invoked without a trigger to phrase


async def test_wave_2_corporate_linkedin_via_xray(monkeypatch, fake_model):
    _patch_wave2(monkeypatch, search_results=[
        {"title": "Acme Capital Partners | LinkedIn", "url": "https://linkedin.com/company/acme-capital"}
    ])
    new_claims, _ = await wave_2([], "Acme Capital Partners", fake_model, domain=None)
    corp = next((c for c in new_claims if c.field_name == "corporate_linkedin"), None)
    assert corp is not None
    assert corp.answer == "https://linkedin.com/company/acme-capital"


# --- domain resolution (PLAN.md T16): the input every wave-1 contact tier is gated on ---
#
# All three "real SERP" cases below were captured from live Serper responses on 2026-08-12
# while diagnosing why principal_email had never once been produced — they are the actual
# first-five organic results for three entities from that pilot batch, not invented shapes.


def _patch_serper(monkeypatch, urls):
    async def _fake_search(query, topic="general", max_results=5):
        return {"results": [{"title": u, "url": u, "content": None} for u in urls], "query": query}

    monkeypatch.setattr(enrichment_module, "serper_search_raw", _fake_search)


async def test_resolve_domain_takes_firms_own_site_over_aggregators(monkeypatch):
    _patch_serper(monkeypatch, [
        "https://icgadvisors.com/",
        "https://icgadvisors.com/team/",
        "https://www.linkedin.com/company/icg-advisors-llc",
        "https://adviserinfo.sec.gov/firm/summary/148066",
    ])
    assert await resolve_domain("ICG Advisors, LLC") == "icgadvisors.com"


async def test_resolve_domain_matches_when_label_equals_the_distinctive_token(monkeypatch):
    _patch_serper(monkeypatch, [
        "https://impactfolio.co/",
        "https://www.facebook.com/IMPACTfolio/",
        "https://fintrx.com/firms/firm/impactfolio-llc-291716",
    ])
    assert await resolve_domain("IMPACTfolio, LLC") == "impactfolio.co"


async def test_resolve_domain_matches_acronym_prefix_of_a_longer_label(monkeypatch):
    """"IMZ Advisory Inc" -> imzfinancialservices.com. The domain is not the firm name, so
    a token-equality test would miss it; the label still *starts with* the one distinctive
    token, which is the anchoring rule."""
    _patch_serper(monkeypatch, [
        "https://imzfinancialservices.com/",
        "https://adviserinfo.sec.gov/firm/summary/314890",
        "https://www.instagram.com/imz_financial/",
    ])
    assert await resolve_domain("IMZ Advisory Inc") == "imzfinancialservices.com"


async def test_resolve_domain_returns_none_when_serp_is_all_aggregators(monkeypatch):
    _patch_serper(monkeypatch, [
        "https://www.linkedin.com/company/nobody-capital",
        "https://adviserinfo.sec.gov/firm/summary/999999",
        "https://m.yelp.com/biz/nobody-capital",
        "https://www.bloomberg.com/profile/company/NOBODY",
    ])
    assert await resolve_domain("Nobody Capital LLC") is None


async def test_resolve_domain_rejects_an_unrelated_firms_domain(monkeypatch):
    """The false-positive this guard exists for: a real, non-aggregator site that simply
    isn't this entity. Attributing it would poison every contact claim derived from it."""
    _patch_serper(monkeypatch, ["https://someothercompany.com/", "https://unrelated-fund.io/"])
    assert await resolve_domain("Bakken Family Office LLC") is None


async def test_resolve_domain_ignores_generic_name_tokens(monkeypatch):
    """"Family"/"Office"/"Capital" match hundreds of unrelated advisers, so a domain that
    only matches on those is not a match at all (adv.py::distinctive_name_tokens)."""
    _patch_serper(monkeypatch, ["https://familyoffice.com/", "https://capital.com/"])
    assert await resolve_domain("Bakken Family Office") is None


async def test_resolve_domain_survives_a_serper_error_result(monkeypatch):
    async def _fake_search(query, topic="general", max_results=5):
        return {"results": [], "query": query, "error": "SERPER_API_KEY not set"}

    monkeypatch.setattr(enrichment_module, "serper_search_raw", _fake_search)
    assert await resolve_domain("Acme Capital Partners") is None


async def test_resolve_domain_skips_non_http_and_malformed_urls(monkeypatch):
    _patch_serper(monkeypatch, ["", "not a url", "ftp://acmecap.com/pub", "https://acmecap.com/"])
    assert await resolve_domain("Acme Capital Partners") == "acmecap.com"

"""T21 — email attribution regression. The four fixtures below come from the live row
`CLASS VI FAMILY OFFICE, LLC` shipped on 2026-08-13, where `principal_email` held
`info@classvipartners.com` — a generic firm inbox attributed as a named principal's
address on a row that named no principal.

T21 stops that three ways: (T21.4) a generic role inbox is emitted as `firm_email`, never
`principal_email`; (T21.5) with no principal named, any discovered address is `firm_email`,
and with a principal named the Snov name-targeted tier runs before the site scrape so an
`info@` can't short-circuit a real personal address; (T21.3) an SMTP-valid Snov hit is
graded `medium`, not `low`; and the wave-1 gate widens to count `firm_email` as a channel
so a firm inbox plus a phone keeps wave 2 reachable (T19's cascade fix).

Same monkeypatch style as tests/test_enrichment.py's `_patch_wave1`.
"""
from __future__ import annotations

import app.enrichment as enrichment_module
from app.enrichment import _find_email_via_snov, wave_1
from app.state import Claim

DOMAIN = "classvipartners.com"
CONTACT_PAGE = "Reach us at info@classvipartners.com for inquiries."


def _patch(monkeypatch, *, free_fetch_content="", snov_emails=None,
           snov_domain_emails=None, search_results=None, gdelt_results=None,
           news_results=None, raw_html=None):
    """Stub every external call `wave_1`/`_find_email_via_snov` make, the same way
    tests/test_enrichment.py's `_patch_wave1` does."""
    async def _fake_raw_html(url):
        return raw_html

    async def _fake_free_fetch(url, fallback=None):
        return {"url": url, "content": free_fetch_content, "extraction_method": "httpx_trafilatura"}

    async def _fake_search(query, topic="general", max_results=5):
        return {"results": search_results or [], "query": query}

    async def _fake_snov_by_name(first_name, last_name, domain):
        return {"results": snov_emails or []}

    async def _fake_snov_domain(domain):
        return {"results": snov_domain_emails or []}

    async def _fake_gdelt(query, lookback_days=365, max_records=75):
        return {"results": gdelt_results or [], "query": query}

    monkeypatch.setattr(enrichment_module, "fetch_raw_html", _fake_raw_html)
    monkeypatch.setattr(enrichment_module, "fetch_page_free_first", _fake_free_fetch)
    monkeypatch.setattr(enrichment_module, "serper_search_raw", _fake_search)
    monkeypatch.setattr(enrichment_module, "snov_emails_by_name_domain_raw", _fake_snov_by_name)
    monkeypatch.setattr(enrichment_module, "snov_domain_search_raw", _fake_snov_domain)
    monkeypatch.setattr(enrichment_module, "news_search_raw", _fake_gdelt)


# --- T21.6 (a): a lone `info@` with no principal named is firm_email, never principal_email

async def test_role_inbox_with_no_principal_becomes_firm_email(monkeypatch):
    _patch(monkeypatch, free_fetch_content=CONTACT_PAGE)  # no snov hits, no news, no xray
    new_claims, _ = await wave_1([], "Class VI Family Office, LLC", domain=DOMAIN)

    emails = [c for c in new_claims if c.field_name in ("principal_email", "firm_email")]
    assert len(emails) == 1
    assert emails[0].field_name == "firm_email"
    assert emails[0].answer == "info@classvipartners.com"
    # NO principal_email claim anywhere in the returned list.
    assert not any(c.field_name == "principal_email" for c in new_claims)


# --- T21.6 (b): with a principal named, Snov runs first and the site tier never fires

async def test_named_principal_snov_runs_first_site_never_called(monkeypatch):
    _patch(
        monkeypatch,
        free_fetch_content=CONTACT_PAGE,  # the site tier WOULD find info@ if reached
        snov_emails=[
            {"email": "mblackburn@classvipartners.com", "smtp_status": "valid"},
        ],
    )
    # Wrap the site tier to count calls — it must never fire when Snov hits.
    calls = {"site": 0}
    real_site = enrichment_module._find_email_on_site

    async def _count_site(domain, principal_name=None):
        calls["site"] += 1
        return await real_site(domain, principal_name)

    monkeypatch.setattr(enrichment_module, "_find_email_on_site", _count_site)

    existing = [Claim(field_name="principal_name", answer="Matt Blackburn",
                      status="confirmed", confidence="high", produced_by="derived", wave="-1")]
    new_claims, _ = await wave_1(existing, "Class VI Family Office, LLC", domain=DOMAIN)

    email = next(c for c in new_claims if c.field_name == "principal_email")
    assert email.answer == "mblackburn@classvipartners.com"
    assert email.extraction_method == "snov_emails_by_name_domain"
    assert calls["site"] == 0, "site scrape must not run when the Snov name-targeted tier hits"


# --- T21.6 (c): smtp_status == "valid" grades as medium

async def test_snov_smtp_valid_grades_medium(monkeypatch):
    _patch(
        monkeypatch,
        snov_emails=[{"email": "x@y.com", "smtp_status": "valid"}],
    )
    claim = await _find_email_via_snov("y.com", "Some Person")
    assert claim is not None
    assert claim.field_name == "principal_email"
    assert claim.answer == "x@y.com"
    assert claim.confidence == "medium"


# --- D1: a role address returned by the name-targeted tier is still firm_email ---

async def test_snov_name_targeted_role_address_is_firm_email(monkeypatch):
    """The name-targeted tier matches surnames server-side, but a catch-all or role
    address can still come back for a name the provider cannot resolve. A generic firm
    inbox is never a person's address regardless of which tier produced it — the contact
    is kept, only relabelled to `firm_email`."""
    _patch(
        monkeypatch,
        snov_emails=[{"email": "info@acmecap.com", "smtp_status": "valid"}],
    )
    claim = await _find_email_via_snov("acmecap.com", "Jane Doe")
    assert claim is not None
    assert claim.field_name == "firm_email"  # NOT principal_email
    assert claim.answer == "info@acmecap.com"  # the contact is kept, only relabelled
    assert claim.extraction_method == "snov_emails_by_name_domain"
    assert claim.confidence == "medium"  # smtp_status valid still grades medium


# --- T21.6 (d): firm_email + principal_phone (no principal_email) keeps wave 2 reachable

async def test_firm_email_plus_phone_is_not_gated(monkeypatch):
    _patch(monkeypatch, free_fetch_content="")  # no new email discovered
    existing = [
        Claim(field_name="principal_name", answer="Matt Blackburn",
              status="confirmed", confidence="high", produced_by="derived", wave="-1"),
        Claim(field_name="firm_email", answer="info@classvipartners.com",
              status="confirmed", confidence="medium", produced_by="enrichment", wave="1"),
        Claim(field_name="principal_phone", answer="+17207330400",
              status="format_only", confidence="low", produced_by="enrichment", wave="1"),
    ]
    _, gated = await wave_1(existing, "Class VI Family Office, LLC", domain=DOMAIN)
    assert gated is False
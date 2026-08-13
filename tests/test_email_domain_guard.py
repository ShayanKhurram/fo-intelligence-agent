"""T22 — a Snov address at a different company must never become this firm's contact.

Regression corpus taken verbatim from the shipped row (observed live 2026-08-13): the lookup
`snov_emails_by_name_domain_raw("Matt", "Blackburn", "classvipartners.com")` returned
`Matt@capitalvalue.net` (a different company) and `_find_email_via_snov` accepted it
unchecked, stamping Class VI's own website as the source_url. These fixtures pin the
domain guard added in T22.2 against that exact value.

Follows `tests/test_enrichment.py`'s monkeypatch style: the two Snov raw functions are
patched on the `app.enrichment` module (where they are imported by name), so no HTTP call
is made and the offline suite stays hermetic.
"""
from __future__ import annotations

import app.enrichment as enrichment_module
from app.enrichment import _find_email_via_snov, wave_1
from app.state import Claim

# Verbatim from the shipped row.
DOMAIN = "classvipartners.com"
OFF_DOMAIN_PERSONAL = "Matt@capitalvalue.net"
ON_DOMAIN_PERSONAL = "matt@classvipartners.com"
ON_DOMAIN_SUBDOMAIN = "matt@mail.classvipartners.com"
OFF_DOMAIN_ROLE = "info@capitalvalue.net"
ON_DOMAIN_ROLE = "info@classvipartners.com"

PRINCIPAL = "Matt Blackburn"


def _patch_snov(
    monkeypatch,
    *,
    by_name_results: list[dict] | None = None,
    domain_results: list[dict] | None = None,
):
    """Patch the two Snov raw functions on the enrichment module.

    Each fake returns the exact list passed in (already in the post-`_flatten_envelopes`
    shape `_find_email_via_snov` consumes: dicts with an `email` key). Empty/omitted means
    "no rows for that tier" so the tier under test can be exercised in isolation.
    """

    async def _fake_by_name(first_name, last_name, domain):
        return {"results": list(by_name_results or [])}

    async def _fake_domain(domain):
        return {"results": list(domain_results or [])}

    monkeypatch.setattr(enrichment_module, "snov_emails_by_name_domain_raw", _fake_by_name)
    monkeypatch.setattr(enrichment_module, "snov_domain_search_raw", _fake_domain)


def _row(email: str, **extra) -> dict:
    row = {"email": email, "smtp_status": "valid", "is_valid_format": True}
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# Name-targeted tier (snov_emails_by_name_domain_raw)
# ---------------------------------------------------------------------------


async def test_name_targeted_off_domain_yields_no_claim(monkeypatch):
    """The shipped row: `Matt@capitalvalue.net` returned for `classvipartners.com` must
    yield no claim — it is a different Matt at a different company, not this principal's
    contact at any confidence."""
    _patch_snov(monkeypatch, by_name_results=[_row(OFF_DOMAIN_PERSONAL)])
    claim = await _find_email_via_snov(DOMAIN, PRINCIPAL)
    assert claim is None


async def test_name_targeted_exact_domain_yields_principal_email(monkeypatch):
    _patch_snov(monkeypatch, by_name_results=[_row(ON_DOMAIN_PERSONAL)])
    claim = await _find_email_via_snov(DOMAIN, PRINCIPAL)
    assert claim is not None
    assert claim.field_name == "principal_email"
    assert claim.answer == ON_DOMAIN_PERSONAL


async def test_name_targeted_subdomain_yields_principal_email(monkeypatch):
    """`matt@mail.classvipartners.com` is on a subdomain of the queried domain and must
    be accepted (the guard accepts `host == domain or host.endswith('.' + domain)`)."""
    _patch_snov(monkeypatch, by_name_results=[_row(ON_DOMAIN_SUBDOMAIN)])
    claim = await _find_email_via_snov(DOMAIN, PRINCIPAL)
    assert claim is not None
    assert claim.field_name == "principal_email"
    assert claim.answer == ON_DOMAIN_SUBDOMAIN


async def test_name_targeted_off_domain_row_does_not_shadow_on_domain_row(monkeypatch):
    """Live observation: the raw response contained `Matt@capitalvalue.net` first, then
    the correct `Matt@classvipartners.com`. The guard must skip the off-domain row and
    keep scanning, so the on-domain row is emitted — not silently dropped (the pathology
    T22.1 explicitly warns a naive guard could create)."""
    _patch_snov(
        monkeypatch,
        by_name_results=[_row(OFF_DOMAIN_PERSONAL), _row(ON_DOMAIN_PERSONAL)],
    )
    claim = await _find_email_via_snov(DOMAIN, PRINCIPAL)
    assert claim is not None
    assert claim.answer == ON_DOMAIN_PERSONAL
    assert claim.field_name == "principal_email"


async def test_name_targeted_off_domain_role_address_yields_no_claim(monkeypatch):
    """Proves the guard runs BEFORE `_email_field_name`: an off-domain role address
    (`info@capitalvalue.net`) is dropped entirely, NOT relabelled to `firm_email`.
    Without the guard a role address would become `firm_email`; the guard must reject it
    before that relabel is ever considered."""
    _patch_snov(monkeypatch, by_name_results=[_row(OFF_DOMAIN_ROLE)])
    claim = await _find_email_via_snov(DOMAIN, PRINCIPAL)
    assert claim is None


async def test_name_targeted_on_domain_role_address_yields_firm_email(monkeypatch):
    """T21 still works: an on-domain role address (`info@classvipartners.com`) is
    relabelled to `firm_email`, not `principal_email`."""
    _patch_snov(monkeypatch, by_name_results=[_row(ON_DOMAIN_ROLE)])
    claim = await _find_email_via_snov(DOMAIN, PRINCIPAL)
    assert claim is not None
    assert claim.field_name == "firm_email"
    assert claim.answer == ON_DOMAIN_ROLE


# ---------------------------------------------------------------------------
# Domain-wide tier (snov_domain_search_raw)
# ---------------------------------------------------------------------------


async def test_domain_wide_off_domain_yields_no_claim(monkeypatch):
    """No principal named -> the name-targeted tier is skipped and only the domain-wide
    tier runs. An off-domain row there must yield no claim."""
    _patch_snov(monkeypatch, domain_results=[_row(OFF_DOMAIN_PERSONAL)])
    claim = await _find_email_via_snov(DOMAIN, None)
    assert claim is None


async def test_domain_wide_exact_domain_yields_firm_email(monkeypatch):
    """No principal named -> any on-domain address is the firm's channel, `firm_email`."""
    _patch_snov(monkeypatch, domain_results=[_row(ON_DOMAIN_PERSONAL)])
    claim = await _find_email_via_snov(DOMAIN, None)
    assert claim is not None
    assert claim.field_name == "firm_email"
    assert claim.answer == ON_DOMAIN_PERSONAL


async def test_domain_wide_off_domain_role_address_yields_no_claim(monkeypatch):
    """Domain-wide tier: an off-domain role address is dropped before the role relabel,
    so it never becomes `firm_email`."""
    _patch_snov(monkeypatch, domain_results=[_row(OFF_DOMAIN_ROLE)])
    claim = await _find_email_via_snov(DOMAIN, None)
    assert claim is None


async def test_domain_wide_on_domain_role_address_yields_firm_email(monkeypatch):
    _patch_snov(monkeypatch, domain_results=[_row(ON_DOMAIN_ROLE)])
    claim = await _find_email_via_snov(DOMAIN, None)
    assert claim is not None
    assert claim.field_name == "firm_email"
    assert claim.answer == ON_DOMAIN_ROLE


async def test_domain_wide_surname_match_on_off_domain_skipped(monkeypatch):
    """With a principal named, the surname-match loop prefers a matching row at
    'medium' confidence. An off-domain row whose email happens to contain the surname
    must be skipped (guard runs before the surname check), and the fallback on-domain
    row is emitted instead."""
    _patch_snov(
        monkeypatch,
        by_name_results=[],  # name-targeted tier finds nothing
        domain_results=[
            _row("blackburn@capitalvalue.net"),  # surname matches, off-domain -> skip
            _row(ON_DOMAIN_PERSONAL),  # on-domain -> emit at 'low'
        ],
    )
    claim = await _find_email_via_snov(DOMAIN, PRINCIPAL)
    assert claim is not None
    assert claim.answer == ON_DOMAIN_PERSONAL
    assert claim.confidence == "low"


# ---------------------------------------------------------------------------
# wave_1 integration: the shipped-row scenario end-to-end
# ---------------------------------------------------------------------------


def _patch_wave1_snov_only(monkeypatch, *, snov_emails=None, free_fetch_content=""):
    """Like tests/test_enrichment.py's `_patch_wave1` but minimal — only what wave_1
    touches for an email-only off-domain Snov scenario (no principal derivation, no
    news). Everything else returns empty so the only possible email source is Snov."""

    async def _fake_free_fetch(url, fallback=None):
        return {"url": url, "content": free_fetch_content, "extraction_method": "httpx_trafilatura"}

    async def _fake_by_name(first_name, last_name, domain):
        return {"results": list(snov_emails or [])}

    async def _fake_domain(domain):
        return {"results": list(snov_emails or [])}

    async def _fake_search(query, topic="general", max_results=5):
        return {"results": [], "query": query}

    async def _fake_gdelt(query, lookback_days=365, max_records=75):
        return {"results": [], "query": query}

    monkeypatch.setattr(enrichment_module, "fetch_page_free_first", _fake_free_fetch)
    monkeypatch.setattr(enrichment_module, "serper_search_raw", _fake_search)
    monkeypatch.setattr(enrichment_module, "snov_emails_by_name_domain_raw", _fake_by_name)
    monkeypatch.setattr(enrichment_module, "snov_domain_search_raw", _fake_domain)
    monkeypatch.setattr(enrichment_module, "news_search_raw", _fake_gdelt)


async def test_wave_1_emits_no_principal_email_when_only_snov_hit_is_off_domain(monkeypatch):
    """The shipped-row scenario, end-to-end: a principal is named, the site scrape finds
    nothing, and the only Snov hit is the off-domain `Matt@capitalvalue.net`. wave_1 must
    not emit any `principal_email` claim (nor a `firm_email` claim) for that address."""
    _patch_wave1_snov_only(monkeypatch, snov_emails=[_row(OFF_DOMAIN_PERSONAL)])
    existing = [Claim(field_name="principal_name", answer=PRINCIPAL, status="confirmed", confidence="high")]
    new_claims, _gated = await wave_1(existing, "CLASS VI FAMILY OFFICE, LLC", domain=DOMAIN)
    fields = {c.field_name for c in new_claims}
    assert "principal_email" not in fields
    # The off-domain address must not sneak in as firm_email either.
    assert "firm_email" not in fields
    # And the off-domain value must appear on no claim of any field.
    assert all(c.answer != OFF_DOMAIN_PERSONAL for c in new_claims)


async def test_wave_1_emits_principal_email_when_snov_hit_is_on_domain(monkeypatch):
    """Companion: with the same setup but an on-domain Snov hit, wave_1 DOES emit
    `principal_email` — the guard does not over-fire and blank a valid result."""
    _patch_wave1_snov_only(monkeypatch, snov_emails=[_row(ON_DOMAIN_PERSONAL)])
    existing = [Claim(field_name="principal_name", answer=PRINCIPAL, status="confirmed", confidence="high")]
    new_claims, _gated = await wave_1(existing, "CLASS VI FAMILY OFFICE, LLC", domain=DOMAIN)
    email_claim = next((c for c in new_claims if c.field_name == "principal_email"), None)
    assert email_claim is not None
    assert email_claim.answer == ON_DOMAIN_PERSONAL
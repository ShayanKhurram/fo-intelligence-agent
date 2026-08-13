"""T32 — an outage must not look like an absence in the email path.

Regression corpus from the live 2026-08-13/14 failure: a 30-lead batch reported
`principal_email: 0/5`, read as "these firms publish no personal addresses". The Snov
account was out of credits. `app/tools/snov.py` reported the failure honestly
(`{"error": "Snov.io credits exhausted (HTTP 402)"}`), but `_find_email_via_snov` did

    for row in targeted.get("results") or []:   # an error dict has no "results" -> empty

so an exhausted account and a firm with genuinely no address were byte-identical: `None`,
no claim, no trace. T32 surfaces the failure as a `could_not_verify` `principal_email`
claim (`tool_unavailable` vs `no_evidence_found`, reusing `app/researcher.py`'s vocabulary)
and guards `wave_1`'s `settled.add` so an unresolved claim cannot flip `have_channel` and
ungate wave 2 on a dead lead.
"""
from __future__ import annotations

import app.enrichment as enrichment_module
from app.dataset import _records_rows, build_candidate
from app.enrichment import _find_email_via_snov, _settled_fields, wave_1
from app.state import Claim

DOMAIN = "fortitudefo.com"
PRINCIPAL = "Matt Walker"
# The real error string app/tools/snov.py emits on a 402 (line 151).
CREDITS_EXHAUSTED = "Snov.io credits exhausted (HTTP 402)"


def _patch_snov(monkeypatch, *, by_name=None, domain=None, free_fetch_content=""):
    """Stub both Snov raw tiers and the free site fetch. Each Snov fake returns exactly
    the dict passed in (an `{"error": ...}` outage shape or a `{"results": [...]}` shape),
    mirroring `tests/test_email_domain_guard.py`'s `_patch_snov`."""

    async def _fake_by_name(first_name, last_name, dom):
        return by_name

    async def _fake_domain(dom):
        return domain

    async def _fake_free_fetch(url, fallback=None):
        return {"url": url, "content": free_fetch_content, "extraction_method": "httpx_trafilatura"}

    async def _fake_search(query, topic="general", max_results=5):
        return {"results": [], "query": query}

    async def _fake_gdelt(query, lookback_days=365, max_records=75):
        return {"results": [], "query": query}

    monkeypatch.setattr(enrichment_module, "snov_emails_by_name_domain_raw", _fake_by_name)
    monkeypatch.setattr(enrichment_module, "snov_domain_search_raw", _fake_domain)
    monkeypatch.setattr(enrichment_module, "fetch_page_free_first", _fake_free_fetch)
    monkeypatch.setattr(enrichment_module, "serper_search_raw", _fake_search)
    monkeypatch.setattr(enrichment_module, "news_search_raw", _fake_gdelt)


# --- T32.3 (a): both tiers erroring with the real 402 text -> tool_unavailable ---------

async def test_both_snov_tiers_error_yields_tool_unavailable_claim(monkeypatch):
    _patch_snov(
        monkeypatch,
        by_name={"error": CREDITS_EXHAUSTED},
        domain={"error": CREDITS_EXHAUSTED},
    )
    claim = await _find_email_via_snov(DOMAIN, PRINCIPAL)
    assert claim is not None
    assert claim.status == "could_not_verify"
    assert claim.source_class == "tool_unavailable"
    # answer is None: the error must NOT print in the principal_email column.
    assert claim.answer is None
    assert claim.field_name == "principal_email"
    assert claim.extraction_method == "snov_error"
    assert claim.confidence == "low"
    assert claim.produced_by == "enrichment"
    assert claim.wave == "1"
    assert claim.source_url == f"https://{DOMAIN}"
    # the error string survives in verification_method so the ledger records why.
    assert claim.verification_method is not None
    assert CREDITS_EXHAUSTED in claim.verification_method


async def test_tool_unavailable_preserves_error_from_only_one_errored_tier(monkeypatch):
    """Any one tier erroring is enough to surface tool_unavailable — the brief: track
    whether ANY Snov call returned an `error` key."""
    _patch_snov(
        monkeypatch,
        by_name={"error": CREDITS_EXHAUSTED},
        domain={"results": []},  # the other tier answered, empty, no error
    )
    claim = await _find_email_via_snov(DOMAIN, PRINCIPAL)
    assert claim is not None
    assert claim.source_class == "tool_unavailable"
    assert claim.answer is None
    assert CREDITS_EXHAUSTED in (claim.verification_method or "")


# --- T32.3 (b): both tiers returning empty -> no_evidence_found -----------------------

async def test_both_snov_tiers_empty_yields_no_evidence_found(monkeypatch):
    _patch_snov(
        monkeypatch,
        by_name={"results": []},
        domain={"results": []},
    )
    claim = await _find_email_via_snov(DOMAIN, PRINCIPAL)
    assert claim is not None
    assert claim.status == "could_not_verify"
    assert claim.source_class == "no_evidence_found"
    assert claim.answer is None
    assert claim.extraction_method == "snov_no_match"
    assert claim.verification_method is None  # no error to record


# --- T32.3 (c): a usable address is unchanged ----------------------------------------

async def test_usable_address_is_unchanged(monkeypatch):
    _patch_snov(
        monkeypatch,
        by_name={"results": [
            {"email": f"matt@{DOMAIN}", "smtp_status": "valid"},
        ]},
        domain={"results": []},
    )
    claim = await _find_email_via_snov(DOMAIN, PRINCIPAL)
    assert claim is not None
    assert claim.status == "confirmed"
    assert claim.answer == f"matt@{DOMAIN}"
    assert claim.field_name == "principal_email"
    assert claim.source_class == "snov"
    assert claim.extraction_method == "snov_emails_by_name_domain"
    assert claim.confidence == "medium"  # smtp_status valid -> medium, unchanged


# --- T32.3 (d): wave 1 stays gated on the tool_unavailable path ----------------------

async def test_wave_1_gated_and_principal_email_not_settled_on_outage(monkeypatch):
    """A lead whose only email outcome is the `tool_unavailable` claim must NOT report a
    contact channel. `have_channel` (T21.5) must stay False, so wave 2 is not ungated and
    money is not spent on a dead lead. `"principal_email"` must be absent from settled."""
    _patch_snov(
        monkeypatch,
        by_name={"error": CREDITS_EXHAUSTED},
        domain={"error": CREDITS_EXHAUSTED},
        free_fetch_content="",  # the site tier also finds nothing -> outage is surfaced
    )
    existing = [Claim(field_name="principal_name", answer=PRINCIPAL, status="confirmed",
                      confidence="high", produced_by="derived", wave="-1")]
    new_claims, gated = await wave_1(existing, "Fortitude Family Office", domain=DOMAIN)

    # The tool_unavailable claim is the only email outcome.
    email_claims = [c for c in new_claims if c.field_name == "principal_email"]
    assert len(email_claims) == 1
    assert email_claims[0].source_class == "tool_unavailable"
    assert email_claims[0].answer is None

    # have_channel evaluates to False: the settled guard kept principal_email OUT of
    # settled (before T32.2 the unconditional settled.add would have put it in and
    # flipped have_channel to True). _settled_fields agrees with the internal set.
    all_claims = [*existing, *new_claims]
    settled = _settled_fields(all_claims)
    assert "principal_email" not in settled
    assert gated is True


async def test_wave_1_not_gated_when_site_finds_email_despite_snov_outage(monkeypatch):
    """Companion: an outage surfacing as `tool_unavailable` must not look like an
    absence, but it also must not MANUFACTURE an absence. If the site tier (an
    independent, free tool) finds an address despite Snov being down, that is a real
    channel and wave 2 stays reachable — the tool_unavailable claim is discarded in
    favour of the real address."""
    _patch_snov(
        monkeypatch,
        by_name={"error": CREDITS_EXHAUSTED},
        domain={"error": CREDITS_EXHAUSTED},
        free_fetch_content=f"Contact Matt at matt@{DOMAIN}.",
    )
    existing = [Claim(field_name="principal_name", answer=PRINCIPAL, status="confirmed",
                      confidence="high", produced_by="derived", wave="-1")]
    new_claims, gated = await wave_1(existing, "Fortitude Family Office", domain=DOMAIN)

    email_claims = [c for c in new_claims if c.field_name == "principal_email"]
    assert len(email_claims) == 1
    assert email_claims[0].answer == f"matt@{DOMAIN}"
    assert email_claims[0].status == "confirmed"
    assert gated is False


# --- T32.3 (e): the row shows BLANK principal_email with tool_unavailable source_class -

async def test_records_rows_blank_email_with_tool_unavailable_source_class(monkeypatch):
    """The whole point of T32, asserted where a reader would see it: the sheet's
    `principal_email` cell is BLANK (answer=None, never the error text), and the
    `principal_email_source_class` companion column reads `tool_unavailable` so a
    reader can tell "we could not look" from "we looked, nothing" from "we have it"."""
    _patch_snov(
        monkeypatch,
        by_name={"error": CREDITS_EXHAUSTED},
        domain={"error": CREDITS_EXHAUSTED},
        free_fetch_content="",
    )
    existing = [Claim(field_name="principal_name", answer=PRINCIPAL, status="confirmed",
                      confidence="high", produced_by="derived", wave="-1")]
    new_claims, _ = await wave_1(existing, "Fortitude Family Office", domain=DOMAIN)

    # Build the candidate the way the production sheet is assembled: every claim,
    # converted to the dict shape build_candidate / _records_rows consume.
    claim_dicts = [c.model_dump(mode="json") for c in [*existing, *new_claims]]
    candidate = build_candidate(
        {"entity_id": "e1", "canonical_name": "Fortitude Family Office"},
        claim_dicts, "ship", "SFO",
    )
    _, rows = _records_rows([candidate])
    row = rows[0]
    assert row["principal_email"] is None  # BLANK — the error never reaches the cell
    assert row["principal_email_status"] == "could_not_verify"
    assert row["principal_email_source_class"] == "tool_unavailable"
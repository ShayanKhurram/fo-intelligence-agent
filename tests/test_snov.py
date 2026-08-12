"""Tests for the Snov.io client (app/tools/snov.py) — respx-mocked, fully offline.

Pins the contract as **verified against the live API on 2026-08-12**, which differs from the
published docs in two ways that both caused real bugs:

  * records are wrapped in a per-input `{"<echo>": <input>, "result": <payload>}` envelope,
    not returned flat — and `result` is a dict for some endpoints, a list for others;
  * `task_hash` is not always top-level: `/v2/domain-search/domain-emails/start` returns it
    under `meta`, alongside an immediate empty `data` list.

Auth is OAuth2 client_credentials for a 1-hour Bearer token, then
POST /start -> task_hash -> GET /result polling.
"""
from __future__ import annotations

import dataclasses

import httpx
import pytest
import respx

import app.tools.snov as snov_mod

BASE = "https://api.snov.io"


def _set_creds(monkeypatch, client_id="cid", client_secret="csecret"):
    new_tools = dataclasses.replace(
        snov_mod.SETTINGS.tools,
        snov_client_id=client_id,
        snov_client_secret=client_secret,
        snov_base_url=BASE,
    )
    monkeypatch.setattr(
        snov_mod, "SETTINGS", dataclasses.replace(snov_mod.SETTINGS, tools=new_tools)
    )


class _NoWaitBucket:
    """Stand-in for SNOV_BUCKET. The real bucket is 1 req/s to stay under Snov.io's
    documented 60/min cap, which would otherwise make this offline module take ~40s."""

    async def acquire(self, cost: float = 1.0) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_token(monkeypatch):
    """The access token is a module global; clear it so one test's token can't satisfy
    another test that is asserting the auth call happens."""
    monkeypatch.setattr(snov_mod, "_token", None)
    monkeypatch.setattr(snov_mod, "_token_expires_at", 0.0)
    monkeypatch.setattr(snov_mod, "_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(snov_mod, "SNOV_BUCKET", _NoWaitBucket())
    yield
    snov_mod._token = None
    snov_mod._token_expires_at = 0.0


def _token_route():
    return respx.post(f"{BASE}/v1/oauth/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    )


# --- credential handling ---


async def test_missing_credentials_returns_error_without_http_call(monkeypatch):
    _set_creds(monkeypatch, client_id="", client_secret="")
    with respx.mock:
        route = _token_route()
        out = await snov_mod.snov_li_profiles_by_urls_raw(["https://linkedin.com/in/x"])
        assert out["results"] == []
        assert "SNOV_CLIENT_ID" in out["error"]
        assert not route.called, "unconfigured credentials must not make a network call"


@respx.mock
async def test_token_is_requested_with_client_credentials_grant(monkeypatch):
    _set_creds(monkeypatch)
    route = _token_route()
    respx.post(f"{BASE}/v2/company-domain-by-name/start").mock(
        return_value=httpx.Response(200, json={"task_hash": "h1", "status": "started"})
    )
    respx.get(f"{BASE}/v2/company-domain-by-name/result").mock(
        return_value=httpx.Response(200, json={"status": "completed", "data": [{"name": "A", "domain": "a.com"}]})
    )
    await snov_mod.snov_company_domain_by_name_raw(["Acme"])
    assert route.called
    sent = route.calls[0].request
    body = sent.content.decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=cid" in body


@respx.mock
async def test_token_is_reused_across_calls(monkeypatch):
    _set_creds(monkeypatch)
    token = _token_route()
    respx.post(f"{BASE}/v2/company-domain-by-name/start").mock(
        return_value=httpx.Response(200, json={"task_hash": "h", "status": "started"})
    )
    respx.get(f"{BASE}/v2/company-domain-by-name/result").mock(
        return_value=httpx.Response(200, json={"status": "completed", "data": []})
    )
    await snov_mod.snov_company_domain_by_name_raw(["A"])
    await snov_mod.snov_company_domain_by_name_raw(["B"])
    assert token.call_count == 1, "a cached, unexpired token must not be re-fetched"


@respx.mock
async def test_bearer_header_present_on_data_request(monkeypatch):
    _set_creds(monkeypatch)
    _token_route()
    start = respx.post(f"{BASE}/v2/li-profiles-by-urls/start").mock(
        return_value=httpx.Response(200, json={"task_hash": "h", "status": "started"})
    )
    respx.get(f"{BASE}/v2/li-profiles-by-urls/result").mock(
        return_value=httpx.Response(200, json={"status": "completed", "data": []})
    )
    await snov_mod.snov_li_profiles_by_urls_raw(["https://linkedin.com/in/x"])
    assert start.calls[0].request.headers["Authorization"] == "Bearer tok"


# --- async start/result pattern ---


@respx.mock
async def test_li_profiles_start_then_result_returns_data(monkeypatch):
    _set_creds(monkeypatch)
    _token_route()
    start = respx.post(f"{BASE}/v2/li-profiles-by-urls/start").mock(
        return_value=httpx.Response(200, json={"task_hash": "abc123", "status": "started"})
    )
    result = respx.get(f"{BASE}/v2/li-profiles-by-urls/result").mock(
        return_value=httpx.Response(
            200, json={"status": "completed", "data": [{"first_name": "Jane", "last_name": "Doe"}]}
        )
    )
    out = await snov_mod.snov_li_profiles_by_urls_raw(["https://linkedin.com/in/janedoe"])
    assert out["results"] == [{"first_name": "Jane", "last_name": "Doe"}]
    assert out["task_hash"] == "abc123"
    import json as _json
    assert _json.loads(start.calls[0].request.content)["urls"] == ["https://linkedin.com/in/janedoe"]
    assert result.calls[0].request.url.params["task_hash"] == "abc123"


@respx.mock
async def test_polls_until_task_completes(monkeypatch):
    _set_creds(monkeypatch)
    _token_route()
    respx.post(f"{BASE}/v2/li-profiles-by-urls/start").mock(
        return_value=httpx.Response(200, json={"task_hash": "h", "status": "started"})
    )
    respx.get(f"{BASE}/v2/li-profiles-by-urls/result").mock(
        side_effect=[
            httpx.Response(200, json={"status": "in_progress"}),
            httpx.Response(200, json={"status": "in_progress"}),
            httpx.Response(200, json={"status": "completed", "data": [{"first_name": "Jane"}]}),
        ]
    )
    out = await snov_mod.snov_li_profiles_by_urls_raw(["https://linkedin.com/in/x"])
    assert out["results"] == [{"first_name": "Jane"}]


@respx.mock
async def test_task_that_never_completes_is_an_error_not_an_empty(monkeypatch):
    """A poll timeout must surface as an error. Reporting it as "no data" is exactly the
    failure mode that hid the exhausted ScrapeOps balance for weeks."""
    _set_creds(monkeypatch)
    monkeypatch.setattr(snov_mod, "_POLL_MAX_WAIT_S", 0.0)
    _token_route()
    respx.post(f"{BASE}/v2/li-profiles-by-urls/start").mock(
        return_value=httpx.Response(200, json={"task_hash": "h", "status": "started"})
    )
    respx.get(f"{BASE}/v2/li-profiles-by-urls/result").mock(
        return_value=httpx.Response(200, json={"status": "in_progress"})
    )
    out = await snov_mod.snov_li_profiles_by_urls_raw(["https://linkedin.com/in/x"])
    assert out["results"] == []
    assert "did not complete" in out["error"]


@respx.mock
async def test_missing_task_hash_is_reported(monkeypatch):
    _set_creds(monkeypatch)
    _token_route()
    respx.post(f"{BASE}/v2/li-profiles-by-urls/start").mock(
        return_value=httpx.Response(200, json={"status": "error"})
    )
    out = await snov_mod.snov_li_profiles_by_urls_raw(["https://linkedin.com/in/x"])
    assert "no task_hash" in out["error"]


# --- failure modes ---


@respx.mock
async def test_credit_exhaustion_is_reported_distinctly(monkeypatch):
    _set_creds(monkeypatch)
    _token_route()
    respx.post(f"{BASE}/v2/li-profiles-by-urls/start").mock(
        return_value=httpx.Response(400, json={"message": "Not enough credits"})
    )
    out = await snov_mod.snov_li_profiles_by_urls_raw(["https://linkedin.com/in/x"])
    assert out["results"] == []
    assert "credits exhausted" in out["error"]


@respx.mock
async def test_401_triggers_one_token_refresh_then_succeeds(monkeypatch):
    _set_creds(monkeypatch)
    token = respx.post(f"{BASE}/v1/oauth/access_token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "stale", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600}),
        ]
    )
    start = respx.post(f"{BASE}/v2/company-domain-by-name/start").mock(
        side_effect=[
            httpx.Response(401, json={"message": "token expired"}),
            httpx.Response(200, json={"task_hash": "h", "status": "started"}),
        ]
    )
    respx.get(f"{BASE}/v2/company-domain-by-name/result").mock(
        return_value=httpx.Response(200, json={"status": "completed", "data": [{"name": "A", "domain": "a.com"}]})
    )
    out = await snov_mod.snov_company_domain_by_name_raw(["Acme"])
    assert out["results"] == [{"name": "A", "domain": "a.com"}]
    assert token.call_count == 2
    assert start.calls[1].request.headers["Authorization"] == "Bearer fresh"


@respx.mock
async def test_auth_failure_degrades_without_raising(monkeypatch):
    _set_creds(monkeypatch)
    respx.post(f"{BASE}/v1/oauth/access_token").mock(return_value=httpx.Response(403, text="nope"))
    out = await snov_mod.snov_emails_by_name_domain_raw("Jane", "Doe", "acmecap.com")
    assert out["results"] == []
    assert out["error"]


@respx.mock
async def test_transport_error_degrades_without_raising(monkeypatch):
    _set_creds(monkeypatch)
    _token_route()
    respx.post(f"{BASE}/v2/email-verification/start").mock(
        side_effect=httpx.ConnectError("boom")
    )
    out = await snov_mod.snov_verify_emails_raw(["a@b.com"])
    assert out["results"] == []
    assert "boom" in out["error"] or "ConnectError" in out["error"]


async def test_empty_input_short_circuits(monkeypatch):
    _set_creds(monkeypatch)
    with respx.mock:
        route = _token_route()
        assert (await snov_mod.snov_li_profiles_by_urls_raw([]))["error"] == "no urls given"
        assert (await snov_mod.snov_company_domain_by_name_raw([]))["error"] == "no names given"
        assert (await snov_mod.snov_emails_by_name_domain_raw("a", "b", ""))["error"] == "no domain given"
        assert not route.called


@respx.mock
async def test_emails_by_name_domain_sends_rows_shape(monkeypatch):
    _set_creds(monkeypatch)
    _token_route()
    start = respx.post(f"{BASE}/v2/emails-by-domain-by-name/start").mock(
        return_value=httpx.Response(200, json={"task_hash": "h", "status": "started"})
    )
    respx.get(f"{BASE}/v2/emails-by-domain-by-name/result").mock(
        return_value=httpx.Response(
            200,
            json={"status": "completed",
                  "data": [{"first_name": "Jane", "last_name": "Doe", "domain": "acmecap.com",
                            "email": "jane@acmecap.com", "confidence": 90, "status": "valid"}]},
        )
    )
    out = await snov_mod.snov_emails_by_name_domain_raw("Jane", "Doe", "acmecap.com")
    import json as _json
    assert _json.loads(start.calls[0].request.content)["rows"] == [
        {"first_name": "Jane", "last_name": "Doe", "domain": "acmecap.com"}
    ]
    assert out["results"][0]["email"] == "jane@acmecap.com"


# --- envelope flattening + task_hash location (live-verified 2026-08-12) ---


def test_flatten_unwraps_dict_result_envelope():
    """The published docs describe a flat data[{first_name,...}] shape. The API actually
    returns one envelope per input with the record nested under `result`."""
    from app.tools.snov import _flatten_envelopes

    rows = [{"url": "https://linkedin.com/in/jane", "result": {"name": "Jane", "positions": []}}]
    out = _flatten_envelopes(rows)
    assert out == [{"name": "Jane", "positions": [], "_query": "https://linkedin.com/in/jane"}]


def test_flatten_unwraps_list_result_envelope():
    from app.tools.snov import _flatten_envelopes

    rows = [{"people": "Jane Doe", "result": [{"email": "a@b.com"}, {"email": "c@d.com"}]}]
    out = _flatten_envelopes(rows)
    assert [r["email"] for r in out] == ["a@b.com", "c@d.com"]
    assert all(r["_query"] == "Jane Doe" for r in out)


def test_flatten_drops_inputs_with_no_data():
    """A no-match must contribute NOTHING, so the overall result is an empty list — which
    app/tools/cache.py then correctly refuses to cache."""
    from app.tools.snov import _flatten_envelopes

    assert _flatten_envelopes([{"name": "Nobody Ltd", "result": []}]) == []
    assert _flatten_envelopes([{"name": "Nobody Ltd", "result": {}}]) == []


def test_flatten_passes_through_already_flat_rows():
    from app.tools.snov import _flatten_envelopes

    assert _flatten_envelopes([{"email": "a@b.com"}]) == [{"email": "a@b.com"}]


def test_task_hash_found_at_top_level():
    from app.tools.snov import _extract_task_hash

    assert _extract_task_hash({"task_hash": "abc", "status": "started"}) == "abc"


def test_task_hash_found_under_meta():
    """Regression: /v2/domain-search/domain-emails/start returns the hash under `meta`
    alongside an immediate empty `data` LIST. Checking only the top level (and `data` as a
    dict) made every domain-email lookup fail with 'returned no task_hash'."""
    from app.tools.snov import _extract_task_hash

    started = {"data": [], "meta": {"domain": "x.com", "task_hash": "deadbeef"}, "links": {}}
    assert _extract_task_hash(started) == "deadbeef"


def test_task_hash_found_under_data_dict():
    from app.tools.snov import _extract_task_hash

    assert _extract_task_hash({"data": {"task_hash": "nested"}}) == "nested"


def test_task_hash_absent_returns_none():
    from app.tools.snov import _extract_task_hash

    assert _extract_task_hash({"status": "error"}) is None
    assert _extract_task_hash({"data": []}) is None


@respx.mock
async def test_domain_emails_uses_meta_task_hash_end_to_end(monkeypatch):
    _set_creds(monkeypatch)
    _token_route()
    respx.post(f"{BASE}/v2/domain-search/domain-emails/start").mock(
        return_value=httpx.Response(
            200, json={"data": [], "meta": {"domain": "acmecap.com", "task_hash": "h9"}}
        )
    )
    result = respx.get(f"{BASE}/v2/domain-search/domain-emails/result").mock(
        return_value=httpx.Response(
            200,
            json={"status": "completed",
                  "data": [{"domain": "acmecap.com", "result": [{"email": "jane@acmecap.com"}]}]},
        )
    )
    out = await snov_mod.snov_domain_search_raw("acmecap.com")
    assert out["results"] == [{"email": "jane@acmecap.com", "_query": "acmecap.com"}]
    assert result.calls[0].request.url.params["task_hash"] == "h9"

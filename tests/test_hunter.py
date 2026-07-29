"""Tests for the Hunter.io domain-search client (app/tools/hunter.py) — respx-mocked,
all offline. Pins the request/response contract and the missing-key/error degradation
(never raises), plus multi-key rotation on a 429 (app/tools/keyrotation.py)."""
from __future__ import annotations

import httpx
import respx

import app.tools.hunter as hunter_mod
from app.config import SETTINGS
from app.tools.hunter import hunter_domain_search_raw
from app.tools.keyrotation import KeyRotator

HUNTER_URL = f"{SETTINGS.tools.hunter_base_url}/v2/domain-search"


def _set_keys(*values: str):
    hunter_mod._HUNTER_ROTATOR = KeyRotator(values)


def _restore_key():
    hunter_mod._HUNTER_ROTATOR = KeyRotator(())


@respx.mock
async def test_domain_search_maps_emails():
    _set_keys("testkey")
    try:
        respx.get(HUNTER_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "domain": "acmecap.com",
                        "pattern": "{first}.{last}",
                        "emails": [
                            {"value": "jane.doe@acmecap.com", "type": "personal",
                             "first_name": "Jane", "last_name": "Doe",
                             "position": "CIO", "confidence": 90}
                        ],
                    }
                },
            )
        )
        result = await hunter_domain_search_raw("acmecap.com")
        assert result["domain"] == "acmecap.com"
        assert result["pattern"] == "{first}.{last}"
        assert result["emails"][0]["value"] == "jane.doe@acmecap.com"
        assert result["emails"][0]["last_name"] == "Doe"
    finally:
        _restore_key()


@respx.mock
async def test_missing_key_returns_error_without_http_call():
    _restore_key()
    route = respx.get(HUNTER_URL).mock(return_value=httpx.Response(200, json={"data": {}}))
    result = await hunter_domain_search_raw("acmecap.com")
    assert result == {"domain": "acmecap.com", "pattern": None, "emails": [], "error": "HUNTER_API_KEY not set"}
    assert not route.called


@respx.mock
async def test_http_error_degrades_with_error():
    _set_keys("testkey")
    try:
        respx.get(HUNTER_URL).mock(return_value=httpx.Response(500))
        result = await hunter_domain_search_raw("acmecap.com")
        assert result["emails"] == []
        assert result["error"] is not None
    finally:
        _restore_key()


@respx.mock
async def test_empty_response_degrades_cleanly():
    _set_keys("testkey")
    try:
        respx.get(HUNTER_URL).mock(return_value=httpx.Response(200, json={}))
        result = await hunter_domain_search_raw("acmecap.com")
        assert result["emails"] == []
        assert result["pattern"] is None
        assert "error" not in result
    finally:
        _restore_key()


@respx.mock
async def test_api_key_param_present_on_request():
    _set_keys("testkey")
    try:
        route = respx.get(HUNTER_URL).mock(return_value=httpx.Response(200, json={"data": {}}))
        await hunter_domain_search_raw("acmecap.com")
        assert route.called
        sent = respx.calls[0].request
        assert "api_key=testkey" in str(sent.url)
        assert "domain=acmecap.com" in str(sent.url)
    finally:
        _restore_key()


# --- multi-key rotation ---

@respx.mock
async def test_rotates_to_next_key_on_429():
    _set_keys("key1", "key2")
    try:
        route = respx.get(HUNTER_URL).mock(
            side_effect=[httpx.Response(429), httpx.Response(200, json={"data": {"emails": []}})]
        )
        result = await hunter_domain_search_raw("acmecap.com")
        assert route.call_count == 2
        assert "api_key=key1" in str(respx.calls[0].request.url)
        assert "api_key=key2" in str(respx.calls[1].request.url)
        assert "error" not in result
    finally:
        _restore_key()


@respx.mock
async def test_all_keys_exhausted_degrades_with_error():
    _set_keys("key1", "key2")
    try:
        respx.get(HUNTER_URL).mock(return_value=httpx.Response(429))
        result = await hunter_domain_search_raw("acmecap.com")
        assert result["emails"] == []
        assert "exhausted" in result["error"]
        assert len(respx.calls) == 2
    finally:
        _restore_key()

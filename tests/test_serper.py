"""Tests for the Serper-backed `web_search` — respx-mocked, all offline.

Page fetching is no longer Serper-backed (it moved to Crawl4AI, see tests/test_crawl.py),
so this module covers search only. Pins the Serper request/response contract
documented in the build brief: POST /search returns {"organic": [...]}, POST /news
returns {"news": [...]}, results map to our {title,url,content,score} shape, and any
failure (missing key, HTTP error) degrades to {"results": [], "query":..., "error":...}
without raising."""
from __future__ import annotations

import json

import httpx
import respx

import app.tools.serper as serper_mod
from app.config import SETTINGS
from app.tools.keyrotation import KeyRotator
from app.tools.serper import serper_search_raw, web_search

SERPER_BASE = SETTINGS.tools.serper_base_url


def _set_key(value: str):
    """The actual key lookup goes through the module-global `_SERPER_ROTATOR`
    (app/tools/keyrotation.py), built once at import time from
    SETTINGS.tools.serper_api_keys — flip that directly rather than the (no longer
    read at call time) SETTINGS.tools.serper_api_key."""
    serper_mod._SERPER_ROTATOR = KeyRotator((value,) if value else ())


def _restore_key():
    serper_mod._SERPER_ROTATOR = KeyRotator(())


@respx.mock
async def test_general_search_maps_organic_results():
    _set_key("testkey")
    try:
        respx.post(f"{SERPER_BASE}/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "searchParameters": {"q": "acme", "type": "search"},
                    "organic": [
                        {
                            "title": "Acme Corp",
                            "link": "https://acme.example",
                            "snippet": "Acme is a widget maker",
                            "position": 1,
                        },
                        {
                            "title": "Acme News",
                            "link": "https://news.example",
                            "snippet": "Latest on Acme",
                            "position": 2,
                        },
                    ],
                },
            )
        )
        result = await serper_search_raw("acme")
        assert result["query"] == "acme"
        assert "error" not in result
        assert len(result["results"]) == 2
        r0 = result["results"][0]
        assert r0["title"] == "Acme Corp"
        assert r0["url"] == "https://acme.example"
        assert r0["content"] == "Acme is a widget maker"
        assert r0["score"] is None
        assert "date" not in r0  # general results here have no date
    finally:
        _restore_key()


@respx.mock
async def test_news_search_hits_news_endpoint_and_maps_news_array():
    _set_key("testkey")
    try:
        route = respx.post(f"{SERPER_BASE}/news").mock(
            return_value=httpx.Response(
                200,
                json={
                    "news": [
                        {
                            "title": "Acme raises Series B",
                            "link": "https://news.example/acme",
                            "snippet": "Acme announced ...",
                            "date": "3 days ago",
                            "source": "TechCrunch",
                            "position": 1,
                        }
                    ]
                },
            )
        )
        search_route = respx.post(f"{SERPER_BASE}/search").mock(
            return_value=httpx.Response(200, json={})
        )
        result = await serper_search_raw("acme", topic="news")
        assert route.called
        assert not search_route.called
        assert "error" not in result
        r0 = result["results"][0]
        assert r0["title"] == "Acme raises Series B"
        assert r0["url"] == "https://news.example/acme"
        assert r0["content"] == "Acme announced ..."
        assert r0["date"] == "3 days ago"
    finally:
        _restore_key()


@respx.mock
async def test_api_key_header_present_on_request():
    _set_key("testkey")
    try:
        route = respx.post(f"{SERPER_BASE}/search").mock(
            return_value=httpx.Response(200, json={"organic": []})
        )
        await serper_search_raw("acme")
        assert route.called
        sent = respx.calls[0].request
        assert sent.headers.get("X-API-KEY") == "testkey"
        assert sent.headers.get("Content-Type") == "application/json"
        payload = json.loads(sent.content)
        assert payload == {"q": "acme", "num": 5}
    finally:
        _restore_key()


@respx.mock
async def test_empty_response_no_error_no_raise():
    _set_key("testkey")
    try:
        respx.post(f"{SERPER_BASE}/search").mock(
            return_value=httpx.Response(200, json={"organic": []})
        )
        result = await serper_search_raw("x")
        assert result["results"] == []
        assert result["query"] == "x"
        assert "error" not in result

        # totally empty body (no organic key at all) also degrades cleanly
        respx.post(f"{SERPER_BASE}/search").mock(
            return_value=httpx.Response(200, json={})
        )
        result2 = await serper_search_raw("y")
        assert result2["results"] == []
        assert "error" not in result2
    finally:
        _restore_key()


@respx.mock
async def test_http_error_degrades_with_error():
    _set_key("testkey")
    try:
        respx.post(f"{SERPER_BASE}/search").mock(return_value=httpx.Response(500))
        result = await serper_search_raw("acme")
        assert result["results"] == []
        assert result["query"] == "acme"
        assert result["error"] is not None
    finally:
        _restore_key()


@respx.mock
async def test_connection_error_degrades():
    _set_key("testkey")
    try:
        respx.post(f"{SERPER_BASE}/search").mock(side_effect=httpx.ConnectError("refused"))
        result = await serper_search_raw("acme")
        assert result["results"] == []
        assert result["error"] is not None
    finally:
        _restore_key()


@respx.mock
async def test_missing_key_returns_error_without_http_call():
    _set_key("")
    try:
        route = respx.post(f"{SERPER_BASE}/search").mock(
            return_value=httpx.Response(200, json={"organic": []})
        )
        result = await serper_search_raw("acme")
        assert result["results"] == []
        assert result["query"] == "acme"
        assert result["error"] == "SERPER_API_KEY not set"
        assert not route.called
    finally:
        _restore_key()


@respx.mock
async def test_web_search_dispatches_to_serper_when_key_set():
    _set_key("testkey")
    try:
        route = respx.post(f"{SERPER_BASE}/search").mock(
            return_value=httpx.Response(
                200, json={"organic": [{"title": "t", "link": "u", "snippet": "s"}]}
            )
        )
        result = await web_search.ainvoke({"query": "acme"})
        assert route.called
        assert result["results"][0]["url"] == "u"
        assert "error" not in result
    finally:
        _restore_key()


@respx.mock
async def test_web_search_no_key_degrades_to_error():
    _set_key("")
    try:
        serper_route = respx.post(f"{SERPER_BASE}/search").mock(
            return_value=httpx.Response(200, json={"organic": []})
        )
        result = await web_search.ainvoke({"query": "x"})
        assert result == {"results": [], "query": "x", "error": "SERPER_API_KEY not set"}
        assert not serper_route.called  # no HTTP call when the key is missing
    finally:
        _restore_key()


@respx.mock
async def test_max_results_caps_organic_and_payload_num():
    _set_key("testkey")
    try:
        route = respx.post(f"{SERPER_BASE}/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "organic": [
                        {"title": f"t{i}", "link": f"u{i}", "snippet": f"s{i}"}
                        for i in range(10)
                    ]
                },
            )
        )
        result = await serper_search_raw("acme", max_results=2)
        assert len(result["results"]) == 2
        sent = json.loads(respx.calls[0].request.content)
        assert sent["num"] == 2
        assert route.called
    finally:
        _restore_key()

# --- multi-key rotation (app/tools/keyrotation.py) ---

def _set_keys(*values: str):
    serper_mod._SERPER_ROTATOR = KeyRotator(values)


@respx.mock
async def test_search_rotates_to_next_key_on_429():
    _set_keys("key1", "key2")
    try:
        route = respx.post(f"{SERPER_BASE}/search").mock(
            side_effect=[
                httpx.Response(429),
                httpx.Response(200, json={"organic": [{"title": "t", "link": "u", "snippet": "s"}]}),
            ]
        )
        result = await serper_search_raw("acme")
        assert route.call_count == 2
        assert respx.calls[0].request.headers.get("X-API-KEY") == "key1"
        assert respx.calls[1].request.headers.get("X-API-KEY") == "key2"
        assert "error" not in result
        assert result["results"][0]["url"] == "u"
    finally:
        _restore_key()


@respx.mock
async def test_search_all_keys_exhausted_degrades_with_error():
    _set_keys("key1", "key2")
    try:
        respx.post(f"{SERPER_BASE}/search").mock(return_value=httpx.Response(429))
        result = await serper_search_raw("acme")
        assert result["results"] == []
        assert "exhausted" in result["error"]
        assert len(respx.calls) == 2  # tried both keys, then gave up
    finally:
        _restore_key()


@respx.mock
async def test_rotation_state_persists_across_calls():
    """Once rotated past a dead key, subsequent calls skip straight to the next key —
    no reason to re-try a key already known exhausted this run."""
    _set_keys("key1", "key2")
    try:
        respx.post(f"{SERPER_BASE}/search").mock(
            side_effect=[
                httpx.Response(429),  # first call: key1 fails, rotates to key2
                httpx.Response(200, json={"organic": []}),
                httpx.Response(200, json={"organic": []}),  # second call: key2 used directly
            ]
        )
        await serper_search_raw("first")
        await serper_search_raw("second")
        assert respx.calls[2].request.headers.get("X-API-KEY") == "key2"
    finally:
        _restore_key()

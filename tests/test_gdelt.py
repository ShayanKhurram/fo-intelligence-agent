"""Tests for the GDELT client's rate-limit handling, added after the first live pilot
run got 429 Too Many Requests on every single GDELT call under batch concurrency."""
from __future__ import annotations

import httpx
import pytest
import respx

import app.tools.gdelt as gdelt_mod
from app.config import SETTINGS


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    import asyncio

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(gdelt_mod.asyncio, "sleep", no_sleep)


@respx.mock
async def test_news_search_success():
    respx.get(SETTINGS.tools.gdelt_base_url).mock(
        return_value=httpx.Response(
            200, json={"articles": [{"title": "x", "url": "http://x", "seendate": "20260101", "domain": "x.com"}]}
        )
    )
    result = await gdelt_mod.news_search_raw("acme")
    assert len(result["results"]) == 1
    assert result["results"][0]["seendate"] == "20260101"
    assert "error" not in result


@respx.mock
async def test_news_search_retries_on_429_then_succeeds():
    route = respx.get(SETTINGS.tools.gdelt_base_url)
    route.side_effect = [
        httpx.Response(429),
        httpx.Response(200, json={"articles": []}),
    ]
    result = await gdelt_mod.news_search_raw("acme")
    assert result["results"] == []
    assert "error" not in result
    assert route.call_count == 2


@respx.mock
async def test_news_search_degrades_after_exhausting_429_retries():
    respx.get(SETTINGS.tools.gdelt_base_url).mock(return_value=httpx.Response(429))
    result = await gdelt_mod.news_search_raw("acme")
    assert result["results"] == []
    assert "429" in result["error"]


@respx.mock
async def test_news_search_degrades_on_connection_error():
    respx.get(SETTINGS.tools.gdelt_base_url).mock(side_effect=httpx.ConnectError("refused"))
    result = await gdelt_mod.news_search_raw("acme")
    assert result["results"] == []
    assert result["error"] is not None

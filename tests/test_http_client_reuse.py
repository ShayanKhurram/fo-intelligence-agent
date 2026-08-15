"""T41 — the free-fetch path must reuse one pooled httpx client.

Constructing an AsyncClient builds an SSL context, and ssl.create_default_context() parses
the whole system CA store synchronously. Done once per fetch, on the event loop, with
Layer V making one fetch per claim per lead across concurrent leads, the loop stops
running coroutines and the run wedges: a real scheduled run sat for ten minutes with 0
leads processed, no tool calls, no log output, and a core burning. py-spy put the stack
squarely in ssl.create_default_context under free_fetch_raw.
"""
from __future__ import annotations

import httpx
import pytest
import respx

import app.tools.freefetch as ff


@pytest.fixture(autouse=True)
def _reset_shared_client():
    """Each test gets a fresh module state, so one test's client cannot mask another's."""
    ff._shared_client = None
    yield
    ff._shared_client = None


@respx.mock
async def test_repeated_fetches_reuse_one_client():
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(200, html="<html><body>" + "word " * 200 + "</body></html>"))
    respx.get("https://example.com/b").mock(
        return_value=httpx.Response(200, html="<html><body>" + "word " * 200 + "</body></html>"))

    await ff.free_fetch_raw("https://example.com/a")
    first = ff._shared_client
    await ff.free_fetch_raw("https://example.com/b")
    await ff.fetch_raw_html("https://example.com/a")

    assert first is not None
    # Same object across every call and across both entry points — one SSL context total,
    # not one per fetch.
    assert ff._shared_client is first


@respx.mock
async def test_a_failed_fetch_does_not_discard_the_client():
    """A dead host must not cost the pool. If a failure replaced the client, a site that
    times out repeatedly would rebuild the SSL context every time — the original bug,
    reintroduced on the error path."""
    respx.get("https://dead.example").mock(side_effect=httpx.ConnectError("refused"))
    respx.get("https://ok.example").mock(
        return_value=httpx.Response(200, html="<html><body>" + "word " * 200 + "</body></html>"))

    assert await ff.free_fetch_raw("https://dead.example") is None
    after_failure = ff._shared_client
    assert await ff.free_fetch_raw("https://ok.example") is not None
    assert ff._shared_client is after_failure


@respx.mock
async def test_the_client_keeps_the_behaviour_the_per_call_one_had():
    """Redirects followed, the research User-Agent sent, and a thin page still rejected —
    the pooled client must not quietly change what a fetch does."""
    route = respx.get("https://example.com/team").mock(
        return_value=httpx.Response(200, html="<html><body>" + "word " * 200 + "</body></html>"))
    thin = respx.get("https://example.com/thin").mock(
        return_value=httpx.Response(200, html="<html><body>tiny</body></html>"))

    assert await ff.free_fetch_raw("https://example.com/team") is not None
    assert await ff.free_fetch_raw("https://example.com/thin") is None
    assert "FO-Intelligence-Agent" in route.calls[0].request.headers["user-agent"]
    assert thin.called
    assert ff._get_shared_client().follow_redirects is True

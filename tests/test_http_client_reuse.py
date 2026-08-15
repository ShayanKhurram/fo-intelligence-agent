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


# ---------------------------------------------------------------------------
# T41b — extraction must never block the event loop, and must be bounded.
# ---------------------------------------------------------------------------


async def test_extraction_runs_off_the_event_loop():
    """`trafilatura.extract` is synchronous. Run on the loop it stops every other
    coroutine — the scheduler's other leads, the progress publisher, and the HTTP server,
    which stopped answering /api/scheduler/live entirely during a live run. Here: while a
    slow extraction is in flight, other coroutines must still get scheduled."""
    import asyncio
    import time

    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    def slow_extract(html, **kwargs):
        time.sleep(0.25)          # blocking, exactly like the real parser
        return "x" * 500

    ff.trafilatura.extract, real = slow_extract, ff.trafilatura.extract
    try:
        t = asyncio.create_task(ticker())
        text = await ff._extract_text("<html></html>")
        await t
    finally:
        ff.trafilatura.extract = real

    assert text is not None
    # If extraction had held the loop, the ticker could not have advanced during it.
    assert ticks >= 15, f"event loop was blocked during extraction (only {ticks} ticks)"


async def test_an_oversized_page_is_refused_before_parsing():
    """One real page drove memory 809MB -> 2.1GB inside trafilatura's repair_faulty_html
    and was heading for an OOM. A firm's team page is tens of KB; multi-MB markup is a
    sitemap or a dump, and nothing this tier wants is in it."""
    called = False

    def should_not_run(html, **kwargs):
        nonlocal called
        called = True
        return "text"

    ff.trafilatura.extract, real = should_not_run, ff.trafilatura.extract
    try:
        assert await ff._extract_text("x" * (ff._MAX_EXTRACT_CHARS + 1)) is None
    finally:
        ff.trafilatura.extract = real
    assert not called, "an oversized page must be refused without being parsed"


async def test_a_pathological_page_is_abandoned_rather_than_waited_on(monkeypatch):
    """The size guard catches the obviously-huge page; this catches the small-but-
    pathological one. Either way the page is treated as unparseable and the caller
    escalates to the headless-browser tier."""
    import time

    monkeypatch.setattr(ff, "_EXTRACT_TIMEOUT_S", 0.1)

    def never_finishes(html, **kwargs):
        time.sleep(5)
        return "text"

    ff.trafilatura.extract, real = never_finishes, ff.trafilatura.extract
    try:
        started = time.monotonic()
        assert await ff._extract_text("<html></html>") is None
        assert time.monotonic() - started < 2, "should abandon, not wait for the parser"
    finally:
        ff.trafilatura.extract = real


async def test_a_parser_crash_is_an_unparseable_page_not_an_error():
    def boom(html, **kwargs):
        raise ValueError("lxml exploded")

    ff.trafilatura.extract, real = boom, ff.trafilatura.extract
    try:
        assert await ff._extract_text("<html></html>") is None
    finally:
        ff.trafilatura.extract = real

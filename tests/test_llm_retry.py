"""Tests for the retry/backoff logic added after the first live pilot run hit real 429s
and a 500 from Ollama Cloud under batch concurrency — see PROJECT_LOG.md's 2026-07-28
pilot-run entry. Backoff sleeps are monkeypatched to no-ops so these run fast offline."""
from __future__ import annotations

import httpx
import pytest
import respx

import app.llm as llm_mod
from app.llm import AnthropicChatModel, OllamaCloudChatModel
from langchain_core.messages import HumanMessage


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    async def no_sleep(attempt, retry_after):
        return None

    monkeypatch.setattr(llm_mod, "_sleep_backoff", no_sleep)


@pytest.fixture(autouse=True)
def _reset_shared_client():
    """The pooled `httpx.AsyncClient` is module-level and would otherwise leak across
    tests (and across respx contexts). Reset it before each test so each starts from a
    clean slate — respx patches `_transport_for_url` at the class level, so a freshly
    created pooled client is still intercepted exactly like a per-call client was."""
    llm_mod._shared_client = None
    yield
    llm_mod._shared_client = None


@respx.mock
async def test_ollama_retries_on_429_then_succeeds():
    route = respx.post("https://ollama.com/v1/chat/completions")
    route.side_effect = [
        httpx.Response(429, headers={"retry-after": "0"}),
        httpx.Response(200, json={"choices": [{"message": {"content": "hi", "tool_calls": None}}], "usage": {}}),
    ]
    model = OllamaCloudChatModel("gpt-oss:20b", api_key="k")
    resp = await model.ainvoke([HumanMessage(content="hi")])
    assert resp.content == "hi"
    assert route.call_count == 2


@respx.mock
async def test_ollama_retries_on_500_then_succeeds():
    route = respx.post("https://ollama.com/v1/chat/completions")
    route.side_effect = [
        httpx.Response(500),
        httpx.Response(200, json={"choices": [{"message": {"content": "ok", "tool_calls": None}}], "usage": {}}),
    ]
    model = OllamaCloudChatModel("gpt-oss:20b", api_key="k")
    resp = await model.ainvoke([HumanMessage(content="hi")])
    assert resp.content == "ok"
    assert route.call_count == 2


@respx.mock
async def test_ollama_raises_after_exhausting_retries():
    respx.post("https://ollama.com/v1/chat/completions").mock(return_value=httpx.Response(429))
    model = OllamaCloudChatModel("gpt-oss:20b", api_key="k")
    with pytest.raises(httpx.HTTPStatusError):
        await model.ainvoke([HumanMessage(content="hi")])


@respx.mock
async def test_ollama_does_not_retry_on_plain_4xx():
    route = respx.post("https://ollama.com/v1/chat/completions").mock(return_value=httpx.Response(401))
    model = OllamaCloudChatModel("gpt-oss:20b", api_key="k")
    with pytest.raises(httpx.HTTPStatusError):
        await model.ainvoke([HumanMessage(content="hi")])
    assert route.call_count == 1  # no retry burned on a non-retryable error


@respx.mock
async def test_anthropic_retries_on_429_then_succeeds():
    route = respx.post("https://api.anthropic.com/v1/messages")
    route.side_effect = [
        httpx.Response(429),
        httpx.Response(200, json={"content": [{"type": "text", "text": "hi"}], "usage": {}}),
    ]
    model = AnthropicChatModel("claude-sonnet-5", api_key="k")
    resp = await model.ainvoke([HumanMessage(content="hi")])
    assert resp.content == "hi"
    assert route.call_count == 2


@respx.mock
async def test_context_truncation_retry_still_works_alongside_backoff():
    """The pre-existing one-time truncation retry (plan §7) must keep working now that
    it shares the loop with the new backoff retry."""
    route = respx.post("https://ollama.com/v1/chat/completions")
    route.side_effect = [
        httpx.Response(400, text="maximum context length exceeded"),
        httpx.Response(200, json={"choices": [{"message": {"content": "truncated ok", "tool_calls": None}}], "usage": {}}),
    ]
    model = OllamaCloudChatModel("gpt-oss:20b", api_key="k")
    resp = await model.ainvoke([HumanMessage(content="a" * 100), HumanMessage(content="b")])
    assert resp.content == "truncated ok"
    assert route.call_count == 2


@respx.mock
async def test_retries_on_transport_error_then_succeeds():
    """A transport-level failure (httpx.ReadError) raised by the transport *before any
    response exists* must be retried like a 429/5xx, not crash the lead outright. respx
    unwraps its SideEffectError and re-raises the original exception instance, so
    `httpx.ReadError` propagates out of `client.post` and is caught by the new
    `except httpx.TransportError` branch."""
    route = respx.post("https://ollama.com/v1/chat/completions")
    route.side_effect = [
        httpx.ReadError("connection reset"),
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok", "tool_calls": None}}], "usage": {}},
        ),
    ]
    model = OllamaCloudChatModel("gpt-oss:20b", api_key="k")
    resp = await model.ainvoke([HumanMessage(content="hi")])
    assert resp.content == "ok"
    assert route.call_count == 2


@respx.mock
async def test_transport_error_exhausted_retries_re_raises():
    """If transport errors persist for all _MAX_ATTEMPTS attempts, the original
    `httpx.TransportError` is re-raised rather than swallowed."""
    respx.post("https://ollama.com/v1/chat/completions").mock(side_effect=httpx.ReadError("down"))
    model = OllamaCloudChatModel("gpt-oss:20b", api_key="k")
    with pytest.raises(httpx.ReadError):
        await model.ainvoke([HumanMessage(content="hi")])


@respx.mock
async def test_shared_client_reused_across_calls(monkeypatch):
    """The pooled `httpx.AsyncClient` is created once and reused across multiple
    `ainvoke` calls (one of the two defects this change fixes: previously each attempt
    opened a fresh client/connection)."""
    route = respx.post("https://ollama.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok", "tool_calls": None}}], "usage": {}},
        )
    )
    init_calls = 0
    real_init = httpx.AsyncClient.__init__

    def counting_init(self, *args, **kwargs):
        nonlocal init_calls
        init_calls += 1
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", counting_init)

    model = OllamaCloudChatModel("gpt-oss:20b", api_key="k")
    await model.ainvoke([HumanMessage(content="hi")])
    await model.ainvoke([HumanMessage(content="hi")])
    assert route.call_count == 2
    assert init_calls == 1
    # The same client object is returned on repeat calls to the helper.
    assert llm_mod._get_shared_client() is llm_mod._get_shared_client()

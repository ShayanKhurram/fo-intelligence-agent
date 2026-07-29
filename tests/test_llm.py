from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.llm import FakeChatModel, _estimate_cost_usd, get_model


async def test_fake_model_fifo_queue():
    model = FakeChatModel()
    model.queue(AIMessage(content="first"))
    model.queue(AIMessage(content="second"))
    r1 = await model.ainvoke([HumanMessage(content="hi")])
    r2 = await model.ainvoke([HumanMessage(content="hi")])
    assert r1.content == "first"
    assert r2.content == "second"


async def test_fake_model_route_matches_before_fifo_fallback():
    model = FakeChatModel()
    model.queue(AIMessage(content="fallback"))
    model.route(
        lambda msgs: any(isinstance(m, SystemMessage) and "special" in str(m.content) for m in msgs),
        AIMessage(content="routed"),
    )
    routed = await model.ainvoke([SystemMessage(content="special case"), HumanMessage(content="x")])
    fallback = await model.ainvoke([SystemMessage(content="ordinary"), HumanMessage(content="x")])
    assert routed.content == "routed"
    assert fallback.content == "fallback"


async def test_fake_model_no_responses_returns_empty_ai_message():
    model = FakeChatModel()
    resp = await model.ainvoke([HumanMessage(content="hi")])
    assert resp.content == ""
    assert resp.tool_calls == []


async def test_fake_model_records_calls():
    model = FakeChatModel()
    model.queue(AIMessage(content="ok"))
    await model.ainvoke([HumanMessage(content="track me")])
    assert len(model.calls) == 1
    assert model.calls[0][0].content == "track me"


def test_get_model_returns_fake_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("FOIA_LLM_PROVIDER", raising=False)
    model = get_model("mid")
    assert isinstance(model, FakeChatModel)


def test_get_model_prefers_ollama_cloud_when_both_keys_set(monkeypatch):
    from app.llm import OllamaCloudChatModel

    monkeypatch.setenv("OLLAMA_API_KEY", "test-ollama-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.delenv("FOIA_LLM_PROVIDER", raising=False)
    model = get_model("strongest")
    assert isinstance(model, OllamaCloudChatModel)
    assert model.model == "glm-5.2"


def test_get_model_forced_provider_overrides_env_keys(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "test-ollama-key")
    monkeypatch.setenv("FOIA_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    model = get_model("cheapest")
    from app.llm import AnthropicChatModel

    assert isinstance(model, AnthropicChatModel)


def test_cost_estimate_scales_with_tokens():
    cheap = _estimate_cost_usd("claude-haiku-4-5-20251001", 1_000_000, 0)
    expensive = _estimate_cost_usd("claude-opus-4-8", 1_000_000, 0)
    assert expensive > cheap
    assert _estimate_cost_usd("claude-haiku-4-5-20251001", 0, 0) == 0.0

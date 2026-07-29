"""LLM abstraction — plan §6 role/tier mapping, provider-agnostic. Every node asks for a
*role* ("supervisor", "researcher", "verdict", "compress", "summarizer"); config.py maps
that role to a tier ("cheapest"/"mid"/"strongest"); this module maps the tier to a
concrete client. Swapping providers means changing the tier maps / env vars below, never
call sites.

Default provider is Ollama Cloud (OpenAI-compatible endpoint at https://ollama.com/v1,
`Authorization: Bearer $OLLAMA_API_KEY`) rather than Anthropic — this workspace's models
run through ollama-cloud (see ~/.pi/agent/settings.json), and per-token pricing there is
far cheaper for a ~150-lead batch. Anthropic remains available as a fallback if
ANTHROPIC_API_KEY is set and OLLAMA_API_KEY isn't; FOIA_LLM_PROVIDER forces the choice.
No SDK dependency for either — both wire formats are small enough to hand-roll over
httpx, which keeps this layer testable offline via FakeChatModel."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import uuid
from abc import ABC, abstractmethod
from typing import Any, Callable, Sequence

logger = logging.getLogger(__name__)

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

# Triggers app.config's .env loader (module-level side effect on first import) before
# the os.environ.get(...) calls below run — those are dict/dataclass-field defaults,
# evaluated once at module-load time, so OLLAMA_API_KEY/SCRAPEOPS_API_KEY/etc. from a
# local .env file need to already be in os.environ by the time this module is imported,
# not just by the time get_model() is later called. Unused otherwise.
import app.config  # noqa: F401

# "cheapest"/"mid"/"strongest" are the only tier names call sites should ever use — never
# hardcode a model id elsewhere.

# Ollama Cloud model ids, verified tool-call-capable and (as of 2026-07-28) not marked for
# retirement in ~/.pi/agent/cache/ollama-cloud-models.json.
OLLAMA_TIER_MODEL_MAP = {
    "cheapest": os.environ.get("FOIA_OLLAMA_MODEL_CHEAPEST", "gpt-oss:20b"),
    "mid": os.environ.get("FOIA_OLLAMA_MODEL_MID", "gpt-oss:120b"),
    "strongest": os.environ.get("FOIA_OLLAMA_MODEL_STRONGEST", "glm-5.2"),
}

# Claude 5-family model ids (see claude-api reference) — used only when the provider
# resolves to Anthropic.
ANTHROPIC_TIER_MODEL_MAP = {
    "cheapest": os.environ.get("FOIA_MODEL_CHEAPEST", "claude-haiku-4-5-20251001"),
    "mid": os.environ.get("FOIA_MODEL_MID", "claude-sonnet-5"),
    "strongest": os.environ.get("FOIA_MODEL_STRONGEST", "claude-opus-4-8"),
}

# $ per million tokens. Ollama Cloud figures are real (pi-ollama-cloud's
# pricing.generated.ts); Anthropic figures are illustrative — plan §5/§9 says to measure
# real cost in the 10-lead pilot and set max_usd from that, not from this table.
MODEL_PRICING_PER_MTOK = {
    "gpt-oss:20b": {"input": 0.03, "output": 0.13},
    "gpt-oss:120b": {"input": 0.037, "output": 0.17},
    "glm-5.2": {"input": 1.4, "output": 4.4},
    "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},
    "claude-opus-4-8": {"input": 15.0, "output": 75.0},
}


def _estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = MODEL_PRICING_PER_MTOK.get(model, {"input": 3.0, "output": 15.0})
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000


class ChatModel(ABC):
    @abstractmethod
    async def ainvoke(
        self, messages: Sequence[BaseMessage], tools: Sequence[BaseTool] | None = None
    ) -> AIMessage: ...


def _tool_to_anthropic_schema(t: BaseTool) -> dict[str, Any]:
    schema = t.args_schema.model_json_schema() if t.args_schema else {"type": "object", "properties": {}}
    schema.pop("title", None)
    return {
        "name": t.name,
        "description": t.description or "",
        "input_schema": schema,
    }


def _messages_to_anthropic(
    messages: Sequence[BaseMessage],
) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            system_parts.append(str(m.content))
        elif isinstance(m, HumanMessage):
            out.append({"role": "user", "content": str(m.content)})
        elif isinstance(m, ToolMessage):
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id,
                            "content": str(m.content),
                        }
                    ],
                }
            )
        elif isinstance(m, AIMessage):
            content: list[dict[str, Any]] = []
            if m.content:
                content.append({"type": "text", "text": str(m.content)})
            for tc in m.tool_calls or []:
                content.append(
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc["args"],
                    }
                )
            out.append({"role": "assistant", "content": content or [{"type": "text", "text": ""}]})
        else:
            out.append({"role": "user", "content": str(m.content)})
    return "\n".join(system_parts), out


def _anthropic_response_to_aimessage(data: dict[str, Any], model: str) -> AIMessage:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {"id": block.get("id", str(uuid.uuid4())), "name": block["name"], "args": block.get("input", {})}
            )
    usage = data.get("usage", {})
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cost_usd = _estimate_cost_usd(model, input_tokens, output_tokens)
    return AIMessage(
        content="\n".join(text_parts),
        tool_calls=tool_calls,
        response_metadata={
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
        },
    )


def _truncate_for_retry(anth_messages: list[dict[str, Any]], keep_last: int = 4) -> list[dict[str, Any]]:
    """Plan §7 — on a token-limit error, retry once with truncated context rather than
    failing outright. Keeps the earliest message (often the task-setting turn) plus the
    most recent `keep_last` messages, which is enough context to keep going without
    resending the full history that just overflowed."""
    if len(anth_messages) <= keep_last + 1:
        return anth_messages
    return [anth_messages[0], *anth_messages[-keep_last:]]


# Rate limits under batch concurrency are the norm, not the exception — the first
# 10-lead pilot run against real Ollama Cloud hit 429s and a 500 within the first minute
# (8 leads x up to 3 parallel lanes x multiple turns each = a real concurrent LLM-call
# burst). Without retry, a rate-limited supervisor call kills the whole lead outright, and
# a rate-limited researcher call silently returns a lane with zero claims — which is
# indistinguishable from "the entity genuinely has no identity evidence." Retrying here is
# what makes that distinction real again.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 5


async def _sleep_backoff(attempt: int, retry_after: str | None) -> None:
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            delay = min(2.0**attempt, 30.0)
    else:
        delay = min(2.0**attempt, 30.0)
    await asyncio.sleep(delay + random.uniform(0, 0.5))


_shared_client: httpx.AsyncClient | None = None


def _get_shared_client() -> httpx.AsyncClient:
    """Lazily-initialized pooled client shared across every LLM call (both providers route
    through `_post_with_retry`). Reusing one client/connection-pool — instead of opening a
    fresh `AsyncClient` per attempt — keeps the simultaneous-new-connection count bounded
    under batch concurrency (many leads x up to 3 parallel lanes x several turns), which is
    what was triggering connection resets. No shutdown logic: this is a batch CLI process
    that exits when its work is done."""
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(
            timeout=120.0,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _shared_client


async def _post_with_retry(url: str, headers: dict[str, str], payload: dict[str, Any], *, messages_key: str) -> dict[str, Any]:
    """Shared retry loop for both providers' chat-completions POST. Exponential backoff
    (honoring Retry-After when present) on 429/5xx; a one-time context-truncation retry on
    a 400/413 that looks like an overflow; retries on transport-level failures
    (`httpx.TransportError`, e.g. ReadError/ConnectError/ReadTimeout/RemoteProtocolError)
    raised before any response exists; raises on anything else or exhausted retries."""
    truncated_once = False
    client = _get_shared_client()
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = await client.post(url, headers=headers, content=json.dumps(payload))
        except httpx.TransportError as exc:
            if attempt < _MAX_ATTEMPTS - 1:
                logger.warning(
                    "LLM call raised %s: %r (attempt %d/%d) — backing off before retry",
                    type(exc).__name__, exc, attempt + 1, _MAX_ATTEMPTS,
                )
                await _sleep_backoff(attempt, None)
                continue
            raise

        if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS - 1:
            logger.warning(
                "LLM call got %s (attempt %d/%d) — backing off before retry",
                resp.status_code, attempt + 1, _MAX_ATTEMPTS,
            )
            await _sleep_backoff(attempt, resp.headers.get("retry-after"))
            continue
        if resp.status_code in (400, 413) and not truncated_once:
            body = resp.text.lower()
            if "too long" in body or "maximum context" in body or "token" in body:
                payload[messages_key] = _truncate_for_retry(payload[messages_key])
                truncated_once = True
                continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("unreachable")  # loop always returns or raises before falling through


class AnthropicChatModel(ChatModel):
    def __init__(self, model: str, api_key: str | None = None, max_tokens: int = 4096) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.max_tokens = max_tokens

    async def ainvoke(
        self, messages: Sequence[BaseMessage], tools: Sequence[BaseTool] | None = None
    ) -> AIMessage:
        if not self.api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set — cannot call the live model. "
                "Use FakeChatModel for offline/dev/test runs."
            )
        system, anth_messages = _messages_to_anthropic(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": anth_messages,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [_tool_to_anthropic_schema(t) for t in tools]

        data = await _post_with_retry(
            "https://api.anthropic.com/v1/messages",
            {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            payload,
            messages_key="messages",
        )
        return _anthropic_response_to_aimessage(data, self.model)


def _tool_to_openai_schema(t: BaseTool) -> dict[str, Any]:
    schema = t.args_schema.model_json_schema() if t.args_schema else {"type": "object", "properties": {}}
    schema.pop("title", None)
    return {
        "type": "function",
        "function": {"name": t.name, "description": t.description or "", "parameters": schema},
    }


def _messages_to_openai(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            out.append({"role": "system", "content": str(m.content)})
        elif isinstance(m, HumanMessage):
            out.append({"role": "user", "content": str(m.content)})
        elif isinstance(m, ToolMessage):
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": str(m.content)})
        elif isinstance(m, AIMessage):
            entry: dict[str, Any] = {"role": "assistant", "content": str(m.content)}
            if m.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])},
                    }
                    for tc in m.tool_calls
                ]
            out.append(entry)
        else:
            out.append({"role": "user", "content": str(m.content)})
    return out


def _openai_response_to_aimessage(data: dict[str, Any], model: str) -> AIMessage:
    message = data["choices"][0]["message"]
    tool_calls: list[dict[str, Any]] = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        tool_calls.append({"id": tc.get("id", str(uuid.uuid4())), "name": fn.get("name", ""), "args": args})

    usage = data.get("usage", {})
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)
    cost_usd = _estimate_cost_usd(model, input_tokens, output_tokens)
    return AIMessage(
        content=message.get("content") or "",
        tool_calls=tool_calls,
        response_metadata={
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "reasoning": message.get("reasoning"),
        },
    )


class OllamaCloudChatModel(ChatModel):
    """Ollama Cloud's OpenAI-compatible chat completions endpoint — verified against the
    live API (https://ollama.com/v1/chat/completions), including a tool-call round trip
    with a `role: "tool"` result message. This is the default provider for this layer."""

    BASE_URL = "https://ollama.com/v1"

    def __init__(self, model: str, api_key: str | None = None, max_tokens: int = 4096) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("OLLAMA_API_KEY", "")
        self.max_tokens = max_tokens

    async def ainvoke(
        self, messages: Sequence[BaseMessage], tools: Sequence[BaseTool] | None = None
    ) -> AIMessage:
        if not self.api_key:
            raise RuntimeError(
                "OLLAMA_API_KEY not set — cannot call the live model. "
                "Use FakeChatModel for offline/dev/test runs."
            )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _messages_to_openai(messages),
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = [_tool_to_openai_schema(t) for t in tools]

        data = await _post_with_retry(
            f"{self.BASE_URL}/chat/completions",
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            payload,
            messages_key="messages",
        )
        return _openai_response_to_aimessage(data, self.model)


class FakeChatModel(ChatModel):
    """Deterministic canned-response model for offline dev/tests. Pass a list of
    AIMessage (or callables returning one) via `responses`; each ainvoke call pops the
    next one. Never touches the network — this is what makes the graph runnable without
    ANTHROPIC_API_KEY or internet access."""

    def __init__(self, responses: list[AIMessage] | None = None) -> None:
        self._responses = list(responses or [])
        self._routes: list[tuple[Callable[[Sequence[BaseMessage]], bool], list[AIMessage]]] = []
        self.calls: list[list[BaseMessage]] = []

    def queue(self, message: AIMessage) -> None:
        """FIFO fallback queue — fine for single-flow tests with no concurrency."""
        self._responses.append(message)

    def route(self, predicate: Callable[[Sequence[BaseMessage]], bool], *messages: AIMessage) -> None:
        """Content-addressed responses for concurrent scenarios (e.g. three researcher
        lanes racing on the same shared model instance): each ainvoke call is matched
        against `predicate(messages)` in registration order, and the first match's own
        FIFO queue is consumed. Falls back to the flat `queue()` list when nothing
        matches — a real provider needs neither, since each call carries its own
        conversation history rather than racing a shared queue."""
        self._routes.append((predicate, list(messages)))

    async def ainvoke(
        self, messages: Sequence[BaseMessage], tools: Sequence[BaseTool] | None = None
    ) -> AIMessage:
        self.calls.append(list(messages))
        response = None
        for predicate, bucket in self._routes:
            if bucket and predicate(messages):
                response = bucket.pop(0)
                break
        if response is None:
            response = self._responses.pop(0) if self._responses else AIMessage(content="", tool_calls=[])
        # No real cost — but stamp the field so cost-accounting code has something to
        # read regardless of whether the live model or the fake answered.
        response.response_metadata.setdefault("cost_usd", 0.0)
        return response


_FAKE_SINGLETON: FakeChatModel | None = None


def _resolve_provider() -> str:
    """FOIA_LLM_PROVIDER forces a choice ("ollama-cloud" | "anthropic" | "fake"). Absent
    that, prefer Ollama Cloud (this workspace's default — see module docstring), then
    Anthropic, then fall back to the offline fake."""
    forced = os.environ.get("FOIA_LLM_PROVIDER", "").strip().lower()
    if forced:
        return forced
    if os.environ.get("OLLAMA_API_KEY"):
        return "ollama-cloud"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "fake"


def get_model(tier: str) -> ChatModel:
    """Resolve a role tier to a concrete ChatModel. Falls back to a shared FakeChatModel
    when neither OLLAMA_API_KEY nor ANTHROPIC_API_KEY is set, so the graph stays runnable
    in dev/CI/offline."""
    provider = _resolve_provider()
    if provider == "ollama-cloud":
        model_id = OLLAMA_TIER_MODEL_MAP.get(tier, OLLAMA_TIER_MODEL_MAP["mid"])
        return OllamaCloudChatModel(model_id)
    if provider == "anthropic":
        model_id = ANTHROPIC_TIER_MODEL_MAP.get(tier, ANTHROPIC_TIER_MODEL_MAP["mid"])
        return AnthropicChatModel(model_id)
    global _FAKE_SINGLETON
    if _FAKE_SINGLETON is None:
        _FAKE_SINGLETON = FakeChatModel()
    return _FAKE_SINGLETON

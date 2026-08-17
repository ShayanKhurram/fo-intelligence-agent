// Ollama Cloud client — this project's existing LLM provider (app/llm.py's
// OllamaCloudChatModel, same OpenAI-compatible endpoint). Used here ONLY for answer
// generation (with mandatory [record_id:field] claim tags). Query understanding is
// deterministic (lib/query-understanding.ts imports no model) and there is no rerank
// caller. Embeddings are NOT handled here — see lib/embeddings.ts (Ollama Cloud has no
// embedding-capable model; local ONNX model used instead, see PROJECT_LOG.md).

const OLLAMA_BASE_URL = "https://ollama.com/v1/chat/completions";

// Tier -> model id. "cheapest" and "mid" stay on gemma4:31b (a non-reasoning instruction
// model, ~10s/call — the latency floor on this backend) as the rollback path: if glm-5.2
// ever misbehaves, set OLLAMA_MODEL_STRONGEST to fall back, or point generation back at
// "cheapest"/"mid". "strongest" is glm-5.2 by direction (app/llm.py's
// OLLAMA_TIER_MODEL_MAP uses the same id for its strongest tier). glm-5.2 IS a reasoning
// model, but its chain-of-thought is emitted in a SIBLING `reasoning` field (verified
// by probing the live endpoint — see lib/ollama.test.ts), never in `content`, so reading
// only `content`/`delta.content` below excludes it by construction; no inline stripping is
// needed. Overridable via env for A/B testing.
const TIER_MODEL: Record<string, string> = {
  cheapest: process.env.OLLAMA_MODEL_CHEAPEST || "gemma4:31b",
  mid: process.env.OLLAMA_MODEL_MID || "gemma4:31b",
  strongest: process.env.OLLAMA_MODEL_STRONGEST || "glm-5.2",
};

export type ChatMessage = { role: "system" | "user" | "assistant"; content: string };

const MAX_ATTEMPTS = 5;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export async function ollamaChat(
  messages: ChatMessage[],
  tier: "cheapest" | "mid" | "strongest" = "cheapest"
): Promise<string> {
  const apiKey = process.env.OLLAMA_API_KEY;
  if (!apiKey) {
    throw new Error("OLLAMA_API_KEY not set");
  }

  // Retry with exponential backoff on transient throttling (429) and 5xx — mirrors
  // app/llm.py's OllamaCloudChatModel, which also retries up to 5 times. Ollama Cloud
  // returns 429 "too many concurrent requests" readily when another process on the same
  // key is busy (e.g. a batch enrichment run), and a single collision must not fail an
  // interactive query. Non-retryable errors (4xx other than 429) throw immediately.
  let lastText = "";
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    const resp = await fetch(OLLAMA_BASE_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: TIER_MODEL[tier],
        messages,
        temperature: 0,
      }),
    });
    if (resp.ok) {
      const data = await resp.json();
      // glm-5.2 emits its chain-of-thought in `message.reasoning` (a sibling field, NOT
      // inline in `content`) — reading only `content` returns final-answer text. Verified
      // against the live endpoint; pinned by lib/ollama.test.ts.
      return data.choices?.[0]?.message?.content ?? "";
    }
    lastText = await resp.text();
    const retryable = resp.status === 429 || resp.status >= 500;
    if (!retryable || attempt === MAX_ATTEMPTS) {
      throw new Error(`Ollama Cloud request failed: ${resp.status} ${lastText}`);
    }
    // 0.5s, 1s, 2s, 4s (+ jitter) — bounded, keeps an interactive query responsive.
    await sleep(500 * 2 ** (attempt - 1) + Math.random() * 250);
  }
  throw new Error(`Ollama Cloud request failed after ${MAX_ATTEMPTS} attempts: ${lastText}`);
}

/** Streaming twin of `ollamaChat` — powers the SSE answer stream (ui_plan.md's `token`
 * events). Ollama Cloud's endpoint is OpenAI-compatible, so `stream: true` returns an
 * SSE body of `data: {...}\n\n` chunks (delta text in `choices[0].delta.content`)
 * terminated by a literal `data: [DONE]` line. Retry is only attempted before the first
 * byte arrives — once content has started streaming to the caller there's no way to
 * "retry" without re-emitting duplicate text, so a mid-stream failure just throws (the
 * route's catch-all turns that into the existing timeout/error message, same as any
 * other generation failure today). */
export async function* ollamaChatStream(
  messages: ChatMessage[],
  tier: "cheapest" | "mid" | "strongest" = "cheapest"
): AsyncGenerator<string> {
  const apiKey = process.env.OLLAMA_API_KEY;
  if (!apiKey) {
    throw new Error("OLLAMA_API_KEY not set");
  }

  let resp: Response | null = null;
  let lastText = "";
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    resp = await fetch(OLLAMA_BASE_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: TIER_MODEL[tier],
        messages,
        temperature: 0,
        stream: true,
      }),
    });
    if (resp.ok) break;
    lastText = await resp.text();
    const retryable = resp.status === 429 || resp.status >= 500;
    if (!retryable || attempt === MAX_ATTEMPTS) {
      throw new Error(`Ollama Cloud stream request failed: ${resp.status} ${lastText}`);
    }
    await sleep(500 * 2 ** (attempt - 1) + Math.random() * 250);
  }
  if (!resp || !resp.ok || !resp.body) {
    throw new Error(`Ollama Cloud stream request failed after ${MAX_ATTEMPTS} attempts: ${lastText}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? ""; // last element may be a partial line — keep it for next read
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("data:")) continue;
      const payload = trimmed.slice("data:".length).trim();
      if (payload === "[DONE]") return;
      let parsed: { choices?: { delta?: { content?: string } }[] };
      try {
        parsed = JSON.parse(payload);
      } catch {
        continue; // a malformed/partial JSON chunk — skip rather than crash the whole stream
      }
      // glm-5.2 streams its chain-of-thought in a sibling `delta.reasoning` field with
      // `delta.content` == "" during the reasoning phase; once reasoning ends, `reasoning`
      // stops appearing and `content` carries answer deltas. Reading only `content` (and
      // skipping empty deltas) therefore yields final-answer deltas only — no reasoning
      // ever reaches the SSE route. Pinned by lib/ollama.test.ts.
      const delta = parsed.choices?.[0]?.delta?.content;
      if (delta) yield delta;
    }
  }
}

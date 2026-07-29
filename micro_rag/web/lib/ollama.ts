// Ollama Cloud client — this project's existing LLM provider (app/llm.py's
// OllamaCloudChatModel, same OpenAI-compatible endpoint). Used here for query
// understanding (filter parsing), generation (with mandatory claim tags), and rerank.
// Embeddings are NOT handled here — see lib/embeddings.ts (Ollama Cloud has no
// embedding-capable model; local ONNX model used instead, see PROJECT_LOG.md).

const OLLAMA_BASE_URL = "https://ollama.com/v1/chat/completions";

// gemma4:31b, NOT gpt-oss — gpt-oss is a reasoning model that emits hidden chain-of-thought
// before every answer, so even a one-word reply took 15-20s on Ollama Cloud. gemma4 is a
// plain instruction model (~10s/call, the floor on this backend) and is more than enough for
// filter parsing and grounded summarization. Overridable via env for A/B testing.
const TIER_MODEL: Record<string, string> = {
  cheapest: process.env.OLLAMA_MODEL_CHEAPEST || "gemma4:31b",
  mid: process.env.OLLAMA_MODEL_MID || "gemma4:31b",
};

export type ChatMessage = { role: "system" | "user" | "assistant"; content: string };

const MAX_ATTEMPTS = 5;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export async function ollamaChat(messages: ChatMessage[], tier: "cheapest" | "mid" = "cheapest"): Promise<string> {
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

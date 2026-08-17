// T48 — pins the glm-5.2 response shape so the "reasoning never reaches the user / Gate 2"
// guarantee stays intact. Run with `npm test` (node --test). Stubs global fetch — no network.
//
// What the live probe found (scripts/probe-glm.mjs, since deleted): glm-5.2 is a reasoning
// model, but its chain-of-thought is carried in a SIBLING `reasoning` field, never inline in
// `content`. Non-streaming: `choices[0].message = { content, reasoning }`. Streaming: each
// `choices[0].delta = { content: "", reasoning: "..." }` during the reasoning phase, then
// `{ content: "..." }` (reasoning absent) for the answer. Because ollamaChat/ollamaChatStream
// read ONLY `content`/`delta.content` and skip empty content, reasoning is excluded by
// construction — no inline marker stripping or buffering is required. These tests pin that
// shape and the transition handling.

import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

import { ollamaChat, ollamaChatStream } from "./ollama.ts";

const MESSAGES = [
  { role: "system" as const, content: "Answer with a tagged sentence." },
  { role: "user" as const, content: "What is Acme's AUM?" },
];

let origFetch: typeof globalThis.fetch;
let origKey: string | undefined;

beforeEach(() => {
  origFetch = globalThis.fetch;
  origKey = process.env.OLLAMA_API_KEY;
  process.env.OLLAMA_API_KEY = "test-key";
});

afterEach(() => {
  globalThis.fetch = origFetch;
  if (origKey === undefined) delete process.env.OLLAMA_API_KEY;
  else process.env.OLLAMA_API_KEY = origKey;
});

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

// Build a ReadableStream that emits the given strings as separate read() chunks. Each entry
// becomes its own Uint8Array push, so the SSE parser's line-buffering is exercised across
// arbitrary byte boundaries.
function streamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c));
      controller.close();
    },
  });
  return { ok: true, status: 200, body: stream } as unknown as Response;
}

// One SSE `data:` line carrying a delta. Use JSON.stringify so the apostrophe / quote
// escaping is never hand-written (a hand-escaped apostrophe previously broke the TS
// parser — see git history).
function sse(delta: Record<string, unknown>): string {
  return `data: ${JSON.stringify({ choices: [{ delta }] })}\n\n`;
}

test("ollamaChat returns only message.content, not the sibling reasoning field", async () => {
  const captured: { model?: string; stream?: boolean } = {};
  globalThis.fetch = (async (_url: string | URL, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body));
    captured.model = body.model;
    captured.stream = body.stream;
    return jsonResponse({
      choices: [
        {
          message: {
            role: "assistant",
            content: "Acme AUM is $650M [disc_abc:overview].",
            reasoning: "The user asks for AUM. Context says $650M. Tag with disc_abc:overview.",
          },
          finish_reason: "stop",
        },
      ],
      usage: {},
    });
  }) as typeof globalThis.fetch;

  const out = await ollamaChat(MESSAGES, "strongest");
  assert.equal(captured.model, "glm-5.2");
  assert.equal(captured.stream, undefined);
  assert.equal(out, "Acme AUM is $650M [disc_abc:overview].");
  assert.ok(!out.includes("The user asks"), "reasoning leaked into the answer");
});

test("ollamaChatStream skips reasoning-phase deltas (content == \"\") and yields only answer deltas", async () => {
  // Reasoning phase: content empty, reasoning sibling carries the CoT. The answer phase
  // begins in a SEPARATE chunk — the reasoning/answer boundary falls between two read()
  // chunks (the last reasoning line and the first answer line are distinct SSE pushes).
  const chunks = [
    sse({ role: "assistant", content: "", reasoning: "Step 1: read context." }),
    sse({ content: "", reasoning: "Step 2: tag it." }),
    sse({ content: "Acme" }),
    sse({ content: " AUM is $650M [disc_abc:overview]." }),
    "data: [DONE]\n\n",
  ];
  globalThis.fetch = (async () => streamResponse(chunks)) as typeof globalThis.fetch;

  const out: string[] = [];
  for await (const d of ollamaChatStream(MESSAGES, "strongest")) out.push(d);
  const joined = out.join("");

  assert.equal(joined, "Acme AUM is $650M [disc_abc:overview].");
  assert.ok(!joined.includes("Step 1"), "reasoning leaked into the stream");
  assert.ok(!joined.includes("Step 2"), "reasoning leaked into the stream");
});

test("ollamaChatStream reassembles an SSE line split across two read() chunks", async () => {
  // The reasoning->answer boundary again splits across chunks, but here a single SSE
  // `data:` line is itself broken across two read() pushes (the answer delta line starts
  // mid-line). The line buffer must reassemble it before JSON.parse runs.
  const answerLine = sse({ content: "Acme [disc_abc:overview]." });
  const half = Math.floor(answerLine.length / 2);
  const chunks = [
    sse({ content: "", reasoning: "thinking" }),
    answerLine.slice(0, half),
    answerLine.slice(half),
    "data: [DONE]\n\n",
  ];
  globalThis.fetch = (async () => streamResponse(chunks)) as typeof globalThis.fetch;

  const out: string[] = [];
  for await (const d of ollamaChatStream(MESSAGES, "strongest")) out.push(d);
  const joined = out.join("");
  assert.equal(joined, "Acme [disc_abc:overview].");
  assert.ok(!joined.includes("thinking"), "reasoning leaked into the stream");
});

test("ollamaChatStream tolerates a malformed JSON chunk without crashing", async () => {
  const chunks = ["data: not-json\n\n", sse({ content: "ok" }), "data: [DONE]\n\n"];
  globalThis.fetch = (async () => streamResponse(chunks)) as typeof globalThis.fetch;
  const out: string[] = [];
  for await (const d of ollamaChatStream(MESSAGES, "strongest")) out.push(d);
  assert.equal(out.join(""), "ok");
});
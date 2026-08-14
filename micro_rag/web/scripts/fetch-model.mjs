#!/usr/bin/env node
// Fetch the query-time embedding model (Xenova/all-MiniLM-L6-v2, ~23 MB quantized) into
// ./models, where lib/embeddings.ts loads it from with local_files_only.
//
// Runs as `prebuild`, so it happens on Vercel too. That is the point: the weights are
// deliberately not committed, and for months they only reached production because CLI
// deploys upload untracked local files. The moment the project started building from git
// instead, /api/query failed at runtime with
//
//     `local_files_only=true` ... file was not found locally at
//     "/var/task/micro_rag/web/models/Xenova/all-MiniLM-L6-v2/tokenizer.json"
//
// while /api/health and the pages kept working — so the app looked fine and every search
// returned "Couldn't complete that search". Fetching at build time makes the deployment
// self-contained regardless of who or what triggered it.
//
// Node rather than the original fetch-model.sh: this has to run on the Vercel build
// container AND on a Windows dev machine, and npm scripts do not get bash on Windows.
// Idempotent — files already present are left alone, so local builds pay nothing.

import { mkdir, stat, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";

const BASE = "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main";
const DEST = join("models", "Xenova", "all-MiniLM-L6-v2");
const FILES = [
  "config.json",
  "tokenizer.json",
  "tokenizer_config.json",
  "onnx/model_quantized.onnx",
];

async function exists(path) {
  try {
    const s = await stat(path);
    return s.size > 0;
  } catch {
    return false;
  }
}

let fetched = 0;
for (const rel of FILES) {
  const out = join(DEST, rel);
  if (await exists(out)) continue;
  await mkdir(dirname(out), { recursive: true });
  const res = await fetch(`${BASE}/${rel}`);
  if (!res.ok) {
    // Fail the build loudly. A deployment that builds "successfully" without the model
    // serves an app whose search is broken in a way only the runtime logs reveal.
    throw new Error(`fetch-model: ${rel} -> HTTP ${res.status} ${res.statusText}`);
  }
  await writeFile(out, Buffer.from(await res.arrayBuffer()));
  fetched += 1;
  console.log(`fetch-model: downloaded ${rel}`);
}

console.log(
  fetched === 0
    ? "fetch-model: model already present, nothing to do"
    : `fetch-model: ${fetched} file(s) downloaded into ${DEST}`
);

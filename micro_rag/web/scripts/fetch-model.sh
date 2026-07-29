#!/usr/bin/env bash
# Fetch the local query-time embedding model (Xenova/all-MiniLM-L6-v2, ~23 MB quantized).
# The weights are intentionally NOT committed; they are bundled into the Vercel deploy via
# next.config's outputFileTracingIncludes and loaded from ./models at runtime (see
# lib/embeddings.ts). Run once after cloning, from micro_rag/web/.
set -euo pipefail

BASE="https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main"
DEST="models/Xenova/all-MiniLM-L6-v2"
mkdir -p "$DEST/onnx"

echo "Fetching all-MiniLM-L6-v2 into $DEST ..."
for f in config.json tokenizer.json tokenizer_config.json; do
  curl -fsSL "$BASE/$f" -o "$DEST/$f"
done
curl -fsSL "$BASE/onnx/model_quantized.onnx" -o "$DEST/onnx/model_quantized.onnx"

echo "Done. Files:"
ls -lh "$DEST" "$DEST/onnx"

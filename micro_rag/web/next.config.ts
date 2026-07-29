import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // @xenova/transformers eagerly imports `onnxruntime-node` at module load (its backend
  // file does `import * as ONNX_NODE from 'onnxruntime-node'` unconditionally), which loads
  // libonnxruntime.so before any WASM fallback can kick in. Vercel installs the correct
  // Linux onnxruntime-node binary during its remote build, but Next's output file tracing
  // doesn't detect the native .so as a dependency of the externalized package, so it never
  // lands in the Lambda → "libonnxruntime.so.1.14.0: cannot open shared object file". Force
  // the whole onnxruntime-node package (JS + native binaries) into the function bundle.
  outputFileTracingIncludes: {
    // The native onnxruntime binary AND the bundled MiniLM model files (lib/embeddings.ts
    // loads the model from ./models locally instead of downloading from the HF hub on every
    // cold start). Both must be force-traced into the /api/query Lambda.
    "/api/query": ["./node_modules/onnxruntime-node/**/*", "./models/**/*"],
  },
};

export default nextConfig;

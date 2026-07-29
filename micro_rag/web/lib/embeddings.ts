// Query-time embeddings — @xenova/transformers running Xenova/all-MiniLM-L6-v2
// (ONNX build of the same model family used for corpus ingestion in
// micro_rag/ingest/embeddings.py's sentence-transformers/all-MiniLM-L6-v2). Both are
// derived from the same pretrained weights, so query vectors land in a compatible
// space with the ingested chunk vectors. Chosen because Ollama Cloud has no
// embedding-capable model (see PROJECT_LOG.md) and this avoids running a separate
// Python service just for query-time embedding.
import path from "node:path";
import { env, pipeline, type FeatureExtractionPipeline } from "@xenova/transformers";

// Load the model from files bundled INTO the deployment (web/models/...) rather than
// downloading it from the HF hub on every cold start. Vercel scales serverless functions to
// zero, so without this each cold request re-downloaded ~23MB and inference took 35-50s;
// loading a local file drops cold start to onnxruntime init only. `allowRemoteModels = false`
// guarantees no network fetch (fail loud if the files aren't traced in, rather than silently
// falling back to a slow download). The models dir is force-included via next.config's
// outputFileTracingIncludes.
env.allowRemoteModels = false;
env.allowLocalModels = true;
env.localModelPath = path.join(process.cwd(), "models");

let extractorPromise: Promise<FeatureExtractionPipeline> | null = null;

function getExtractor(): Promise<FeatureExtractionPipeline> {
  if (!extractorPromise) {
    extractorPromise = pipeline("feature-extraction", "Xenova/all-MiniLM-L6-v2") as Promise<FeatureExtractionPipeline>;
  }
  return extractorPromise;
}

export async function embedQuery(text: string): Promise<number[]> {
  const extractor = await getExtractor();
  const output = await extractor(text, { pooling: "mean", normalize: true });
  return Array.from(output.data as Float32Array);
}

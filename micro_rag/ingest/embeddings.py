"""Local embeddings — sentence-transformers/all-MiniLM-L6-v2, 384-dim. Substitutes for
micro_rag_plan.md §3.3's text-embedding-3-small/1536-dim: Ollama Cloud (this project's
existing LLM provider) has no embedding-capable model in its catalog (checked against
~/.pi/agent/cache/ollama-cloud-models.json — no model declares "embedding" as a
capability), so a new OpenAI key would have been required to follow the plan literally.
User chose a local model instead: free, no new account, and at ~150-300 chunks total
(the whole point the plan itself makes about embedding choice not mattering much at this
scale) quality is not a meaningful trade-off either way."""
from __future__ import annotations

from functools import lru_cache

EMBEDDING_DIM = 384
_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


@lru_cache(maxsize=1)
def _get_model():
    """Load all-MiniLM-L6-v2 via `transformers` directly (AutoModel + AutoTokenizer)
    rather than through `sentence_transformers`. sentence-transformers 5.6 imports
    `AutoProcessor` at module load, which is broken in the installed transformers 5.14.1
    (a lazy-import chain failure unrelated to text models) — AutoModel/AutoTokenizer, the
    only pieces MiniLM needs, import cleanly. Mean-pooling over the last hidden state +
    L2-normalize is exactly what SentenceTransformer("all-MiniLM-L6-v2") computes, so the
    resulting 384-dim vectors are identical (and stay vector-space-compatible with the
    Xenova/all-MiniLM-L6-v2 ONNX model used at query time in the Node.js app)."""
    import torch
    from transformers import AutoModel, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(_MODEL_NAME)
    model = AutoModel.from_pretrained(_MODEL_NAME)
    model.eval()
    return tokenizer, model, torch


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Returns one 384-dim, L2-normalized vector per input text, in order."""
    if not texts:
        return []
    tokenizer, model, torch = _get_model()
    out: list[list[float]] = []
    batch_size = 32
    with torch.no_grad():
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            encoded = tokenizer(
                batch, padding=True, truncation=True, max_length=256, return_tensors="pt"
            )
            model_out = model(**encoded)
            token_embeddings = model_out.last_hidden_state  # (B, T, 384)
            mask = encoded["attention_mask"].unsqueeze(-1).float()  # (B, T, 1)
            summed = (token_embeddings * mask).sum(dim=1)
            counts = mask.sum(dim=1).clamp(min=1e-9)
            mean_pooled = summed / counts  # (B, 384)
            normalized = torch.nn.functional.normalize(mean_pooled, p=2, dim=1)
            out.extend(normalized.cpu().tolist())
    return out

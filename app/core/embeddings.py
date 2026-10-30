"""Local text embeddings (no API, no LLM). The model is downloaded on first use and loaded once per process."""

from functools import lru_cache

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIMENSIONS = 384  # fixed by the model; the log_events.embedding column must match
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "  # BGE: queries only, not documents


@lru_cache
def _model():
    from fastembed import TextEmbedding  # imported lazily: it is heavy and tests that never embed should not pay for it

    from app.core.config import get_settings

    return TextEmbedding(EMBEDDING_MODEL, cache_dir=str(get_settings().embedding_cache_dir))


# ponytail: blocking CPU work; async callers wrap these in asyncio.to_thread.
def embed_documents(texts: list[str]) -> list[list[float]]:
    return [vector.tolist() for vector in _model().embed(texts)]


def embed_query(query: str) -> list[float]:
    return embed_documents([QUERY_PREFIX + query])[0]

import sys
import types

import numpy as np

from app.core import embeddings
from app.core.config import ROOT_DIR, get_settings


class FakeModel:
    def __init__(self):
        self.seen: list[str] = []

    def embed(self, texts):
        self.seen.extend(texts)
        return (np.zeros(embeddings.EMBEDDING_DIMENSIONS, dtype=np.float32) for _ in texts)


def test_queries_get_the_bge_prefix_and_documents_do_not(monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(embeddings, "_model", lambda: fake)

    vector = embeddings.embed_query("db timeouts")
    embeddings.embed_documents(["ERROR pool timeout"])

    assert fake.seen == [embeddings.QUERY_PREFIX + "db timeouts", "ERROR pool timeout"]
    assert len(vector) == embeddings.EMBEDDING_DIMENSIONS and isinstance(vector[0], float)


def test_the_model_is_cached_in_the_configured_directory_not_the_temp_dir(monkeypatch):
    created = []
    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=lambda *a, **kw: created.append((a, kw))))
    embeddings._model.cache_clear()
    try:
        embeddings._model()
    finally:
        embeddings._model.cache_clear()

    assert created == [((embeddings.EMBEDDING_MODEL,), {"cache_dir": str(get_settings().embedding_cache_dir)})]
    assert get_settings().embedding_cache_dir == ROOT_DIR / ".cache" / "fastembed"

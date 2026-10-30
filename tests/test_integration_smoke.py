"""5.0 spike: proves the tables work against real PostgreSQL and the real embedding model runs."""

import math
import time
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.embeddings import EMBEDDING_DIMENSIONS, embed_documents, embed_query
from app.ingestion.semantic_text import build_semantic_text
from app.models import LogEvent
from tests.test_semantic_text import spring_events, spring_npe

VECTOR = [0.1] * EMBEDDING_DIMENSIONS


def make_event(**overrides) -> LogEvent:
    fields = dict(
        source_id="demo", event_key="0:abc", locator={"type": "file", "offset_start": 0},
        raw_text="ERROR HikariPool timeout trace-abc123", semantic_text="ERROR HikariPool timeout trace-abc123",
        fields={"nested": {"a": [1, 2]}}, embedding=VECTOR, embedding_model="test",
    )
    return LogEvent(**{**fields, **overrides})


@pytest.mark.integration
async def test_an_event_round_trips_with_vector_jsonb_and_generated_keyword_index(session):
    session.add(make_event())
    await session.commit()

    event = (await session.execute(select(LogEvent))).scalar_one()
    keyword_hit = await session.scalar(text("select count(*) from log_events where search_tsv @@ plainto_tsquery('simple', 'trace-abc123')"))
    nearest = await session.scalar(text("select event_key from log_events order by embedding <=> cast(:v as vector) limit 1"), {"v": str(VECTOR)})

    assert event.fields == {"nested": {"a": [1, 2]}} and event.locator["type"] == "file"
    assert list(event.embedding) == pytest.approx(VECTOR) and keyword_hit == 1 and nearest == "0:abc"


@pytest.mark.integration
async def test_an_event_without_an_embedding_is_kept(session):
    session.add(make_event(embedding=None, embedding_model=None))
    await session.commit()

    assert (await session.execute(select(LogEvent))).scalar_one().embedding is None


@pytest.mark.integration
async def test_the_same_event_key_twice_in_one_source_conflicts_but_not_across_sources(session):
    session.add_all([make_event(source_id="a"), make_event(source_id="b")])
    await session.commit()

    session.add(make_event(source_id="a"))
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.slow
def test_the_real_model_embeds_the_spring_fixture_and_reports_throughput():
    texts = [build_semantic_text(spring_npe()).text] * 2 + [spring_events()[0]]
    embed_documents(texts[:1])  # warm-up: loads the model

    started = time.perf_counter()
    vectors = embed_documents(texts * 10)
    rate = len(vectors) / (time.perf_counter() - started)
    query = embed_query("Why did checkout fail with a null pointer?")

    print(f"\nembedding throughput: {rate:.1f} docs/sec on this CPU")
    assert all(len(v) == EMBEDDING_DIMENSIONS and all(math.isfinite(x) for x in v) for v in vectors + [query])
    similar = sum(a * b for a, b in zip(query, vectors[0]))  # unit vectors: dot product = cosine similarity
    unrelated = sum(a * b for a, b in zip(query, vectors[2]))
    assert similar > unrelated  # the NPE event is closer to the NPE question than a startup line is

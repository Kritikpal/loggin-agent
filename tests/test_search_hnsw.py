"""The semantic leg with a filter on a table big enough for the planner to pick the approximate HNSW index.

Half the rows are INFO and close to the query, half are ERROR and far from it, so the planner sees the level filter as
unselective and walks the HNSW graph. A plain HNSW scan only looks at hnsw.ef_search nearest rows (all INFO) and then
filters them out, returning nothing; pgvector 0.8's iterative scan keeps walking until it finds ERROR rows.
Slow (about 25 s to insert 8 000 vectors); run with  .venv/Scripts/python -m pytest -m slow tests/test_search_hnsw.py
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

import app.search.queries as search_queries
from app.ingestion.sources import FileSource
from tests.search_helpers import fake_vector, far, insert_rows, make_row, near, search

pytestmark = [pytest.mark.integration, pytest.mark.slow]

HALF = 4_000
CHUNK = 1_000
QUERY = "alpha bravo"  # shares no word with the rows: only the semantic leg answers
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def skewed(tmp_path, engine, session_factory) -> FileSource:
    target = fake_vector(QUERY)
    rows = [make_row(n, f"heartbeat {n}", level="INFO", occurred_at=T0, embedding=near(target, n, 0.5))
            for n in range(HALF)]
    rows += [make_row(HALF + n, f"disk {n}", level="ERROR", occurred_at=T0, embedding=far(target, n))
             for n in range(HALF)]
    for start in range(0, len(rows), CHUNK):
        await insert_rows(session_factory, "big", rows[start:start + CHUNK])
    async with engine.begin() as conn:
        await conn.execute(text("ANALYZE log_events"))
    return FileSource(id="big", path=tmp_path / "big.log")


async def test_a_filtered_semantic_search_still_fills_top_k_through_the_hnsw_index(session_factory, skewed, monkeypatch):
    found = await search(session_factory, [skewed], QUERY, levels=["ERROR"], top_k=10)
    monkeypatch.setattr(search_queries, "HNSW_ITERATIVE_SCAN", "off")
    plain_hnsw = await search(session_factory, [skewed], QUERY, levels=["ERROR"], top_k=10)

    assert len(found.results) == 10 and {hit.level for hit in found.results} == {"ERROR"}
    assert len(plain_hnsw.results) < 10  # proves the index was used and iterative scan is what fills the page

"""Acceptance measurement AC10: search latency over 20 000 indexed events stays under 3 s at p95.
Slow; run with  .venv/Scripts/python -m pytest -m slow -s tests/test_search_perf.py

Query vectors come from the real embed_query, computed ONCE per gold query and cached, so the numbers measure the
search itself (both legs, fusion, coverage), not the embedding model."""

import random
import statistics
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.ingestion.sources import FileSource
from tests.search_helpers import fake_vector, insert_rows, load_gold_set, make_row, search

pytestmark = [pytest.mark.integration, pytest.mark.slow]

ROWS = 20_000
CHUNK = 1_000
REPEATS = 7
P95_LIMIT_S = 3.0  # AC10
SOURCE_IDS = ("auth", "billing", "gateway", "orders")
LEVELS = ("INFO",) * 14 + ("WARN",) * 4 + ("ERROR",) * 2
T0 = datetime(2026, 10, 1, tzinfo=UTC)
TEMPLATES = (
    "{level} c.t.{svc}.Pool HikariPool-1 - Connection is not available, request timed out after 30000ms",
    "{level} c.t.{svc}.Http GET /api/v1/{svc}/{n} completed 200 in {ms} ms",
    "{level} c.t.{svc}.Auth token for user {n} expired; refreshing",
    "{level} o.h.e.j.s.SqlExceptionHelper SQL Error: 0, SQLState: 08001 connection refused port 5432",
    "{level} c.t.{svc}.Jobs scheduled job batch-{n} finished, processed {ms} records",
    "{level} o.s.w.s.DispatcherServlet Completed initialization in {ms} ms",
)


def synthetic_rows(source_id: str, count: int, rng: random.Random):
    for n in range(count):
        level = rng.choice(LEVELS)
        line = rng.choice(TEMPLATES).format(level=level, svc=source_id, n=n, ms=rng.randint(1, 5000))
        yield make_row(n, line, level=level, occurred_at=T0 + timedelta(seconds=n * 30 + rng.randint(0, 29)),
                       embedding=fake_vector(f"{source_id}-{n}"))


def percentiles(samples: list[float]) -> tuple[float, float, float]:
    ordered = sorted(samples)
    return statistics.median(ordered), ordered[int(0.95 * (len(ordered) - 1))], ordered[-1]


async def test_p95_search_latency_over_20k_events_is_under_3_seconds(tmp_path, session_factory, capsys):
    from app.core.embeddings import embed_query

    rng = random.Random(7)
    per_source = ROWS // len(SOURCE_IDS)
    for source_id in SOURCE_IDS:
        rows = list(synthetic_rows(source_id, per_source, rng))
        for start in range(0, len(rows), CHUNK):
            await insert_rows(session_factory, source_id, rows[start:start + CHUNK])
    sources = tuple(FileSource(id=s, path=tmp_path / f"{s}.log") for s in SOURCE_IDS)

    queries = [q.query for q in load_gold_set()]
    vectors = {q: embed_query(q) for q in queries}  # embedding cost kept out of the measurement
    shapes = {
        "unfiltered": {},
        "level=ERROR": {"levels": ["ERROR"]},
        "source+time": {"source_ids": ["billing"], "start_time": T0 + timedelta(days=1),
                        "end_time": T0 + timedelta(days=2)},
    }
    results: dict[str, tuple[float, float, float]] = {}
    for name, filters in shapes.items():
        samples = []
        for _ in range(REPEATS):
            for query in queries:
                started = time.perf_counter()
                await search(session_factory, sources, query, embed=vectors.__getitem__, top_k=10, **filters)
                samples.append(time.perf_counter() - started)
        results[name] = percentiles(samples)

    with capsys.disabled():
        print(f"\n{ROWS} rows, {len(queries)} queries x {REPEATS} repeats per shape")
        for name, (p50, p95, worst) in results.items():
            print(f"{name:12} p50={p50 * 1000:7.1f} ms  p95={p95 * 1000:7.1f} ms  max={worst * 1000:7.1f} ms")
    assert {name: p95 for name, (_, p95, _) in results.items() if p95 >= P95_LIMIT_S} == {}

"""Shared helpers for the search_logs tests: the gold set, a fake embedder, indexing fixtures into the test database,
hand-made rows with chosen vectors, and an HTTP client with the tool's collaborators overridden.

The tool's modules (app.search.hybrid, app.ingestion.coverage, app.controllers.dependencies) are imported INSIDE
functions: written test-first, this kept the modules collectable before the tool existed.
"""

import hashlib
import math
import os
import random
import re
import shutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.core.embeddings import EMBEDDING_DIMENSIONS
from app.ingestion.pipeline import IndexReport, index_source
from app.ingestion.sources import FileSource
from app.ingestion.store import EventRow, upsert_events
from app.models import SourceIndexState

FIXTURES = Path(__file__).parent / "fixtures"
DEV_AUTH_LOG = FIXTURES / "dev-auth-service.log"
SPRING_LOG = FIXTURES / "spring_boot.log"
GOLD_SET = FIXTURES / "dev-auth-service.queries.yaml"
DEV_AUTH_ID = "dev-auth-service"
SPRING_ID = "spring-boot"
KINDS = ("meaning", "identifier", "mixed")
TIMESTAMP_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
HOLD_SECONDS = 1e12  # a settle time so long that the reader always holds the newest event back
OLD_MTIME = datetime(2026, 1, 1, tzinfo=UTC)

# Ground truth read from dev-auth-service.log (see the event map at the top of the gold set).
ERROR_LINES = frozenset({158, 159, 167})
WARN_LINES = frozenset({18, 147, 161, 162, 163})
DEV_AUTH_EVENTS = 30
SPRING_EVENTS = 15


# ---- gold set ----

@dataclass(frozen=True)
class GoldQuery:
    id: str
    query: str
    kind: str
    expect_lines: tuple[int, ...]
    note: str


def load_gold_set(path: Path = GOLD_SET) -> tuple[GoldQuery, ...]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return tuple(
        GoldQuery(q["id"], q["query"], q["kind"], tuple(q["expect_lines"]), q.get("note", ""))
        for q in document["queries"]
    )


def event_start_lines(path: Path = DEV_AUTH_LOG) -> frozenset[int]:
    """1-based numbers of the lines that begin with a timestamp, i.e. where an event starts."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return frozenset(n for n, line in enumerate(lines, 1) if TIMESTAMP_LINE.match(line))


# ---- fake embedder ----

def fake_vector(text: str) -> list[float]:
    """A deterministic 384-dim unit vector derived from a hash of the text: equal texts give equal vectors,
    different texts give unrelated ones, so the semantic leg is reproducible noise."""
    rng = random.Random(hashlib.sha256(text.encode("utf-8")).digest())
    vector = [rng.gauss(0.0, 1.0) for _ in range(EMBEDDING_DIMENSIONS)]
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


def fake_embed_query(query: str) -> list[float]:
    return fake_vector(query)


def fake_embed_documents(texts: list[str]) -> list[list[float]]:
    return [fake_vector(text) for text in texts]


def near(vector: Sequence[float], seed: int, spread: float = 0.05) -> list[float]:
    """A unit vector close to `vector` (cosine similarity ~0.99 for the default spread)."""
    noise = fake_vector(f"noise-{seed}")
    mixed = [v + spread * n for v, n in zip(vector, noise)]
    norm = math.sqrt(sum(x * x for x in mixed))
    return [x / norm for x in mixed]


def far(vector: Sequence[float], seed: int) -> list[float]:
    """A unit vector pointing away from `vector` (cosine similarity ~ -0.99)."""
    return [-x for x in near(vector, seed)]


# ---- sources and indexing ----

def dev_auth_source(path: Path, source_id: str = DEV_AUTH_ID) -> FileSource:
    return FileSource(id=source_id, path=path, timezone="Asia/Kolkata", app_packages=("com.tatapower.smrd",))


def spring_source(path: Path, source_id: str = SPRING_ID) -> FileSource:
    return FileSource(id=source_id, path=path, app_packages=("com.example",))


def copy_fixture(fixture: Path, directory: Path, name: str | None = None) -> Path:
    """A private copy, so tests can append to, touch or delete it without harming the fixture. Its mtime is pinned
    well in the past, so a later index run is always newer than the file (coverage 'complete', never by luck)."""
    target = directory / (name or fixture.name)
    shutil.copyfile(fixture, target)
    set_mtime(target, OLD_MTIME)
    return target


async def index_file(
    source: FileSource, session_factory: Callable, *, settle_seconds: float = 0.0,
    embed: Callable[[list[str]], list[list[float]]] = fake_embed_documents,
) -> IndexReport:
    return await index_source(source, session_factory, embed=embed, settle_seconds=settle_seconds)


def make_row(
    n: int, semantic_text: str, *, level: str | None, occurred_at: datetime | None,
    embedding: list[float] | None = None, raw_text: str | None = None,
) -> EventRow:
    raw = raw_text if raw_text is not None else semantic_text
    return EventRow(
        event_key=f"hand:{n}", locator={"type": "file", "path": "hand.log", "line_start": n, "line_end": n},
        occurred_at=occurred_at, level=level, raw_text=raw, fields={}, semantic_text=semantic_text,
        embedding=embedding, embedding_model="fake" if embedding is not None else None,
    )


async def insert_rows(session_factory: Callable, source_id: str, rows: Iterable[EventRow]) -> None:
    async with session_factory() as session, session.begin():
        await upsert_events(session, source_id, list(rows))


async def state_row(session_factory: Callable, source_id: str) -> SourceIndexState | None:
    async with session_factory() as session:
        return await session.get(SourceIndexState, source_id)


def set_mtime(path: Path, when: datetime) -> None:
    """Pin the file's mtime to an exact instant, so stale/complete never depend on filesystem timing."""
    ns = int(when.timestamp() * 1_000_000_000)
    os.utime(path, ns=(ns, ns))


# ---- calling the tool ----

def request(query: str, **kwargs: Any):
    from app.schemas.search_logs import SearchLogsRequest

    return SearchLogsRequest(query=query, **kwargs)


async def search(
    session_factory: Callable, sources: Sequence[FileSource], query: str, *,
    embed: Callable[[str], list[float]] = fake_embed_query, **kwargs: Any,
):
    from app.search.hybrid import search_logs  # the new signature; imported here so collection never fails

    async with session_factory() as session:
        return await search_logs(request(query, **kwargs), session, sources=tuple(sources), embed=embed)


async def coverage(session_factory: Callable, source: FileSource):
    from app.ingestion.coverage import coverage_for

    async with session_factory() as session:
        return await coverage_for(session, source)


def hit_lines(response: Any) -> list[int]:
    return [hit.locator["line_start"] for hit in response.results]

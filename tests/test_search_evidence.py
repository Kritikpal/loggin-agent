"""search_logs evidence (AC traceability): every hit leads back to the exact bytes of the original log."""

import hashlib
import math

import pytest

from app.ingestion.sources import FileSource
from tests.search_helpers import (
    DEV_AUTH_ID, DEV_AUTH_LOG, TIMESTAMP_LINE, copy_fixture, dev_auth_source, event_start_lines, index_file,
    search,
)

pytestmark = pytest.mark.integration

QUERIES = ("Why did the application fail to start?", "HikariPool-1", "memory leak detection", "which port")


def normalise(text: str) -> str:
    return text.replace("\r\n", "\n").rstrip("\n")


@pytest.fixture
async def indexed(tmp_path, session_factory) -> FileSource:
    source = dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))
    await index_file(source, session_factory)
    return source


@pytest.mark.parametrize("query", QUERIES)
async def test_every_hit_reproduces_its_original_bytes(session_factory, indexed, query):
    response = await search(session_factory, [indexed], query, top_k=50)

    data = indexed.path.read_bytes()
    mismatched = [
        hit.locator["line_start"] for hit in response.results
        if normalise(data[hit.locator["offset_start"]:hit.locator["offset_end"]].decode("utf-8")) != normalise(hit.raw_text)
    ]
    assert response.results and mismatched == []


@pytest.mark.parametrize("query", QUERIES)
async def test_every_hit_carries_the_hash_of_its_original_bytes(session_factory, indexed, query):
    response = await search(session_factory, [indexed], query, top_k=50)

    data = indexed.path.read_bytes()
    wrong = [
        hit.locator["line_start"] for hit in response.results
        if hashlib.sha256(data[hit.locator["offset_start"]:hit.locator["offset_end"]]).hexdigest() != hit.locator["sha256"]
    ]
    assert response.results and wrong == []


@pytest.mark.parametrize("query", QUERIES)
async def test_line_start_points_at_the_first_line_of_the_event(session_factory, indexed, query):
    response = await search(session_factory, [indexed], query, top_k=50)

    lines = indexed.path.read_text(encoding="utf-8").splitlines()
    wrong = [
        hit.locator["line_start"] for hit in response.results
        if not TIMESTAMP_LINE.match(lines[hit.locator["line_start"] - 1])
        or lines[hit.locator["line_start"] - 1] != normalise(hit.raw_text).split("\n")[0]
    ]
    assert response.results and wrong == []


async def test_every_hit_names_its_source_level_time_path_and_a_finite_score(session_factory, indexed):
    response = await search(session_factory, [indexed], "what went wrong", top_k=50)

    incomplete = [
        hit.locator["line_start"] for hit in response.results
        if hit.source_id != DEV_AUTH_ID or not hit.level or hit.occurred_at is None or not math.isfinite(hit.score)
        or hit.occurred_at.utcoffset() is None or hit.locator.get("type") != "file"
    ]
    paths = {hit.locator["path"] for hit in response.results}
    assert incomplete == [] and len(paths) == 1 and indexed.path.samefile(paths.pop())


async def test_a_wide_search_reaches_every_event_of_the_file(session_factory, indexed):
    response = await search(session_factory, [indexed], "startup", top_k=50)

    lines = [hit.locator["line_start"] for hit in response.results]
    assert set(lines) == event_start_lines(indexed.path) and len(lines) == len(set(lines))

"""Index coverage (AC7): coverage_for, the coverage in every search response, freshness, and GET /api/v1/sources.
mtimes are pinned with os.utime relative to the state row's updated_at, so no test depends on filesystem timing."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.ingestion.readers.base import Cursor
from app.ingestion.sources import FileSource
from app.ingestion.store import record_error
from tests.search_helpers import (
    DEV_AUTH_ID, DEV_AUTH_LOG, HOLD_SECONDS, OLD_MTIME, SPRING_ID, SPRING_LOG, api_client, copy_fixture, coverage,
    dev_auth_source, hit_lines, index_file, search, set_mtime, spring_source, state_row,
)

pytestmark = pytest.mark.integration

LAST_EVENT_AT = datetime(2026, 3, 10, 10, 14, 8, 219000, tzinfo=UTC)  # line 167, 15:44:08.219 Asia/Kolkata
NEW_EVENTS = (
    "2026-03-10 15:50:01.000 [main] ERROR c.t.smrd.Probe - freshness probe one\n"
    "2026-03-10 15:50:02.000 [main] WARN  c.t.smrd.Probe - freshness probe two\n"
    "2026-03-10 15:50:03.000 [main] INFO  c.t.smrd.Probe - freshness probe three\n"
)
HOUR = timedelta(hours=1)


@pytest.fixture
def source(tmp_path) -> FileSource:
    return dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))


async def updated_at(session_factory, source_id: str = DEV_AUTH_ID) -> datetime:
    return (await state_row(session_factory, source_id)).updated_at


def append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(text)


async def test_a_source_never_indexed_is_not_indexed(session_factory, source):
    result = await coverage(session_factory, source)

    assert (result.source_id, result.status, result.indexed_through, result.last_error) == (
        DEV_AUTH_ID, "not_indexed", None, None)


async def test_a_fully_indexed_unchanged_file_is_complete(session_factory, source):
    await index_file(source, session_factory)
    set_mtime(source.path, await updated_at(session_factory) - HOUR)

    result = await coverage(session_factory, source)

    assert (result.status, result.indexed_through, result.last_error) == ("complete", LAST_EVENT_AT, None)


async def test_a_file_modified_after_the_last_index_run_is_stale(session_factory, source):
    await index_file(source, session_factory)
    append(source.path, NEW_EVENTS)
    set_mtime(source.path, await updated_at(session_factory) + HOUR)

    result = await coverage(session_factory, source)

    assert (result.status, result.indexed_through) == ("stale", LAST_EVENT_AT)


async def test_an_unconsumed_tail_of_an_unchanged_file_is_partial(session_factory, source):
    await index_file(source, session_factory, settle_seconds=HOLD_SECONDS)  # the newest event is held back
    state = await state_row(session_factory, DEV_AUTH_ID)
    assert Cursor.from_json(state.cursor).offset < source.path.stat().st_size  # precondition, not the behaviour
    set_mtime(source.path, state.updated_at - HOUR)

    result = await coverage(session_factory, source)

    assert result.status == "partial" and result.indexed_through < LAST_EVENT_AT


async def test_a_recorded_error_wins_and_is_reported(session_factory, source):
    await index_file(source, session_factory)
    async with session_factory() as s, s.begin():
        await record_error(s, DEV_AUTH_ID, "OSError")

    result = await coverage(session_factory, source)

    assert (result.status, result.last_error, result.indexed_through) == ("error", "OSError", LAST_EVENT_AT)


async def test_a_file_deleted_after_indexing_reports_an_error_once_indexing_notices(session_factory, source):
    await index_file(source, session_factory)
    source.path.unlink()
    await index_file(source, session_factory)

    result = await coverage(session_factory, source)

    assert (result.status, result.last_error) == ("error", "FileNotFoundError")


async def test_the_search_response_carries_the_same_coverage(session_factory, source):
    await index_file(source, session_factory)
    append(source.path, NEW_EVENTS)
    set_mtime(source.path, await updated_at(session_factory) + HOUR)

    response = await search(session_factory, [source], "Application run failed")
    direct = await coverage(session_factory, source)

    assert response.results and [c.model_dump() for c in response.coverage] == [direct.model_dump()]


async def test_new_lines_appear_only_after_reindexing_and_coverage_says_so(session_factory, source):
    await index_file(source, session_factory)
    append(source.path, NEW_EVENTS)
    set_mtime(source.path, await updated_at(session_factory) + HOUR)

    before = await search(session_factory, [source], "freshness probe", top_k=50)
    set_mtime(source.path, OLD_MTIME)  # a future mtime would make the reader hold the newest event back as unsettled
    await index_file(source, session_factory)
    set_mtime(source.path, await updated_at(session_factory) - HOUR)  # the file was written before this run
    after = await search(session_factory, [source], "freshness probe", top_k=50)

    assert not {324, 325, 326} & set(hit_lines(before)) and before.coverage[0].status == "stale"
    assert {324, 325, 326} <= set(hit_lines(after)) and after.coverage[0].status == "complete"
    assert after.coverage[0].indexed_through == datetime(2026, 3, 10, 10, 20, 3, tzinfo=UTC)


async def test_coverage_is_reported_for_every_searched_source_even_with_no_results(session_factory, tmp_path, source):
    spring = spring_source(copy_fixture(SPRING_LOG, tmp_path))
    await index_file(source, session_factory)

    response = await search(session_factory, [source, spring], "x", start_time=datetime(2030, 1, 1, tzinfo=UTC))

    assert response.results == []
    assert sorted((c.source_id, c.status) for c in response.coverage) == [
        (DEV_AUTH_ID, "complete"), (SPRING_ID, "not_indexed")]


async def test_the_sources_endpoint_lists_every_configured_source_with_its_status(session_factory, tmp_path, source):
    spring = spring_source(copy_fixture(SPRING_LOG, tmp_path))
    await index_file(source, session_factory)

    async with api_client(session_factory, [source, spring]) as http:
        res = await http.get("/api/v1/sources")

    body = sorted(res.json(), key=lambda item: item["id"])
    assert res.status_code == 200
    assert [set(item) for item in body] == [{"id", "type", "format", "status", "indexed_through", "last_error"}] * 2
    assert [(i["id"], i["type"], i["format"], i["status"]) for i in body] == [
        (DEV_AUTH_ID, "file", "text", "complete"), (SPRING_ID, "file", "text", "not_indexed")]
    assert body[0]["indexed_through"] is not None and body[1]["indexed_through"] is None


async def test_the_sources_endpoint_with_no_sources_is_an_empty_list(session_factory):
    async with api_client(session_factory, []) as http:
        res = await http.get("/api/v1/sources")

    assert (res.status_code, res.json()) == (200, [])

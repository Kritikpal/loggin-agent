"""search_logs: filters, ranking, top_k, the source allowlist and the two search legs. Fake embedder only, so the
semantic leg is deterministic noise and every ranking assertion here is about filtering and fusion, not quality."""

import math
from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import NotFoundError
from app.ingestion.sources import FileSource
from tests.search_helpers import (
    DEV_AUTH_EVENTS, DEV_AUTH_ID, DEV_AUTH_LOG, ERROR_LINES, SPRING_EVENTS, SPRING_ID, SPRING_LOG,
    WARN_LINES, copy_fixture, dev_auth_source, fake_vector, far, hit_lines, index_file, insert_rows, make_row, near,
    search, spring_source,
)

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 29, 8, 0, tzinfo=UTC)
NO_KEYWORD_QUERY = "qwxz plover zebu"  # none of these words occurs in either fixture: only the semantic leg can match


def at(hh_mm_ss_ms: str) -> datetime:
    """A UTC instant on the fixture's day; the log's 15:44 Asia/Kolkata is 10:14 UTC."""
    return datetime.fromisoformat(f"2026-03-10T{hh_mm_ss_ms}+00:00")


@pytest.fixture
async def dev_auth(tmp_path, session_factory) -> FileSource:
    source = dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))
    await index_file(source, session_factory)
    return source


@pytest.fixture
async def both(tmp_path, session_factory, dev_auth) -> tuple[FileSource, FileSource]:
    spring = spring_source(copy_fixture(SPRING_LOG, tmp_path))
    await index_file(spring, session_factory)
    return dev_auth, spring


@pytest.fixture
def hand_source(tmp_path) -> FileSource:
    path = tmp_path / "hand.log"
    path.write_text("", encoding="utf-8")
    return FileSource(id="hand", path=path)


# ---- level filter ----

# ---- time filter ----

# ---- source filter ----

# ---- ranking and top_k ----

async def test_filters_apply_before_top_k(session_factory, hand_source):
    query = "alpha bravo"  # shares no word with the rows: the keyword leg stays silent
    target = fake_vector(query)
    infos = [make_row(n, f"routine heartbeat {n}", level="INFO", occurred_at=T0 + timedelta(seconds=n),
                      embedding=near(target, n)) for n in range(20)]
    errors = [make_row(100 + n, f"disk failure {n}", level="ERROR", occurred_at=T0 + timedelta(seconds=n),
                       embedding=far(target, 100 + n)) for n in range(3)]
    await insert_rows(session_factory, hand_source.id, infos + errors)

    filtered = await search(session_factory, [hand_source], query, levels=["ERROR"], top_k=5)
    unfiltered = await search(session_factory, [hand_source], query, top_k=5)

    assert sorted(hit_lines(filtered)) == [100, 101, 102]
    assert {hit.level for hit in unfiltered.results} == {"INFO"} and len(unfiltered.results) == 5


# ---- the two legs ----

async def test_rows_without_an_embedding_are_found_by_keyword_and_skipped_by_the_semantic_leg(session_factory, hand_source):
    unembedded = [make_row(n, f"quokka overflow in ledger {n}", level="ERROR", occurred_at=T0) for n in range(2)]
    embedded = [make_row(10 + n, f"plain event {n}", level="INFO", occurred_at=T0, embedding=fake_vector(str(n)))
                for n in range(3)]
    await insert_rows(session_factory, hand_source.id, unembedded + embedded)

    keyword = await search(session_factory, [hand_source], "quokka", top_k=10)
    semantic = await search(session_factory, [hand_source], NO_KEYWORD_QUERY, top_k=10)

    assert sorted(hit_lines(keyword))[:2] == [0, 1]
    assert {tuple(h.matched_by) for h in keyword.results if h.locator["line_start"] < 10} == {("keyword",)}
    assert sorted(hit_lines(semantic)) == [10, 11, 12]

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

@pytest.mark.parametrize(("levels", "expected"), [
    (["ERROR"], ERROR_LINES), (["warn"], WARN_LINES), (["error", "WARN"], ERROR_LINES | WARN_LINES),
], ids=["error", "warn_lowercase", "error_and_warn"])
async def test_the_level_filter_returns_exactly_the_events_of_those_levels(session_factory, dev_auth, levels, expected):
    response = await search(session_factory, [dev_auth], "what went wrong", levels=levels, top_k=50)

    assert set(hit_lines(response)) == set(expected) and len(response.results) == len(expected)


async def test_a_junk_level_finds_nothing_and_is_not_an_error(session_factory, dev_auth):
    response = await search(session_factory, [dev_auth], "what went wrong", levels=["NOPE' OR '1'='1"])

    assert response.results == [] and [c.source_id for c in response.coverage] == [DEV_AUTH_ID]


# ---- time filter ----

async def test_a_time_window_returns_only_the_events_inside_it(session_factory, dev_auth):
    response = await search(
        session_factory, [dev_auth], "startup", start_time=at("10:14:08.160"), end_time=at("10:14:08.180"), top_k=50)

    assert sorted(hit_lines(response)) == [157, 158, 159]
    assert all(at("10:14:08.160") <= hit.occurred_at <= at("10:14:08.180") for hit in response.results)


async def test_a_window_outside_the_file_finds_nothing_but_still_reports_coverage(session_factory, dev_auth):
    response = await search(session_factory, [dev_auth], "startup", start_time=datetime(2030, 1, 1, tzinfo=UTC))

    assert response.results == []
    assert [(c.source_id, c.status) for c in response.coverage] == [(DEV_AUTH_ID, "complete")]


# ---- source filter ----

async def test_source_ids_isolate_one_source(session_factory, both):
    response = await search(session_factory, both, "Tomcat initialized with port", source_ids=[SPRING_ID], top_k=50)

    assert {hit.source_id for hit in response.results} == {SPRING_ID} and len(response.results) == SPRING_EVENTS
    assert [c.source_id for c in response.coverage] == [SPRING_ID]


async def test_no_source_ids_searches_every_configured_source(session_factory, both):
    response = await search(session_factory, both, "Tomcat initialized with port", top_k=50)

    assert {hit.source_id for hit in response.results} == {DEV_AUTH_ID, SPRING_ID}
    assert len(response.results) == DEV_AUTH_EVENTS + SPRING_EVENTS
    assert sorted(c.source_id for c in response.coverage) == sorted([DEV_AUTH_ID, SPRING_ID])


async def test_rows_of_a_source_that_is_not_configured_are_never_returned(session_factory, dev_auth, hand_source):
    await insert_rows(session_factory, "retired", [make_row(1, "Tomcat port", level="ERROR", occurred_at=T0)])

    response = await search(session_factory, [dev_auth], "Tomcat port", top_k=50)

    assert "retired" not in {hit.source_id for hit in response.results}


async def test_an_unknown_source_id_is_refused(session_factory, dev_auth):
    with pytest.raises(NotFoundError):
        await search(session_factory, [dev_auth], "startup", source_ids=[DEV_AUTH_ID, "not-configured"])


async def test_combined_filters_all_apply(session_factory, both):
    response = await search(
        session_factory, both, "warning", source_ids=[DEV_AUTH_ID], levels=["WARN"],
        start_time=at("10:14:08.190"), end_time=at("10:14:08.200"), top_k=50,
    )

    assert sorted(hit_lines(response)) == [161, 162, 163]
    assert {(hit.source_id, hit.level) for hit in response.results} == {(DEV_AUTH_ID, "WARN")}


# ---- ranking and top_k ----

async def test_top_k_is_respected_and_the_order_is_deterministic(session_factory, dev_auth):
    first = await search(session_factory, [dev_auth], "database connection failed", top_k=7)
    second = await search(session_factory, [dev_auth], "database connection failed", top_k=7)
    scores = [hit.score for hit in first.results]

    assert len(first.results) == 7
    assert [h.event_id for h in first.results] == [h.event_id for h in second.results]
    assert all(math.isfinite(s) for s in scores) and scores == sorted(scores, reverse=True)


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

async def test_an_identifier_in_a_question_is_found_by_the_keyword_leg(session_factory, dev_auth):
    response = await search(session_factory, [dev_auth], "why does jwtAuthFilter fail", top_k=5)

    by_line = {hit.locator["line_start"]: hit for hit in response.results}
    assert 159 in by_line, hit_lines(response)
    assert "keyword" in by_line[159].matched_by


async def test_a_question_with_no_keyword_match_is_answered_by_the_semantic_leg(session_factory, dev_auth):
    response = await search(session_factory, [dev_auth], NO_KEYWORD_QUERY, top_k=5)

    assert len(response.results) == 5
    assert [hit.matched_by for hit in response.results] == [["semantic"]] * 5


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


async def test_an_empty_index_finds_nothing_and_says_not_indexed(session_factory, tmp_path):
    source = dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))

    response = await search(session_factory, [source], "why did startup fail")

    assert response.results == []
    assert [(c.source_id, c.status, c.indexed_through) for c in response.coverage] == [(DEV_AUTH_ID, "not_indexed", None)]

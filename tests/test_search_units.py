"""Pieces of search_logs the behaviour suites do not pin down: lexeme extraction, tsquery text, fusion, the embedder
fallback, the HTTP dependencies and coverage edge cases."""

import logging
import math
import uuid
from datetime import UTC, datetime

import pytest

from app.controllers import dependencies
from app.core.embeddings import embed_query
from app.ingestion.sources import FileSource
from app.ingestion.store import record_error
from app.tools.keyword_query import MAX_LEXEMES, keyword_lexemes, tsquery_text
from app.tools.search_logs import RRF_K, fuse
from app.tools.search_queries import Candidate
from tests.search_helpers import (
    DEV_AUTH_ID, DEV_AUTH_LOG, api_client, copy_fixture, coverage, dev_auth_source, hit_lines, index_file, search,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


# ---- lexemes and tsquery ----

@pytest.mark.parametrize(("question", "expected"), [
    ("why does jwtAuthFilter fail", ("jwtauthfilter", "jwt", "auth", "filter", "fail")),
    ("${SPRING_DATASOURCE_URL}", ("spring_datasource_url", "spring", "datasource", "url")),
    ("Tomcat/11.0.12", ("tomcat/11.0.12", "tomcat", "11", "12")),
    ("org.postgresql.Driver", ("org.postgresql.driver", "org", "postgresql", "driver")),
    ("HikariPool-1 on port 8080", ("hikaripool-1", "hikari", "pool", "port", "8080")),
    ("HHH000342", ("hhh000342", "hhh", "000342")),
    ("?? !! ... the a of", ()),
    ("boot\u0000fail ' OR 1=1 --", ("boot", "fail")),
], ids=["camel", "snake_placeholder", "slash_version", "dotted", "hyphen_and_digits", "code", "only_stop_words",
        "nul_and_quotes"])
def test_keyword_lexemes(question, expected):
    assert keyword_lexemes(question) == expected


def test_lexemes_are_bounded_and_always_safe():
    lexemes = keyword_lexemes(" ".join(f"word{n}" for n in range(500)) + " 'x\\y' <-> :* !&|")

    assert len(lexemes) == MAX_LEXEMES and all("'" not in lex and "\\" not in lex for lex in lexemes)


def test_tsquery_text_quotes_each_lexeme_and_drops_anything_unsafe():
    assert tsquery_text(("jwtauthfilter", "tomcat/11.0.12")) == "'jwtauthfilter' | 'tomcat/11.0.12'"
    assert tsquery_text(("ok", "x' | '1", "a\\b", "-lead")) == "'ok'"
    assert tsquery_text(()) is None


# ---- fusion ----

def candidate(n: int, *, at: datetime | None = T0, key: str | None = None) -> Candidate:
    return Candidate(uuid.UUID(int=n), at, key or f"k{n}", 0.0)


def test_rrf_adds_the_legs_and_records_who_found_each_hit():
    a, b, c = candidate(1), candidate(2), candidate(3)

    fused = fuse({"semantic": [a, b], "keyword": [b, c]})

    assert [f.id.int for f in fused] == [2, 1, 3]
    assert fused[0].score == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))
    assert [f.matched_by for f in fused] == [("semantic", "keyword"), ("semantic",), ("keyword",)]
    assert all(math.isfinite(f.score) for f in fused)


def test_rrf_ties_go_to_the_newest_event_then_the_event_key_and_untimed_events_last():
    older, newer = candidate(1, at=T0.replace(year=2025)), candidate(2)
    untimed, same_time = candidate(3, at=None, key="a"), candidate(4, key="a")

    order = fuse({"semantic": [older, untimed], "keyword": [newer, same_time]})

    assert [f.id.int for f in order] == [2, 1, 4, 3]


def test_no_legs_means_no_hits():
    assert fuse({}) == []


# ---- dependencies ----

def test_get_sources_reads_the_configured_file(monkeypatch, tmp_path):
    config = tmp_path / "sources.yaml"
    config.write_text("sources:\n  - {id: a, type: file, path: a.log}\n", encoding="utf-8")
    monkeypatch.setattr(dependencies, "get_settings", lambda: type("S", (), {"sources_config_path": config})())

    assert [s.id for s in dependencies.get_sources()] == ["a"]


def test_get_sources_without_a_config_file_is_no_sources(monkeypatch, tmp_path):
    missing = tmp_path / "nope.yaml"
    monkeypatch.setattr(dependencies, "get_settings", lambda: type("S", (), {"sources_config_path": missing})())

    assert dependencies.get_sources() == ()


def test_get_embedder_is_the_real_query_embedder():
    assert dependencies.get_embedder() is embed_query


# ---- embedder fallback (integration) ----

def broken(query: str) -> list[float]:
    raise RuntimeError(f"model crashed on {query}")


@pytest.mark.integration
@pytest.mark.parametrize("embed", [broken, lambda q: [0.1] * 3, lambda q: [math.nan] * 384],
                         ids=["raises", "wrong_dimension", "nan"])
async def test_a_failing_embedder_falls_back_to_keyword_only(tmp_path, session_factory, caplog, embed):
    source = dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))
    await index_file(source, session_factory)

    with caplog.at_level(logging.WARNING):
        response = await search(session_factory, [source], "jwtAuthFilter", embed=embed, top_k=5)

    assert 159 in hit_lines(response) and all(hit.matched_by == ["keyword"] for hit in response.results)
    assert "jwtAuthFilter" not in caplog.text and "keyword-only" in caplog.text


@pytest.mark.integration
async def test_no_vector_and_no_lexeme_finds_nothing(tmp_path, session_factory):
    source = dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))
    await index_file(source, session_factory)

    response = await search(session_factory, [source], "?? the a of", embed=broken)

    assert response.results == [] and [c.status for c in response.coverage] == ["complete"]


@pytest.mark.integration
async def test_a_repeated_source_id_is_searched_and_reported_once(tmp_path, session_factory):
    source = dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))

    response = await search(session_factory, [source], "x", source_ids=[DEV_AUTH_ID, DEV_AUTH_ID])

    assert [c.source_id for c in response.coverage] == [DEV_AUTH_ID]


# ---- coverage edge cases (integration) ----

@pytest.mark.integration
async def test_a_file_deleted_before_indexing_notices_is_stale(tmp_path, session_factory):
    source = dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))
    await index_file(source, session_factory)
    source.path.unlink()

    assert (await coverage(session_factory, source)).status == "stale"


@pytest.mark.integration
async def test_the_sources_endpoint_reports_a_recorded_error(tmp_path, session_factory):
    source = FileSource(id="broken", path=tmp_path / "broken.log")
    async with session_factory() as s, s.begin():
        await record_error(s, "broken", "PermissionError")

    async with api_client(session_factory, [source]) as http:
        body = (await http.get("/api/v1/sources")).json()

    assert body == [{"id": "broken", "type": "file", "format": "text", "status": "error", "indexed_through": None,
                     "last_error": "PermissionError"}]

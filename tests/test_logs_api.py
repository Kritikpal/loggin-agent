"""Reading logs back: a filtered, paged list, one event, a histogram and filter counts. Open: no keys."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models import LogEvent
from app.models.project import ProjectRow
from tests.project_helpers import db_client

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
BASE = "/api/v1/projects/shop/logs"


def event(n: int, *, level="INFO", project="shop", text=None, signature="sig-a", minutes=None, **kw) -> LogEvent:
    return LogEvent(
        source_id=f"{project}/push", event_key=f"k{project}{n}", locator={"line": n},
        occurred_at=T0 + timedelta(minutes=n if minutes is None else minutes), level=level,
        raw_text=text or f"line {n}", fields={"n": n}, semantic_text=text or f"line {n}",
        signature=signature, signature_template=f"template {signature}", **kw)


@pytest.fixture
async def seeded(session_factory):
    async with session_factory() as s:
        for pid in ("shop", "other"):
            s.add(ProjectRow(id=pid, title=pid, type="wso2"))
        s.add_all([event(i, level="ERROR" if i % 5 == 0 else "INFO", signature="sig-e" if i % 5 == 0 else "sig-a")
                   for i in range(12)])
        s.add(event(0, project="other", text="belongs to another project"))
        await s.commit()
    return session_factory


async def get(factory, path="", **params):
    async with db_client(factory) as http:
        return await http.get(f"{BASE}{path}", params=params)


async def test_lists_newest_first_and_only_this_projects_events(seeded):
    body = (await get(seeded, limit=5)).json()
    texts = [e["text"] for e in body["items"]]
    assert texts == [f"line {i}" for i in (11, 10, 9, 8, 7)]
    assert body["next_cursor"]
    assert all(e["source"] == "push" for e in body["items"])


async def test_cursor_pages_through_everything_without_repeats(seeded):
    seen, cursor = [], None
    for _ in range(10):
        params = {"limit": 5, **({"cursor": cursor} if cursor else {})}
        body = (await get(seeded, **params)).json()
        seen += [e["text"] for e in body["items"]]
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert seen == [f"line {i}" for i in range(11, -1, -1)]


async def test_sort_ascending(seeded):
    body = (await get(seeded, sort="asc", limit=3)).json()
    assert [e["text"] for e in body["items"]] == ["line 0", "line 1", "line 2"]


async def test_filters_level_time_keyword_and_signature(seeded):
    errors = (await get(seeded, level="error")).json()["items"]
    assert [e["level"] for e in errors] == ["ERROR"] * 3
    window = (await get(seeded, **{"from": (T0 + timedelta(minutes=3)).isoformat(), "to": (T0 + timedelta(minutes=6)).isoformat()})).json()
    assert [e["text"] for e in window["items"]] == ["line 5", "line 4", "line 3"]  # `to` is exclusive
    hit = (await get(seeded, q="line 7")).json()["items"]
    assert [e["text"] for e in hit] == ["line 7"]
    sig = (await get(seeded, signature="sig-e")).json()["items"]
    assert len(sig) == 3


async def test_keyword_is_a_case_insensitive_substring_not_a_wildcard(session_factory):
    async with session_factory() as s:
        s.add(ProjectRow(id="shop", title="shop", type="wso2"))
        s.add_all([event(1, text="java.net.SocketTimeoutException: read timed out"), event(2, text="100% done"), event(3, text="plain")])
        await s.commit()
    assert len((await get(session_factory, q="sockettimeoutexception")).json()["items"]) == 1
    assert len((await get(session_factory, q="timeoutex")).json()["items"]) == 1
    assert [e["text"] for e in (await get(session_factory, q="%")).json()["items"]] == ["100% done"]  # % is not a wildcard
    assert (await get(session_factory, q="absent")).json()["items"] == []


async def test_list_items_carry_the_audit_actor_and_action(session_factory):
    async with session_factory() as s:
        s.add(ProjectRow(id="shop", title="shop", type="wso2"))
        audit = event(1)
        audit.fields = {"audit_actor": "alice", "audit_action": "Login"}
        s.add_all([audit, event(2)])
        await s.commit()
    items = {e["text"]: e for e in (await get(session_factory)).json()["items"]}
    assert (items["line 1"]["actor"], items["line 1"]["action"]) == ("alice", "Login")
    assert items["line 2"]["actor"] is None and items["line 2"]["action"] is None


async def test_a_long_text_is_cut_in_the_list_but_whole_in_the_detail(session_factory):
    async with session_factory() as s:
        s.add(ProjectRow(id="shop", title="shop", type="wso2"))
        s.add(event(1, text="x" * 5000))
        await s.commit()
    item = (await get(session_factory)).json()["items"][0]
    assert len(item["text"]) == 2000 and item["truncated"] is True
    full = (await get(session_factory, f"/{item['id']}")).json()
    assert len(full["raw_text"]) == 5000 and full["locator"] == {"line": 1} and full["fields"] == {"n": 1}


async def test_detail_is_404_for_another_projects_event_or_a_bad_id(seeded):
    async with seeded() as s:
        other = (await s.execute(LogEvent.__table__.select().where(LogEvent.source_id == "other/push"))).first()
    assert (await get(seeded, f"/{other.id}")).status_code == 404
    assert (await get(seeded, f"/{uuid.uuid4()}")).status_code == 404
    assert (await get(seeded, "/not-a-uuid")).status_code == 422


async def test_unknown_project_and_bad_parameters(seeded):
    async with db_client(seeded) as http:
        assert (await http.get("/api/v1/projects/nope/logs")).status_code == 404
    assert (await get(seeded, limit=201)).status_code == 422
    assert (await get(seeded, cursor="garbage")).status_code == 422
    assert (await get(seeded, **{"from": T0.isoformat(), "to": T0.isoformat()})).status_code == 422


async def test_events_without_a_time_are_outside_every_window(session_factory):
    async with session_factory() as s:
        s.add(ProjectRow(id="shop", title="shop", type="wso2"))
        untimed = event(3)
        untimed.occurred_at = None
        s.add_all([event(1), event(2), untimed])
        await s.commit()
    body = (await get(session_factory)).json()
    assert [e["text"] for e in body["items"]] == ["line 2", "line 1"]


async def test_stats_counts_per_bucket_and_level(seeded):
    frm, to = T0.isoformat(), (T0 + timedelta(minutes=12)).isoformat()
    body = (await get(seeded, "/stats", **{"from": frm, "to": to, "interval": "5m"})).json()
    buckets = {b["start"]: b for b in body["buckets"]}
    assert sum(b["total"] for b in buckets.values()) == 12
    first = buckets[min(buckets)]
    assert first["total"] == 5 and first["by_level"] == {"ERROR": 1, "INFO": 4}
    assert body["interval"] == "5m"


async def test_stats_refuses_too_many_buckets_and_unknown_intervals(seeded):
    too_many = await get(seeded, "/stats", **{"from": T0.isoformat(), "to": (T0 + timedelta(days=30)).isoformat(), "interval": "1m"})
    assert too_many.status_code == 422
    bad = await get(seeded, "/stats", **{"from": T0.isoformat(), "to": (T0 + timedelta(hours=1)).isoformat(), "interval": "7m"})
    assert bad.status_code == 422


async def test_without_a_range_every_endpoint_uses_the_last_24_hours_of_what_is_indexed(session_factory):
    async with session_factory() as s:
        s.add(ProjectRow(id="shop", title="shop", type="wso2"))
        s.add_all([event(1, minutes=0), event(2, minutes=60 * 30)])  # one event 30 h before the newest, one newest
        await s.commit()
    assert [e["text"] for e in (await get(session_factory)).json()["items"]] == ["line 2"]
    stats = (await get(session_factory, "/stats")).json()
    assert sum(b["total"] for b in stats["buckets"]) == 1
    assert [f["count"] for f in (await get(session_factory, "/facets")).json()["levels"]] == [1]


@pytest.mark.parametrize("path", ["", "/stats", "/facets"])
async def test_a_window_over_31_days_is_refused(seeded, path):
    res = await get(seeded, path, **{"from": T0.isoformat(), "to": (T0 + timedelta(days=32)).isoformat()})
    assert res.status_code == 422 and "31" in res.json()["error"]["message"]


async def test_stats_and_facets_read_at_most_the_cap_and_say_so(seeded, monkeypatch):
    monkeypatch.setattr("app.controllers.logs.SCAN_CAP", 5)
    stats = (await get(seeded, "/stats")).json()
    assert sum(b["total"] for b in stats["buckets"]) == 5 and stats["sampled"] is True
    facets = (await get(seeded, "/facets")).json()
    assert sum(f["count"] for f in facets["levels"]) == 5 and facets["sampled"] is True
    monkeypatch.setattr("app.controllers.logs.SCAN_CAP", 12)
    assert (await get(seeded, "/stats")).json()["sampled"] is False  # exactly the cap is not over it


async def test_a_slow_statement_becomes_a_narrow_it_error(session, monkeypatch):
    from app.controllers.logs import _run
    from app.core.errors import QueryTimeoutError
    from sqlalchemy import text

    await session.execute(text("SET LOCAL statement_timeout = 50"))
    with pytest.raises(QueryTimeoutError):
        await _run(session, text("SELECT pg_sleep(2)"))


async def test_a_page_is_an_index_scan_with_no_sort(session_factory):
    """The list never sorts the project: it walks ix_log_events_source_time_id, forward or backward, from the cursor."""
    from sqlalchemy import text
    from sqlalchemy.dialects import postgresql

    from app.controllers.logs import Filters, list_statement
    from app.ingestion.projects import project_from_row
    from app.models.project import ProjectRow as Row

    async with session_factory() as s:
        s.add(Row(id="shop", title="shop", type="wso2"))
        s.add_all([event(i) for i in range(50)])
        await s.commit()
        project = project_from_row(await s.get(Row, "shop"))
        filters = Filters(project, T0, T0 + timedelta(days=1), None, None, None)
        await s.execute(text("ANALYZE log_events"))
        await s.execute(text("SET enable_seqscan = off"))
        for ascending in (False, True):
            stmt = list_statement(filters, ascending=ascending, limit=10).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
            plan = " | ".join(r[0] for r in await s.execute(text("EXPLAIN " + str(stmt))))
            assert "Index Scan" in plan and "ix_log_events_source_time_id" in plan and "Sort" not in plan, plan


async def test_facets_count_levels_and_signatures_under_the_same_filters(seeded):
    body = (await get(seeded, "/facets")).json()
    assert {f["value"]: f["count"] for f in body["levels"]} == {"INFO": 9, "ERROR": 3}
    top = body["signatures"][0]
    assert top == {"value": "sig-a", "template": "template sig-a", "count": 9}
    filtered = (await get(seeded, "/facets", level="ERROR")).json()
    assert [f["value"] for f in filtered["levels"]] == ["ERROR"]

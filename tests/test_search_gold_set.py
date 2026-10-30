"""The labelled questions for search_logs and the ground truth they rest on. Nothing here needs the tool, so these
tests pass now: they prove the gold set and the shared helpers are right before the tool exists."""

import hashlib
from collections import Counter

import pytest
from sqlalchemy import select

from app.models import LogEvent
from tests.search_helpers import (
    DEV_AUTH_EVENTS, DEV_AUTH_ID, DEV_AUTH_LOG, ERROR_LINES, KINDS, WARN_LINES, copy_fixture, dev_auth_source,
    event_start_lines, fake_embed_documents, fake_vector, far, index_file, load_gold_set, near,
)


def test_the_gold_set_is_valid():
    gold = load_gold_set()
    starts = event_start_lines()
    kinds = Counter(q.kind for q in gold)
    bad_lines = {q.id: [n for n in q.expect_lines if n not in starts] for q in gold}

    assert len(gold) >= 30 and len({q.id for q in gold}) == len(gold)
    assert set(kinds) <= set(KINDS) and all(kinds[kind] >= 5 for kind in KINDS), dict(kinds)
    assert all(q.query.strip() and q.expect_lines and q.note for q in gold)
    assert {k: v for k, v in bad_lines.items() if v} == {}
    assert len(starts) == DEV_AUTH_EVENTS


def test_the_fake_embedder_gives_deterministic_unit_vectors():
    a, b = fake_vector("why did startup fail"), fake_vector("why did startup fail")
    dot = lambda x, y: sum(p * q for p, q in zip(x, y))  # noqa: E731

    assert a == b and len(a) == 384 and dot(a, a) == pytest.approx(1.0)
    assert abs(dot(a, fake_vector("something else"))) < 0.3
    assert dot(a, near(a, 1)) > 0.95 and dot(a, far(a, 1)) < -0.95
    assert fake_embed_documents(["x", "y"]) == [fake_vector("x"), fake_vector("y")]


@pytest.mark.integration
async def test_the_indexed_fixture_matches_the_ground_truth(tmp_path, session_factory):
    log = copy_fixture(DEV_AUTH_LOG, tmp_path)
    report = await index_file(dev_auth_source(log), session_factory)

    async with session_factory() as s:
        rows = list((await s.execute(select(LogEvent).where(LogEvent.source_id == DEV_AUTH_ID))).scalars())
    by_line = {r.locator["line_start"]: r for r in rows}
    data = log.read_bytes()
    reproduced = [
        data[r.locator["offset_start"]:r.locator["offset_end"]].decode().replace("\r\n", "\n").rstrip("\n")
        == r.raw_text.replace("\r\n", "\n").rstrip("\n")
        for r in rows
    ]
    gold_lines = {n for q in load_gold_set() for n in q.expect_lines}

    assert (report.status, report.events_stored, report.embedded) == ("ok", DEV_AUTH_EVENTS, DEV_AUTH_EVENTS)
    assert set(by_line) == event_start_lines() and gold_lines <= set(by_line)
    assert {n for n, r in by_line.items() if r.level == "ERROR"} == ERROR_LINES
    assert {n for n, r in by_line.items() if r.level == "WARN"} == WARN_LINES
    hashes_ok = [
        hashlib.sha256(data[r.locator["offset_start"]:r.locator["offset_end"]]).hexdigest() == r.locator["sha256"]
        for r in rows
    ]
    lines = log.read_text(encoding="utf-8").splitlines()
    first_lines_ok = [lines[n - 1] == r.raw_text.replace("\r\n", "\n").split("\n")[0] for n, r in by_line.items()]
    assert all(reproduced) and all(hashes_ok) and all(first_lines_ok)
    assert all(r.occurred_at is not None for r in rows) and all(log.samefile(r.locator["path"]) for r in rows)
    assert "jwtAuthFilter" in by_line[159].semantic_text and "HHH000342" in by_line[18].semantic_text

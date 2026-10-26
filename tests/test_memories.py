import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Memory
from app.repositories.memory import MemoryRepository

START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def t(i: int) -> datetime:
    return START + timedelta(seconds=i)


def pref(key: str, value: str, *, status: str = "active", at: int = 0, content: str | None = None) -> Memory:
    text_ = content or f"Wants {key} to be {value}."
    return Memory(
        kind="preference", key=key, content=text_, attributes={"value": value}, evidence=value, status=status,
        content_hash=hashlib.md5(text_.lower().encode()).hexdigest(), created_at=t(at),
    )


def obs(subject: str, content: str, *, status: str = "active", at: int = 0) -> Memory:
    return Memory(
        kind="observation", subject=subject, content=content, evidence=content, status=status,
        content_hash=hashlib.md5(content.lower().encode()).hexdigest(), created_at=t(at),
    )


async def commit_all(session, *memories: Memory) -> None:
    MemoryRepository(session).add(*memories)
    await session.commit()


async def test_active_preferences_are_active_only_and_ordered_by_key(session):
    await commit_all(
        session,
        pref("report_week", "Monday to Sunday"),
        pref("default_discount", "15%"),
        pref("old_tone", "formal", status="superseded"),
        obs("Jeans", "Jeans slow down in monsoon."),
    )

    assert [m.key for m in await MemoryRepository(session).active_preferences()] == ["default_discount", "report_week"]


async def test_database_allows_one_active_preference_per_key(session):
    await commit_all(session, pref("report_week", "Monday to Sunday"))

    session.add(pref("report_week", "Sunday to Saturday"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_supersede_then_insert_in_one_transaction(session):
    repo = MemoryRepository(session)
    old = pref("report_week", "Monday to Sunday")
    await commit_all(session, old)

    await repo.supersede(old.id)
    new = pref("report_week", "Sunday to Saturday", at=1)
    new.replaces_id = old.id
    repo.add(new)
    await session.commit()

    assert [m.attributes["value"] for m in await repo.active_preferences()] == ["Sunday to Saturday"]
    assert (await repo.get(old.id)).status == "superseded"


async def test_database_rejects_a_second_live_row_with_the_same_hash(session):
    await commit_all(session, obs("Jeans", "Jeans slow down in monsoon."))

    session.add(obs("Jeans", "Jeans slow down in monsoon.", status="conflicted"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_hash_may_repeat_once_the_earlier_row_is_not_live(session):
    await commit_all(session, obs("Jeans", "Jeans slow down in monsoon.", status="superseded"))

    await commit_all(session, obs("Jeans", "Jeans slow down in monsoon."))


async def test_database_ties_key_to_preferences(session):
    bad_observation = obs("Jeans", "Jeans slow down in monsoon.")
    bad_observation.key = "oops"
    session.add(bad_observation)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()

    keyless = pref("x", "y")
    keyless.key = None
    session.add(keyless)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_database_rejects_unknown_kind_and_status(session):
    for bad in (Memory(kind="episode", content="x", evidence="x", content_hash="a" * 32),
                obs("Jeans", "something", status="archived")):
        session.add(bad)
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


async def test_find_duplicate_only_matches_live_rows(session):
    live = obs("Jeans", "Jeans slow down in monsoon.")
    gone = obs("Shirts", "Shirts sell well in July.", status="deleted")
    await commit_all(session, live, gone)
    repo = MemoryRepository(session)

    assert (await repo.find_duplicate(live.content_hash)).id == live.id
    assert await repo.find_duplicate(gone.content_hash) is None
    assert await repo.find_duplicate("0" * 32) is None


async def test_has_similar_compares_live_rows_of_the_same_kind(session):
    await commit_all(
        session,
        obs("Jeans", "Jeans sales slow down in monsoon, June to September."),
        obs("Shirts", "Shirts sell well before the wedding season.", status="superseded"),
    )
    repo = MemoryRepository(session)

    assert await repo.has_similar("Jeans sales slow down in the monsoon, June to September.", "observation", 0.8)
    assert not await repo.has_similar("Suppliers deliver late on Fridays.", "observation", 0.8)
    assert not await repo.has_similar("Jeans sales slow down in monsoon, June to September.", "preference", 0.8)
    assert not await repo.has_similar("Shirts sell well before the wedding season.", "observation", 0.8)  # superseded


async def test_related_observations_rank_by_relevance_and_skip_everything_else(session):
    await commit_all(
        session,
        obs("Jeans", "Jeans sales slow down in monsoon, June to September.", at=0),
        obs("Shirts", "Shirts sell well in July.", at=1),
        obs("Jeans", "Jeans demand peaks in July before the wedding season.", at=2),
        obs("suppliers", "Suppliers deliver late on Fridays.", at=3),
        obs("Jeans", "Jeans were popular with students in the past.", status="superseded", at=4),
        pref("report_week", "Monday to Sunday", content="Wants jeans reports in July style."),
    )

    found = await MemoryRepository(session).related_observations("jeans or july", limit=5)

    assert found[0].content.startswith("Jeans demand peaks")  # matches both terms
    assert {m.content[:6] for m in found} == {"Jeans ", "Shirts"}
    assert len(found) == 3  # no supplier note, no superseded row, no preference


async def test_related_observations_returns_nothing_for_stop_words_and_respects_the_limit(session):
    await commit_all(session, *(obs("Jeans", f"Jeans note number {i}.", at=i) for i in range(8)))
    repo = MemoryRepository(session)

    assert await repo.related_observations("the and of") == []
    assert len(await repo.related_observations("jeans", limit=5)) == 5


async def test_list_defaults_to_active_and_conflicted_newest_first(session):
    await commit_all(
        session,
        obs("Jeans", "first", at=0),
        obs("Jeans", "second", status="conflicted", at=1),
        obs("Jeans", "third", status="superseded", at=2),
        obs("Jeans", "fourth", status="deleted", at=3),
        pref("report_week", "Monday to Sunday", at=4),
    )
    repo = MemoryRepository(session)

    assert [m.content for m in await repo.list()][-2:] == ["second", "first"]
    assert len(await repo.list()) == 3
    assert [m.content for m in await repo.list(kind="observation", statuses=("deleted",))] == ["fourth"]


async def test_get_returns_none_for_an_unknown_id(session):
    assert await MemoryRepository(session).get(uuid.uuid4()) is None

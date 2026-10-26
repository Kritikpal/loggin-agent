import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import ChatMessage
from app.repositories.chat_message import ChatMessageRepository

START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def t(i: int) -> datetime:
    return START + timedelta(seconds=i)


async def test_recent_returns_the_newest_n_oldest_first(session):
    conv = uuid.uuid4()
    repo = ChatMessageRepository(session)
    repo.add(*(ChatMessage(conversation_id=conv, role="user", content=f"m{i}", created_at=t(i)) for i in range(12)))
    repo.add(ChatMessage(conversation_id=uuid.uuid4(), role="user", content="other", created_at=t(99)))
    await session.commit()

    assert [m.content for m in await repo.recent(conv, 10)] == [f"m{i}" for i in range(2, 12)]


async def test_user_sorts_before_assistant_at_the_same_instant(session):
    conv = uuid.uuid4()
    repo = ChatMessageRepository(session)
    repo.add(  # added assistant-first on purpose: the order must come from the query, not insertion
        ChatMessage(conversation_id=conv, role="assistant", content="a", created_at=t(0)),
        ChatMessage(conversation_id=conv, role="user", content="q", created_at=t(0)),
    )
    await session.commit()

    assert [m.role for m in await repo.recent(conv, 10)] == ["user", "assistant"]


async def test_latest_thread_id_skips_messages_without_one(session):
    conv, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    repo = ChatMessageRepository(session)
    repo.add(
        ChatMessage(conversation_id=conv, role="assistant", content="1", thread_id=a, created_at=t(0)),
        ChatMessage(conversation_id=conv, role="assistant", content="2", thread_id=b, created_at=t(1)),
        ChatMessage(conversation_id=conv, role="user", content="3", created_at=t(2)),
    )
    await session.commit()

    assert await repo.latest_thread_id(conv) == b
    assert await repo.latest_thread_id(uuid.uuid4()) is None


async def test_conversation_for_thread(session):
    conv, a = uuid.uuid4(), uuid.uuid4()
    repo = ChatMessageRepository(session)
    repo.add(ChatMessage(conversation_id=conv, role="assistant", content="x", thread_id=a))
    await session.commit()

    assert await repo.conversation_for_thread(a) == conv
    assert await repo.conversation_for_thread(uuid.uuid4()) is None


async def test_database_rejects_unknown_roles(session):
    session.add(ChatMessage(conversation_id=uuid.uuid4(), role="system", content="x"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()

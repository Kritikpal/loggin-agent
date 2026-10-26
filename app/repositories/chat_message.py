import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatMessage


class ChatMessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def recent(self, conversation_id: uuid.UUID, limit: int) -> list[ChatMessage]:
        """The newest `limit` messages, oldest first. Both messages of a turn may share a timestamp; the
        user's then sorts first ('assistant' < 'user', so it comes first in the newest-first query)."""
        rows = await self.session.scalars(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.created_at.desc(), ChatMessage.role.asc())
            .limit(limit)
        )
        return list(rows)[::-1]

    async def latest_thread_id(self, conversation_id: uuid.UUID) -> uuid.UUID | None:
        """The most recently *proposed* workflow, i.e. ranked by its first message. Later messages about an older
        workflow (a button decision on it) must not make it "the newest proposal" again."""
        return await self.session.scalar(
            select(ChatMessage.thread_id)
            .where(ChatMessage.conversation_id == conversation_id, ChatMessage.thread_id.is_not(None))
            .group_by(ChatMessage.thread_id)
            .order_by(func.min(ChatMessage.created_at).desc())
            .limit(1)
        )

    async def conversation_for_thread(self, thread_id: uuid.UUID) -> uuid.UUID | None:
        return await self.session.scalar(
            select(ChatMessage.conversation_id).where(ChatMessage.thread_id == thread_id).limit(1)
        )

    def add(self, *messages: ChatMessage) -> None:
        self.session.add_all(messages)  # the caller commits, so a turn's messages land together

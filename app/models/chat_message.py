import uuid

from sqlalchemy import CheckConstraint, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt, UuidPk


class ChatMessage(Base):
    """One message of an AI chat conversation. There is no conversations table: a conversation is its messages."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="role_allowed"),
        Index("ix_chat_messages_conversation_id_created_at", "conversation_id", "created_at"),
    )

    id: Mapped[UuidPk]
    conversation_id: Mapped[uuid.UUID]
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    thread_id: Mapped[uuid.UUID | None] = mapped_column(index=True)  # workflow behind a proposal or decision
    created_at: Mapped[CreatedAt]

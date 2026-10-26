import uuid
from typing import Any

from sqlalchemy import CheckConstraint, Computed, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt, UuidPk


class Memory(Base):
    """A fact the manager told the assistant, kept across conversations. Append-only: an update supersedes the old
    row instead of editing it. See docs/superpowers/specs/2026-10-26-semantic-memory-design.md."""

    __tablename__ = "memories"
    __table_args__ = (
        CheckConstraint("kind IN ('preference', 'observation')", name="kind_allowed"),
        CheckConstraint("status IN ('active', 'conflicted', 'superseded', 'deleted')", name="status_allowed"),
        CheckConstraint("(kind = 'preference') = (key IS NOT NULL)", name="key_iff_preference"),
        # One active preference per key, and no exact duplicate among live rows - enforced by the database.
        Index("uq_memories_active_key", "key", unique=True, postgresql_where=text("status = 'active'")),
        Index(
            "uq_memories_live_hash", "content_hash", unique=True,
            postgresql_where=text("status IN ('active', 'conflicted')"),
        ),
        Index("ix_memories_search", "search", postgresql_using="gin"),
        Index("ix_memories_content_trgm", "content", postgresql_using="gin", postgresql_ops={"content": "gin_trgm_ops"}),
        Index("ix_memories_kind_status", "kind", "status"),
    )

    id: Mapped[UuidPk]
    kind: Mapped[str] = mapped_column(String(20))
    key: Mapped[str | None] = mapped_column(String(60))  # preferences only
    subject: Mapped[str | None] = mapped_column(String(60))  # observations only
    content: Mapped[str] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    evidence: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    replaces_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("memories.id"))
    conflicts_with_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("memories.id"))
    source_conversation_id: Mapped[uuid.UUID | None]  # chat_messages.conversation_id; no FK, no conversations table
    content_hash: Mapped[str] = mapped_column(String(32))  # md5 of normalised content
    # Computed by PostgreSQL; deferred so the async session never lazy-loads it. Only queried, never read.
    search: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(subject, '') || ' ' || content)", persisted=True),
        deferred=True,
    )
    created_at: Mapped[CreatedAt]

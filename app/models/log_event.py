from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, DateTime, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.embeddings import EMBEDDING_DIMENSIONS
from app.models.base import Base, CreatedAt, UuidPk



class LogEvent(Base):
    """One assembled, parsed and redacted log event. The original log stays the source of truth."""

    __tablename__ = "log_events"
    __table_args__ = (
        UniqueConstraint("source_id", "event_key", name="uq_log_events_source_event"),
        Index("ix_log_events_source_time", "source_id", "occurred_at"),
        Index("ix_log_events_level_time", "level", "occurred_at"),
        Index("ix_log_events_signature_time", "signature", "occurred_at"),
        Index("ix_log_events_search_tsv", "search_tsv", postgresql_using="gin"),
        Index(
            "ix_log_events_embedding", "embedding", postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[UuidPk]
    source_id: Mapped[str] = mapped_column(Text)
    event_key: Mapped[str] = mapped_column(Text, comment="Stable id within the source (e.g. file+offset); makes re-indexing idempotent")
    locator: Mapped[dict[str, Any]] = mapped_column(JSONB, comment="Where the original lives: file path/lines or container/line")
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    level: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str] = mapped_column(Text, comment="Redacted original event text")
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"), comment="Redacted dynamic fields with provenance")
    semantic_text: Mapped[str] = mapped_column(Text, comment="Text that was embedded and keyword-indexed")
    signature: Mapped[str | None] = mapped_column(Text, comment="Id of the event's kind (app.ingestion.signature)")
    signature_template: Mapped[str | None] = mapped_column(Text, comment="The kind's text with variable parts as <*>")
    search_tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', semantic_text)", persisted=True)
    )
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))  # null = kept, not yet embedded
    embedding_model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[CreatedAt]


class SourceIndexState(Base):
    """How far each source has been indexed, so search can report stale or partial coverage (AC7)."""

    __tablename__ = "source_index_state"

    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    indexed_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor: Mapped[str | None] = mapped_column(Text, comment="Resume point, e.g. file offset or Docker 'since'")
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[CreatedAt]

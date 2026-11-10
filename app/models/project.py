from typing import Any

from sqlalchemy import Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt


class ProjectRow(Base):
    """A project's settings. Its logs are `log_events` rows whose source_id starts with '<id>/'."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="The slug; also the prefix of its source ids")
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    type: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, default="UTC", server_default="UTC")
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"),
        comment="Optional event_start, app_packages and field_mappings")
    push_token_hash: Mapped[str] = mapped_column(Text, comment="SHA-256 of the bearer token; the token is shown once")
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[CreatedAt]

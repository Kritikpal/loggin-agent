from decimal import Decimal

from sqlalchemy import CheckConstraint, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt, UuidPk

CAMPAIGN_STATUSES = ("draft", "approved", "active", "completed", "cancelled")


class Campaign(Base):
    """A promotion definition. Phase 1 only ever persists drafts; nothing is sent or applied."""

    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint("discount_percent >= 0 AND discount_percent <= 100", name="discount_percent_range"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in CAMPAIGN_STATUSES) + ")", name="status_allowed"
        ),
    )

    id: Mapped[UuidPk]
    name: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    discount_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    status: Mapped[str] = mapped_column(String(30), default="draft", server_default="draft", index=True)
    created_at: Mapped[CreatedAt]

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UuidPk, utcnow


class Inventory(Base):
    __tablename__ = "inventory"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint("reorder_level >= 0", name="reorder_level_non_negative"),
    )

    id: Mapped[UuidPk]
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), unique=True)
    quantity: Mapped[int]
    reorder_level: Mapped[int]
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now()
    )

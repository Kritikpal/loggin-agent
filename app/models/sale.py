import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Money, UuidPk, utcnow


class Sale(Base):
    """A single-product sale (learning MVP: no order/line-item split)."""

    __tablename__ = "sales"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint("total_amount = quantity * unit_price", name="total_matches_quantity_price"),
        Index("ix_sales_sold_at", "sold_at"),
        Index("ix_sales_product_id_sold_at", "product_id", "sold_at"),
    )

    id: Mapped[UuidPk]
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    quantity: Mapped[int]
    unit_price: Mapped[Money]
    total_amount: Mapped[Money]
    sold_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt, Money, UuidPk


class Product(Base):
    """One clothing SKU (a specific item in a specific size and color)."""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price > 0", name="price_positive"),
        CheckConstraint("cost_price >= 0", name="cost_price_non_negative"),
    )

    id: Mapped[UuidPk]
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(100), index=True)
    size: Mapped[str] = mapped_column(String(20))
    color: Mapped[str] = mapped_column(String(50))
    sku: Mapped[str] = mapped_column(String(100), unique=True)
    price: Mapped[Money]
    cost_price: Mapped[Money]
    created_at: Mapped[CreatedAt]

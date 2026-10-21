import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.models import Product, Sale


class SaleCreate(BaseModel):
    """Unit price is taken from the product at the time of sale, never from the client."""

    model_config = ConfigDict(extra="forbid")

    product_id: uuid.UUID
    quantity: int = Field(gt=0, le=10_000)


class SaleRead(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID
    sku: str
    product_name: str
    category: str
    quantity: int
    unit_price: Decimal
    total_amount: Decimal
    sold_at: datetime

    @classmethod
    def build(cls, sale: Sale, product: Product) -> "SaleRead":
        return cls(
            id=sale.id,
            product_id=sale.product_id,
            sku=product.sku,
            product_name=product.name,
            category=product.category,
            quantity=sale.quantity,
            unit_price=sale.unit_price,
            total_amount=sale.total_amount,
            sold_at=sale.sold_at,
        )


class CategorySummary(BaseModel):
    category: str
    revenue: Decimal
    units: int
    sale_count: int


class SalesSummary(BaseModel):
    date_from: date | None
    date_to: date | None
    total_revenue: Decimal
    total_units: int
    sale_count: int
    average_sale_value: Decimal = Field(description="total_revenue / sale_count, rounded to 2 places")
    by_category: list[CategorySummary]


class Granularity(str, Enum):
    day = "day"
    week = "week"  # ISO weeks, starting Monday
    month = "month"


class TrendPoint(BaseModel):
    period: date = Field(description="First day of the day/week/month bucket (UTC)")
    revenue: Decimal
    units: int
    sale_count: int


class SalesTrend(BaseModel):
    granularity: Granularity
    date_from: date | None
    date_to: date | None
    points: list[TrendPoint] = Field(description="Ascending by period; empty periods are included with zeros")

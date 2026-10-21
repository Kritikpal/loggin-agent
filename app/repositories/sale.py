from __future__ import annotations  # the `list` method below would otherwise shadow the builtin in annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import Date, Select, cast, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product, Sale
from app.repositories.common import count_rows
from app.schemas.sale import Granularity


@dataclass(frozen=True)
class SalesFilters:
    """date_from / date_to are inclusive UTC calendar days."""

    date_from: date | None = None
    date_to: date | None = None
    category: str | None = None
    product_id: uuid.UUID | None = None


@dataclass(frozen=True)
class SalesTotals:
    revenue: Decimal
    units: int
    sale_count: int


@dataclass(frozen=True)
class CategoryTotals(SalesTotals):
    category: str


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def _apply(stmt: Select, filters: SalesFilters) -> Select:
    """Expects `sales` joined to `products`."""
    if filters.date_from:
        stmt = stmt.where(Sale.sold_at >= _day_start(filters.date_from))
    if filters.date_to:
        stmt = stmt.where(Sale.sold_at < _day_start(filters.date_to + timedelta(days=1)))
    if filters.category:
        stmt = stmt.where(func.lower(Product.category) == filters.category.lower())
    if filters.product_id:
        stmt = stmt.where(Sale.product_id == filters.product_id)
    return stmt


_SUMS = (
    func.coalesce(func.sum(Sale.total_amount), Decimal(0)).label("revenue"),
    func.coalesce(func.sum(Sale.quantity), 0).label("units"),
    func.count(Sale.id).label("sale_count"),
)


class SaleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, sale: Sale) -> Sale:
        self.session.add(sale)
        await self.session.flush()
        return sale

    async def list(self, filters: SalesFilters, limit: int, offset: int) -> tuple[list[tuple[Sale, Product]], int]:
        base = _apply(select(Sale, Product).join(Product, Product.id == Sale.product_id), filters)
        total = await count_rows(self.session, base)
        rows = await self.session.execute(base.order_by(Sale.sold_at.desc(), Sale.id).limit(limit).offset(offset))
        return [(sale, product) for sale, product in rows.all()], total

    async def totals(self, filters: SalesFilters) -> SalesTotals:
        stmt = _apply(select(*_SUMS).select_from(Sale).join(Product, Product.id == Sale.product_id), filters)
        row = (await self.session.execute(stmt)).one()
        return SalesTotals(revenue=row.revenue, units=row.units, sale_count=row.sale_count)

    async def totals_by_category(self, filters: SalesFilters) -> list[CategoryTotals]:
        stmt = _apply(
            select(Product.category, *_SUMS).select_from(Sale).join(Product, Product.id == Sale.product_id), filters
        )
        stmt = stmt.group_by(Product.category).order_by(func.sum(Sale.total_amount).desc(), Product.category)
        rows = (await self.session.execute(stmt)).all()
        return [
            CategoryTotals(revenue=r.revenue, units=r.units, sale_count=r.sale_count, category=r.category)
            for r in rows
        ]

    async def totals_by_period(self, filters: SalesFilters, granularity: str) -> list[tuple[date, SalesTotals]]:
        """Bucket in PostgreSQL. `granularity` is a validated enum value, so inlining it is safe and keeps the
        SELECT / GROUP BY expressions textually identical."""
        granularity = Granularity(granularity).value  # raises unless day/week/month: nothing else is ever inlined
        period = cast(
            func.date_trunc(literal_column(f"'{granularity}'"), func.timezone(literal_column("'UTC'"), Sale.sold_at)),
            Date,
        ).label("period")
        stmt = _apply(select(period, *_SUMS).select_from(Sale).join(Product, Product.id == Sale.product_id), filters)
        stmt = stmt.group_by(period).order_by(period)
        rows = (await self.session.execute(stmt)).all()
        return [(r.period, SalesTotals(revenue=r.revenue, units=r.units, sale_count=r.sale_count)) for r in rows]

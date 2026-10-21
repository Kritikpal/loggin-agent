from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InsufficientStockError, InvalidRequestError, NotFoundError
from app.models import Sale
from app.repositories.inventory import InventoryRepository
from app.repositories.product import ProductRepository
from app.repositories.sale import SaleRepository, SalesFilters, SalesTotals
from app.schemas.common import Page, Pagination
from app.schemas.sale import (
    CategorySummary,
    Granularity,
    SaleCreate,
    SaleRead,
    SalesSummary,
    SalesTrend,
    TrendPoint,
)

CENT = Decimal("0.01")
MAX_TOTAL = Decimal("9999999999.99")  # NUMERIC(12,2) ceiling
MAX_TREND_POINTS = 1000


def period_start(day: date, granularity: Granularity) -> date:
    """Mirror of PostgreSQL date_trunc (weeks start on Monday)."""
    if granularity is Granularity.day:
        return day
    if granularity is Granularity.week:
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def next_period(start: date, granularity: Granularity) -> date:
    if granularity is Granularity.day:
        return start + timedelta(days=1)
    if granularity is Granularity.week:
        return start + timedelta(days=7)
    return date(start.year + (start.month == 12), start.month % 12 + 1, 1)


class SaleService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.sales = SaleRepository(session)
        self.products = ProductRepository(session)
        self.inventory = InventoryRepository(session)

    async def create(self, data: SaleCreate) -> SaleRead:
        """Record a sale and decrement stock in ONE transaction.

        The inventory row is locked (SELECT ... FOR UPDATE) before the stock check, so concurrent sales of the
        same product queue up and can never oversell. Any failure rolls back both the sale and the decrement.
        """
        product = await self.products.get(data.product_id)
        if product is None:
            raise NotFoundError(f"Product {data.product_id} not found")
        total = (product.price * data.quantity).quantize(CENT)
        if total > MAX_TOTAL:
            raise InvalidRequestError(
                f"Sale total {total} exceeds the maximum of {MAX_TOTAL}", details={"max_total": str(MAX_TOTAL)}
            )
        inventory = await self.inventory.get_for_update(data.product_id)
        if inventory is None:
            raise NotFoundError(f"No inventory record for product {data.product_id}")
        available = inventory.quantity
        if available < data.quantity:
            error = InsufficientStockError(
                f"Insufficient stock for {product.sku}: requested {data.quantity}, available {available}",
                details={"product_id": str(product.id), "requested": data.quantity, "available": available},
            )
            await self.session.rollback()  # release the row lock now (rollback expires ORM objects, so build first)
            raise error
        inventory.quantity = available - data.quantity
        sale = await self.sales.add(
            Sale(
                product_id=product.id,
                quantity=data.quantity,
                unit_price=product.price,
                total_amount=total,
            )
        )
        await self.session.commit()
        return SaleRead.build(sale, product)

    async def list(self, filters: SalesFilters, pagination: Pagination) -> Page[SaleRead]:
        rows, total = await self.sales.list(filters, pagination.page_size, pagination.offset)
        return Page.of([SaleRead.build(sale, product) for sale, product in rows], total, pagination)

    async def summary(self, filters: SalesFilters) -> SalesSummary:
        totals = await self.sales.totals(filters)
        by_category = await self.sales.totals_by_category(filters)
        average = (totals.revenue / totals.sale_count).quantize(CENT, ROUND_HALF_UP) if totals.sale_count else Decimal("0.00")
        return SalesSummary(
            date_from=filters.date_from,
            date_to=filters.date_to,
            total_revenue=totals.revenue,
            total_units=totals.units,
            sale_count=totals.sale_count,
            average_sale_value=average,
            by_category=[
                CategorySummary(category=c.category, revenue=c.revenue, units=c.units, sale_count=c.sale_count)
                for c in by_category
            ],
        )

    async def trends(self, filters: SalesFilters, granularity: Granularity) -> SalesTrend:
        buckets = dict(await self.sales.totals_by_period(filters, granularity.value))
        start = period_start(filters.date_from, granularity) if filters.date_from else min(buckets, default=None)
        end = period_start(filters.date_to, granularity) if filters.date_to else max(buckets, default=None)
        points: list[TrendPoint] = []
        empty = SalesTotals(revenue=Decimal("0.00"), units=0, sale_count=0)
        period = start
        while period is not None and end is not None and period <= end:  # fill gaps so a dead stretch is visible
            if len(points) >= MAX_TREND_POINTS:
                raise InvalidRequestError(
                    f"Range spans more than {MAX_TREND_POINTS} {granularity.value} buckets; narrow it or use a coarser granularity"
                )
            t = buckets.get(period, empty)
            points.append(TrendPoint(period=period, revenue=t.revenue, units=t.units, sale_count=t.sale_count))
            period = next_period(period, granularity)
        return SalesTrend(granularity=granularity, date_from=filters.date_from, date_to=filters.date_to, points=points)

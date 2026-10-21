import uuid
from dataclasses import dataclass

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product
from app.repositories.common import contains_pattern, count_rows


@dataclass(frozen=True)
class ProductFilters:
    category: str | None = None
    size: str | None = None
    color: str | None = None
    search: str | None = None


def apply_product_filters(stmt: Select, filters: ProductFilters) -> Select:
    """Shared by every query that joins products (products, inventory)."""
    if filters.category:
        stmt = stmt.where(func.lower(Product.category) == filters.category.lower())
    if filters.size:
        stmt = stmt.where(func.lower(Product.size) == filters.size.lower())
    if filters.color:
        stmt = stmt.where(func.lower(Product.color) == filters.color.lower())
    if filters.search:
        pattern = contains_pattern(filters.search)
        stmt = stmt.where(or_(Product.name.ilike(pattern, escape="\\"), Product.sku.ilike(pattern, escape="\\")))
    return stmt


class ProductRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self, filters: ProductFilters, limit: int, offset: int) -> tuple[list[Product], int]:
        stmt = apply_product_filters(select(Product), filters)
        total = await count_rows(self.session, stmt)
        rows = await self.session.scalars(stmt.order_by(Product.name, Product.sku).limit(limit).offset(offset))
        return list(rows), total

    async def get(self, product_id: uuid.UUID) -> Product | None:
        return await self.session.get(Product, product_id)

    async def get_by_sku(self, sku: str) -> Product | None:
        return await self.session.scalar(select(Product).where(Product.sku == sku))

    async def add(self, product: Product) -> Product:
        self.session.add(product)
        await self.session.flush()
        return product

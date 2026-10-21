import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Inventory, Product
from app.repositories.common import count_rows
from app.repositories.product import ProductFilters, apply_product_filters


class InventoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(
        self, filters: ProductFilters, limit: int, offset: int, *, low_stock_only: bool = False
    ) -> tuple[list[tuple[Inventory, Product]], int]:
        stmt = apply_product_filters(
            select(Inventory, Product).join(Product, Product.id == Inventory.product_id), filters
        )
        if low_stock_only:
            stmt = stmt.where(Inventory.quantity <= Inventory.reorder_level)
            order = (Inventory.quantity - Inventory.reorder_level, Product.sku)  # largest shortfall first
        else:
            order = (Product.name, Product.sku)
        total = await count_rows(self.session, stmt)
        rows = await self.session.execute(stmt.order_by(*order).limit(limit).offset(offset))
        return [(inv, prod) for inv, prod in rows.all()], total

    async def get_with_product(self, product_id: uuid.UUID) -> tuple[Inventory, Product] | None:
        row = (
            await self.session.execute(
                select(Inventory, Product)
                .join(Product, Product.id == Inventory.product_id)
                .where(Inventory.product_id == product_id)
            )
        ).first()
        return (row[0], row[1]) if row else None

    async def get_for_update(self, product_id: uuid.UUID) -> Inventory | None:
        """Lock the inventory row until the transaction ends so concurrent sales serialize per product."""
        return await self.session.scalar(
            select(Inventory)
            .where(Inventory.product_id == product_id)
            .with_for_update()
            .execution_options(populate_existing=True)  # never trust a copy cached before we got the lock
        )

    async def add(self, inventory: Inventory) -> Inventory:
        self.session.add(inventory)
        await self.session.flush()
        return inventory

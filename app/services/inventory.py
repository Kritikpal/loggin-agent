import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.repositories.inventory import InventoryRepository
from app.repositories.product import ProductFilters
from app.schemas.common import Page, Pagination
from app.schemas.inventory import InventoryRead, InventoryUpdate


class InventoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.inventory = InventoryRepository(session)

    async def list(
        self, filters: ProductFilters, pagination: Pagination, *, low_stock_only: bool = False
    ) -> Page[InventoryRead]:
        rows, total = await self.inventory.list(
            filters, pagination.page_size, pagination.offset, low_stock_only=low_stock_only
        )
        return Page.of([InventoryRead.build(inv, prod) for inv, prod in rows], total, pagination)

    async def get(self, product_id: uuid.UUID) -> InventoryRead:
        row = await self.inventory.get_with_product(product_id)
        if row is None:
            raise NotFoundError(f"No inventory record for product {product_id}")
        return InventoryRead.build(*row)

    async def update(self, product_id: uuid.UUID, data: InventoryUpdate) -> InventoryRead:
        inventory = await self.inventory.get_for_update(product_id)  # serializes with concurrent sales
        if inventory is None:
            raise NotFoundError(f"No inventory record for product {product_id}")
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(inventory, field, value)
        await self.session.commit()
        return await self.get(product_id)

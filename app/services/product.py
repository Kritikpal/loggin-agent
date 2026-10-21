import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models import Inventory, Product
from app.repositories.inventory import InventoryRepository
from app.repositories.product import ProductFilters, ProductRepository
from app.schemas.common import Page, Pagination
from app.schemas.product import ProductCreate, ProductRead, ProductUpdate


class ProductService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.products = ProductRepository(session)
        self.inventory = InventoryRepository(session)

    async def list(self, filters: ProductFilters, pagination: Pagination) -> Page[ProductRead]:
        rows, total = await self.products.list(filters, pagination.page_size, pagination.offset)
        return Page.of([ProductRead.model_validate(p) for p in rows], total, pagination)

    async def get(self, product_id: uuid.UUID) -> ProductRead:
        return ProductRead.model_validate(await self._get_or_404(product_id))

    async def create(self, data: ProductCreate) -> ProductRead:
        if await self.products.get_by_sku(data.sku):
            raise ConflictError(f"A product with SKU '{data.sku}' already exists")
        product = Product(**data.model_dump(exclude={"initial_quantity", "reorder_level"}))
        try:
            await self.products.add(product)
            await self.inventory.add(
                Inventory(product_id=product.id, quantity=data.initial_quantity, reorder_level=data.reorder_level)
            )
            await self.session.commit()
        except IntegrityError as exc:  # lost a race on the unique SKU
            await self.session.rollback()
            raise ConflictError(f"A product with SKU '{data.sku}' already exists") from exc
        return ProductRead.model_validate(product)

    async def update(self, product_id: uuid.UUID, data: ProductUpdate) -> ProductRead:
        product = await self._get_or_404(product_id)
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(product, field, value)
        await self.session.commit()
        return ProductRead.model_validate(product)

    async def _get_or_404(self, product_id: uuid.UUID) -> Product:
        product = await self.products.get(product_id)
        if product is None:
            raise NotFoundError(f"Product {product_id} not found")
        return product

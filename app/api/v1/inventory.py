import uuid

from fastapi import APIRouter

from app.api.deps import NOT_FOUND, ExactFilter, InventoryServiceDep, PaginationDep, SearchFilter
from app.repositories.product import ProductFilters
from app.schemas.common import Page
from app.schemas.inventory import InventoryRead, InventoryUpdate

router = APIRouter(prefix="/inventory", tags=["Inventory"])


@router.get("", response_model=Page[InventoryRead], summary="List inventory")
async def list_inventory(
    service: InventoryServiceDep,
    pagination: PaginationDep,
    category: ExactFilter = None,
    size: ExactFilter = None,
    color: ExactFilter = None,
    search: SearchFilter = None,
) -> Page[InventoryRead]:
    return await service.list(ProductFilters(category, size, color, search), pagination)


@router.get(
    "/low-stock",
    response_model=Page[InventoryRead],
    summary="List items at or below their reorder level (largest shortfall first)",
)
async def list_low_stock(
    service: InventoryServiceDep,
    pagination: PaginationDep,
    category: ExactFilter = None,
    size: ExactFilter = None,
    color: ExactFilter = None,
    search: SearchFilter = None,
) -> Page[InventoryRead]:
    return await service.list(ProductFilters(category, size, color, search), pagination, low_stock_only=True)


@router.get("/{product_id}", response_model=InventoryRead, responses=NOT_FOUND, summary="Get a product's stock")
async def get_inventory(product_id: uuid.UUID, service: InventoryServiceDep) -> InventoryRead:
    return await service.get(product_id)


@router.patch(
    "/{product_id}",
    response_model=InventoryRead,
    responses=NOT_FOUND,
    summary="Set a product's stock level and/or reorder level",
)
async def update_inventory(product_id: uuid.UUID, data: InventoryUpdate, service: InventoryServiceDep) -> InventoryRead:
    return await service.update(product_id, data)

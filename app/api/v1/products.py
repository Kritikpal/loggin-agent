import uuid

from fastapi import APIRouter, status

from app.api.deps import CONFLICT, NOT_FOUND, ExactFilter, PaginationDep, ProductServiceDep, SearchFilter
from app.repositories.product import ProductFilters
from app.schemas.common import Page
from app.schemas.product import ProductCreate, ProductRead, ProductUpdate

router = APIRouter(prefix="/products", tags=["Products"])


@router.get("", response_model=Page[ProductRead], summary="List products")
async def list_products(
    service: ProductServiceDep,
    pagination: PaginationDep,
    category: ExactFilter = None,
    size: ExactFilter = None,
    color: ExactFilter = None,
    search: SearchFilter = None,
) -> Page[ProductRead]:
    return await service.list(ProductFilters(category, size, color, search), pagination)


@router.get("/{product_id}", response_model=ProductRead, responses=NOT_FOUND, summary="Get a product")
async def get_product(product_id: uuid.UUID, service: ProductServiceDep) -> ProductRead:
    return await service.get(product_id)


@router.post(
    "",
    response_model=ProductRead,
    status_code=status.HTTP_201_CREATED,
    responses=CONFLICT,
    summary="Create a product (and its inventory row)",
)
async def create_product(data: ProductCreate, service: ProductServiceDep) -> ProductRead:
    return await service.create(data)


@router.patch("/{product_id}", response_model=ProductRead, responses=NOT_FOUND, summary="Update a product")
async def update_product(product_id: uuid.UUID, data: ProductUpdate, service: ProductServiceDep) -> ProductRead:
    return await service.update(product_id, data)

from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.schemas.common import MAX_PAGE, MAX_PAGE_SIZE, NO_NUL, ErrorResponse, Pagination
from app.services.campaign import CampaignService
from app.services.customer import CustomerService
from app.services.inventory import InventoryService
from app.services.product import ProductService
from app.services.sale import SaleService

# Bounded free-text filters (no NUL bytes, sane length).
ExactFilter = Annotated[
    str | None, Query(max_length=100, pattern=NO_NUL, description="Exact match, case-insensitive")
]
SearchFilter = Annotated[str | None, Query(max_length=100, pattern=NO_NUL, description="Substring search")]

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def pagination(
    page: Annotated[int, Query(ge=1, le=MAX_PAGE, description="1-based page number")] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> Pagination:
    return Pagination(page=page, page_size=page_size)


PaginationDep = Annotated[Pagination, Depends(pagination)]


def product_service(session: SessionDep) -> ProductService:
    return ProductService(session)


def inventory_service(session: SessionDep) -> InventoryService:
    return InventoryService(session)


def sale_service(session: SessionDep) -> SaleService:
    return SaleService(session)


def customer_service(session: SessionDep) -> CustomerService:
    return CustomerService(session)


def campaign_service(session: SessionDep) -> CampaignService:
    return CampaignService(session)


ProductServiceDep = Annotated[ProductService, Depends(product_service)]
InventoryServiceDep = Annotated[InventoryService, Depends(inventory_service)]
SaleServiceDep = Annotated[SaleService, Depends(sale_service)]
CustomerServiceDep = Annotated[CustomerService, Depends(customer_service)]
CampaignServiceDep = Annotated[CampaignService, Depends(campaign_service)]

# Error shapes documented in OpenAPI (the runtime envelope is built in app.core.errors).
NOT_FOUND = {404: {"model": ErrorResponse, "description": "Resource not found"}}
CONFLICT = {409: {"model": ErrorResponse, "description": "Conflicts with existing data"}}

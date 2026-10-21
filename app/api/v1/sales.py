import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import CONFLICT, NOT_FOUND, ExactFilter, PaginationDep, SaleServiceDep
from app.core.errors import InvalidRequestError
from app.repositories.sale import SalesFilters
from app.schemas.common import Page
from app.schemas.sale import Granularity, SaleCreate, SaleRead, SalesSummary, SalesTrend

router = APIRouter(prefix="/sales", tags=["Sales"])

MIN_DATE, MAX_DATE = date(2000, 1, 1), date(2100, 1, 1)  # keeps date arithmetic and trend sizes sane


def sales_filters(
    date_from: Annotated[date | None, Query(description="Inclusive start day (UTC)", ge=MIN_DATE, le=MAX_DATE)] = None,
    date_to: Annotated[date | None, Query(description="Inclusive end day (UTC)", ge=MIN_DATE, le=MAX_DATE)] = None,
    category: ExactFilter = None,
    product_id: uuid.UUID | None = None,
) -> SalesFilters:
    if date_from and date_to and date_from > date_to:
        raise InvalidRequestError("date_from must not be after date_to")
    return SalesFilters(date_from, date_to, category, product_id)


FiltersDep = Annotated[SalesFilters, Depends(sales_filters)]


@router.get("", response_model=Page[SaleRead], summary="List sales (newest first)")
async def list_sales(service: SaleServiceDep, filters: FiltersDep, pagination: PaginationDep) -> Page[SaleRead]:
    return await service.list(filters, pagination)


@router.get("/summary", response_model=SalesSummary, summary="Revenue, units and per-category totals")
async def sales_summary(service: SaleServiceDep, filters: FiltersDep) -> SalesSummary:
    return await service.summary(filters)


@router.get("/trends", response_model=SalesTrend, summary="Revenue/units over time, bucketed in PostgreSQL")
async def sales_trends(
    service: SaleServiceDep,
    filters: FiltersDep,
    granularity: Granularity = Granularity.day,
) -> SalesTrend:
    return await service.trends(filters, granularity)


@router.post(
    "",
    response_model=SaleRead,
    status_code=status.HTTP_201_CREATED,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Record a sale and decrement stock atomically",
)
async def create_sale(data: SaleCreate, service: SaleServiceDep) -> SaleRead:
    return await service.create(data)

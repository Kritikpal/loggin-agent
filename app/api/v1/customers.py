from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import CONFLICT, CustomerServiceDep, PaginationDep, SearchFilter
from app.repositories.customer import CustomerFilters
from app.schemas.common import Page
from app.schemas.customer import CustomerCreate, CustomerRead

router = APIRouter(prefix="/customers", tags=["Customers"])


@router.get("", response_model=Page[CustomerRead], summary="List customers")
async def list_customers(
    service: CustomerServiceDep,
    pagination: PaginationDep,
    marketing_consent: Annotated[bool | None, Query(description="Only customers who (don't) accept marketing")] = None,
    search: SearchFilter = None,
) -> Page[CustomerRead]:
    return await service.list(CustomerFilters(marketing_consent, search), pagination)


@router.post(
    "", response_model=CustomerRead, status_code=status.HTTP_201_CREATED, responses=CONFLICT, summary="Create a customer"
)
async def create_customer(data: CustomerCreate, service: CustomerServiceDep) -> CustomerRead:
    return await service.create(data)

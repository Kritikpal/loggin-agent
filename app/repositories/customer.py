from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Customer
from app.repositories.common import contains_pattern, count_rows


@dataclass(frozen=True)
class CustomerFilters:
    marketing_consent: bool | None = None
    search: str | None = None


class CustomerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self, filters: CustomerFilters, limit: int, offset: int) -> tuple[list[Customer], int]:
        stmt = select(Customer)
        if filters.marketing_consent is not None:
            stmt = stmt.where(Customer.marketing_consent == filters.marketing_consent)
        if filters.search:
            pattern = contains_pattern(filters.search)
            stmt = stmt.where(Customer.name.ilike(pattern, escape="\\") | Customer.email.ilike(pattern, escape="\\"))
        total = await count_rows(self.session, stmt)
        rows = await self.session.scalars(stmt.order_by(Customer.name, Customer.id).limit(limit).offset(offset))
        return list(rows), total

    async def get_by_email(self, email: str) -> Customer | None:
        return await self.session.scalar(select(Customer).where(Customer.email == email))

    async def add(self, customer: Customer) -> Customer:
        self.session.add(customer)
        await self.session.flush()
        return customer

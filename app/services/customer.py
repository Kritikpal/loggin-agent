from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Customer
from app.repositories.customer import CustomerFilters, CustomerRepository
from app.schemas.common import Page, Pagination
from app.schemas.customer import CustomerCreate, CustomerRead


class CustomerService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.customers = CustomerRepository(session)

    async def list(self, filters: CustomerFilters, pagination: Pagination) -> Page[CustomerRead]:
        rows, total = await self.customers.list(filters, pagination.page_size, pagination.offset)
        return Page.of([CustomerRead.model_validate(c) for c in rows], total, pagination)

    async def create(self, data: CustomerCreate) -> CustomerRead:
        email = data.email.lower() if data.email else None
        if email and await self.customers.get_by_email(email):
            raise ConflictError(f"A customer with email '{email}' already exists")
        customer = Customer(
            name=data.name, phone=data.phone, email=email, marketing_consent=data.marketing_consent
        )
        try:
            await self.customers.add(customer)
            await self.session.commit()
        except IntegrityError as exc:  # lost a race on the unique email
            await self.session.rollback()
            raise ConflictError(f"A customer with email '{email}' already exists") from exc
        return CustomerRead.model_validate(customer)

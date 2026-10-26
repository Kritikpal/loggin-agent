from contextlib import asynccontextmanager
from datetime import date

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.core.database import get_session
from app.repositories.customer import CustomerFilters
from app.schemas.common import NO_NUL, Pagination
from app.services.customer import CustomerService

MAX_CUSTOMERS = 50  # keeps the tool output (and the model's context) bounded


class CustomerItem(BaseModel):
    """Contact details (email, phone) are deliberately left out: they would be sent to the model provider."""

    name: str
    marketing_consent: bool
    customer_since: date


class CustomerLookup(BaseModel):
    total_matching: int = Field(description="All customers matching the filters")
    returned: int
    truncated: bool = Field(description="True when more customers match than are listed")
    customers: list[CustomerItem]


class CustomerLookupInput(BaseModel):
    search: str | None = Field(
        default=None, max_length=100, pattern=NO_NUL, description="Case-insensitive substring of name or email"
    )
    marketing_consent: bool | None = Field(
        default=None, description="true: only customers who accept marketing; false: only those who don't"
    )


@tool(args_schema=CustomerLookupInput)
async def find_customers(search: str | None = None, marketing_consent: bool | None = None) -> str:
    """Look up customers by name/email substring and/or marketing consent. Returns JSON with the total number of
    matching customers and up to 50 of them (name, marketing consent, customer since), sorted by name.
    Use for questions like "how many customers can we market to" or "is X a customer". Contact details are not
    available, and customers are not linked to sales, so it cannot show what anyone bought."""
    async with asynccontextmanager(get_session)() as session:
        page = await CustomerService(session).list(
            CustomerFilters(marketing_consent=marketing_consent, search=search),
            Pagination(page=1, page_size=MAX_CUSTOMERS),
        )
    customers = [
        CustomerItem(name=c.name, marketing_consent=c.marketing_consent, customer_since=c.created_at.date())
        for c in page.items
    ]
    return CustomerLookup(
        total_matching=page.total, returned=len(customers), truncated=page.total > len(customers), customers=customers
    ).model_dump_json()

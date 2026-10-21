from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Self

from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field, model_validator

from app.api.v1.sales import MAX_DATE, MIN_DATE
from app.core.database import get_session
from app.core.errors import InvalidRequestError
from app.repositories.sale import SalesFilters
from app.schemas.common import NO_NUL
from app.schemas.sale import Granularity
from app.services.sale import SaleService

SaleDate = Annotated[date, Field(ge=MIN_DATE, le=MAX_DATE)]


class SalesTrendsInput(BaseModel):
    start_date: SaleDate = Field(description="Inclusive first day, UTC, ISO format YYYY-MM-DD")
    end_date: SaleDate = Field(description="Inclusive last day, UTC, ISO format YYYY-MM-DD")
    category: str | None = Field(
        default=None, max_length=100, pattern=NO_NUL, description="Product category, case-insensitive exact match"
    )

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


@tool(args_schema=SalesTrendsInput)
async def get_sales_trends(start_date: date, end_date: date, category: str | None = None) -> str:
    """Daily sales trend (revenue, units and sale count per UTC day, gaps filled with zeros) between two dates,
    optionally for one product category. Returns JSON.
    Use for questions about revenue, units sold, sales over time, comparing periods, or spotting a sales drop or
    spike. It says nothing about why sales changed and contains no stock data (use get_inventory_status for that).
    Call it once per period or category you need to compare."""
    filters = SalesFilters(date_from=start_date, date_to=end_date, category=category)
    async with asynccontextmanager(get_session)() as session:  # the same dependency the API uses
        try:
            trend = await SaleService(session).trends(filters, Granularity.day)
        except InvalidRequestError as exc:  # e.g. range spans too many daily buckets
            raise ToolException(exc.message) from exc
    return trend.model_dump_json()


get_sales_trends.handle_tool_error = True  # ToolException text goes back to the model instead of raising

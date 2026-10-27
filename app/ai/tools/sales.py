import json
from contextlib import asynccontextmanager
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Self

from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field, model_validator

from app.api.v1.sales import MAX_DATE, MIN_DATE
from app.core.database import get_session
from app.core.errors import InvalidRequestError
from app.repositories.sale import SalesFilters
from app.schemas.common import NO_NUL
from app.schemas.sale import Granularity, SalesSummary
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


@tool(args_schema=SalesTrendsInput)  # same date range + category inputs as the trend tool
async def get_sales_summary(start_date: date, end_date: date, category: str | None = None) -> str:
    """Sales totals between two dates: total revenue, units, sale count and average sale value, plus the same
    totals per product category (highest revenue first). Optionally for one category. Returns JSON.
    Use for "how much did we sell", best/worst categories, category share or comparing categories. It has no
    day-by-day breakdown (use get_sales_trends for that) and no stock data (use get_inventory_status)."""
    filters = SalesFilters(date_from=start_date, date_to=end_date, category=category)
    async with asynccontextmanager(get_session)() as session:
        summary = await SaleService(session).summary(filters)
    body = summary.model_dump(mode="json")
    for row in body["by_category"]:  # the reply guardrail only accepts numbers a tool returned, so shares come from here
        row["share_percent"] = _percent(Decimal(row["revenue"]), summary.total_revenue) or "0.00"
    return json.dumps(body, ensure_ascii=False)  # escaped "\u200b" would slip past injection()


CENT = Decimal("0.01")


def _percent(part: Decimal, whole: Decimal) -> str | None:
    """part / whole as a percentage with two decimals; None when whole is 0."""
    return str((part / whole * 100).quantize(CENT, ROUND_HALF_UP)) if whole else None


class ComparePeriodsInput(BaseModel):
    a_start: SaleDate = Field(description="First period's inclusive first day, UTC, YYYY-MM-DD")
    a_end: SaleDate = Field(description="First period's inclusive last day")
    b_start: SaleDate = Field(description="Second period's inclusive first day")
    b_end: SaleDate = Field(description="Second period's inclusive last day")
    category: str | None = Field(
        default=None, max_length=100, pattern=NO_NUL, description="Product category, case-insensitive exact match"
    )

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.a_start > self.a_end or self.b_start > self.b_end:
            raise ValueError("each period's start must not be after its end")
        return self


def _period(summary: SalesSummary) -> dict:
    return {
        "date_from": summary.date_from.isoformat(), "date_to": summary.date_to.isoformat(),
        "revenue": str(summary.total_revenue), "units": summary.total_units, "sale_count": summary.sale_count,
    }


@tool(args_schema=ComparePeriodsInput)
async def compare_sales_periods(
    a_start: date, a_end: date, b_start: date, b_end: date, category: str | None = None
) -> str:
    """Totals for two periods and the change between them. Use for any comparison between periods ("this week vs
    last week", "did sales drop"): revenue_change is second minus first, revenue_change_percent is relative to the
    first (null when the first had no revenue). Returns JSON."""
    async with asynccontextmanager(get_session)() as session:
        service = SaleService(session)
        a = await service.summary(SalesFilters(date_from=a_start, date_to=a_end, category=category))
        b = await service.summary(SalesFilters(date_from=b_start, date_to=b_end, category=category))
    change = b.total_revenue - a.total_revenue
    return json.dumps({
        "period_a": _period(a), "period_b": _period(b),
        "revenue_change": str(change.quantize(CENT)), "revenue_change_percent": _percent(change, a.total_revenue),
    }, ensure_ascii=False)

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.core.database import get_session
from app.repositories.product import ProductFilters
from app.schemas.common import NO_NUL, Pagination
from app.schemas.inventory import InventoryRead
from app.services.inventory import InventoryService

MAX_ITEMS = 50  # keeps the tool output (and the model's context) bounded

StockStatus = Literal["out_of_stock", "low_stock", "in_stock"]


class StockItem(BaseModel):
    sku: str
    product_name: str
    category: str
    size: str
    color: str
    quantity: int
    reorder_level: int
    stock_status: StockStatus = Field(description="out_of_stock: 0 units; low_stock: at/below reorder level")

    @classmethod
    def build(cls, inv: InventoryRead) -> "StockItem":
        status: StockStatus = "out_of_stock" if inv.quantity == 0 else "low_stock" if inv.is_low_stock else "in_stock"
        return cls(**inv.model_dump(include=set(cls.model_fields) - {"stock_status"}), stock_status=status)


class InventoryStatus(BaseModel):
    as_of: datetime = Field(description="UTC time of this snapshot; stock history is not available")
    total_matching: int = Field(description="All items matching the filters")
    returned: int
    truncated: bool = Field(description="True when more items match than are listed")
    items: list[StockItem]


class InventoryStatusInput(BaseModel):
    category: str | None = Field(
        default=None, max_length=100, pattern=NO_NUL, description="Product category, case-insensitive exact match"
    )
    low_stock_only: bool = Field(
        default=False, description="If true, list only items at or below their reorder level, most urgent first"
    )


@tool(args_schema=InventoryStatusInput)
async def get_inventory_status(category: str | None = None, low_stock_only: bool = False) -> str:
    """Current stock levels per SKU: quantity, reorder level and stock status (out_of_stock, low_stock, in_stock),
    optionally for one product category and/or only items that need restocking. Returns JSON.
    Use for questions about stock, availability, stockouts or restocking, and to check whether stock could explain
    a sales change. It is a snapshot of right now: it cannot show past stock levels and contains no sales data
    (use get_sales_trends for that). At most 50 items are listed; check `truncated` and `total_matching`."""
    async with asynccontextmanager(get_session)() as session:
        page = await InventoryService(session).list(
            ProductFilters(category=category), Pagination(page=1, page_size=MAX_ITEMS), low_stock_only=low_stock_only
        )
    items = [StockItem.build(inv) for inv in page.items]
    return InventoryStatus(
        as_of=datetime.now(UTC),
        total_matching=page.total,
        returned=len(items),
        truncated=page.total > len(items),
        items=items,
    ).model_dump_json()

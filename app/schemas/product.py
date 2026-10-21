import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import Cost, Price, reject_explicit_nulls, text


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: text(200)
    category: text(100)
    size: text(20)
    color: text(50)
    sku: text(100)
    price: Price
    cost_price: Cost
    initial_quantity: int = Field(0, ge=0, le=1_000_000, description="Opening stock for the product's inventory row")
    reorder_level: int = Field(10, ge=0, le=1_000_000)


class ProductUpdate(BaseModel):
    """The SKU is the product's identity and cannot be changed."""

    model_config = ConfigDict(extra="forbid")

    name: text(200) | None = None
    category: text(100) | None = None
    size: text(20) | None = None
    color: text(50) | None = None
    price: Price | None = None
    cost_price: Cost | None = None

    @model_validator(mode="after")
    def at_least_one_field(self) -> "ProductUpdate":
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        reject_explicit_nulls(self)
        return self


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    category: str
    size: str
    color: str
    sku: str
    price: Decimal
    cost_price: Decimal
    created_at: datetime

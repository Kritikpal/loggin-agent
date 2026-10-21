import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import Inventory, Product
from app.schemas.common import reject_explicit_nulls


class InventoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: int | None = Field(None, ge=0, le=1_000_000)
    reorder_level: int | None = Field(None, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def at_least_one_field(self) -> "InventoryUpdate":
        if not self.model_fields_set:
            raise ValueError("Provide quantity and/or reorder_level")
        reject_explicit_nulls(self)
        return self


class InventoryRead(BaseModel):
    product_id: uuid.UUID
    sku: str
    product_name: str
    category: str
    size: str
    color: str
    quantity: int
    reorder_level: int
    is_low_stock: bool = Field(description="True when quantity <= reorder_level")
    updated_at: datetime

    @classmethod
    def build(cls, inventory: Inventory, product: Product) -> "InventoryRead":
        return cls(
            product_id=inventory.product_id,
            sku=product.sku,
            product_name=product.name,
            category=product.category,
            size=product.size,
            color=product.color,
            quantity=inventory.quantity,
            reorder_level=inventory.reorder_level,
            is_low_stock=inventory.quantity <= inventory.reorder_level,
            updated_at=inventory.updated_at,
        )

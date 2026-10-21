from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, Field, StringConstraints

T = TypeVar("T")

# Money is Decimal end to end: NUMERIC(12,2) in PostgreSQL, serialized as an exact string in JSON.
Price = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
Cost = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]
NO_NUL = r"^[^\x00]*$"  # PostgreSQL text cannot contain NUL bytes
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, pattern=NO_NUL)]


def text(max_length: int) -> Any:
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=max_length, pattern=NO_NUL)]


MAX_PAGE_SIZE = 200
MAX_PAGE = 1_000_000


def reject_explicit_nulls(model: BaseModel) -> None:
    """PATCH bodies may omit a field but not null it: every patched column is NOT NULL."""
    nulls = sorted(f for f in model.model_fields_set if getattr(model, f) is None)
    if nulls:
        raise ValueError(f"Fields cannot be null: {', '.join(nulls)}")


@dataclass(frozen=True)
class Pagination:
    page: int = 1
    page_size: int = 50

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int = Field(description="Total rows matching the filters, across all pages")
    page: int
    page_size: int

    @classmethod
    def of(cls, items: list[T], total: int, pagination: Pagination) -> "Page[T]":
        return cls(items=items, total=total, page=pagination.page, page_size=pagination.page_size)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any = None


class ErrorResponse(BaseModel):
    error: ErrorBody

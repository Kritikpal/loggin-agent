import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from sqlalchemy import DateTime, MetaData, Numeric, func, text
from sqlalchemy.orm import DeclarativeBase, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# Python-side defaults keep values available right after flush (no lazy refresh in async);
# server defaults keep raw SQL inserts valid too.
UuidPk = Annotated[
    uuid.UUID,
    mapped_column(primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")),
]
Money = Annotated[Decimal, mapped_column(Numeric(12, 2))]
CreatedAt = Annotated[
    datetime,
    mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now()),
]

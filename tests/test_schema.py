import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

EXPECTED_TABLES = {"products", "inventory", "sales", "customers", "campaigns"}


async def test_migration_creates_the_five_business_tables(session):
    rows = await session.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    assert EXPECTED_TABLES <= set(rows)


async def test_database_enforces_non_negative_stock(session, make_product):
    product = await make_product(quantity=1)
    with pytest.raises(IntegrityError):
        await session.execute(text("UPDATE inventory SET quantity = -1 WHERE product_id = :id"), {"id": product.id})
    await session.rollback()

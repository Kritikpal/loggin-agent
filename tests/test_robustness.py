from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.repositories.customer import CustomerRepository
from app.repositories.product import ProductRepository

PRODUCT = {"name": "N", "category": "C", "size": "S", "color": "Red", "sku": "RACE-1", "price": "5.00", "cost_price": "1.00"}


async def test_unique_sku_race_falls_back_to_conflict(client, monkeypatch):
    """Two requests pass the friendly pre-check at once; the database unique constraint still answers 409."""
    assert (await client.post("/api/v1/products", json=PRODUCT)).status_code == 201

    async def no_precheck(self, sku):
        return None

    monkeypatch.setattr(ProductRepository, "get_by_sku", no_precheck)
    resp = await client.post("/api/v1/products", json=PRODUCT)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "conflict"
    assert (await client.get("/api/v1/products", params={"search": "RACE-1"})).json()["total"] == 1  # nothing half-saved


async def test_unique_email_race_falls_back_to_conflict(client, monkeypatch):
    body = {"name": "A", "email": "race@example.com"}
    assert (await client.post("/api/v1/customers", json=body)).status_code == 201

    async def no_precheck(self, email):
        return None

    monkeypatch.setattr(CustomerRepository, "get_by_email", no_precheck)
    assert (await client.post("/api/v1/customers", json=body)).status_code == 409


async def test_unexpected_errors_return_a_generic_500_without_leaking_details():
    app = create_app()

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret internal detail")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.get("/boom")
    assert resp.status_code == 500
    assert resp.json() == {"error": {"code": "internal_error", "message": "Internal server error", "details": None}}


async def test_trends_with_explicit_start_for_week_and_month_buckets(client, make_product, add_sale):
    from tests.test_sales import at

    product = await make_product(price="1.00")
    await add_sale(product, at("2025-02-12"))  # a Wednesday
    week = (await client.get("/api/v1/sales/trends", params={
        "granularity": "week", "date_from": "2025-02-03", "date_to": "2025-02-23"})).json()
    assert [(p["period"], p["units"]) for p in week["points"]] == [("2025-02-03", 0), ("2025-02-10", 1), ("2025-02-17", 0)]
    month = (await client.get("/api/v1/sales/trends", params={
        "granularity": "month", "date_from": "2024-12-15", "date_to": "2025-02-20"})).json()
    assert [(p["period"], p["units"]) for p in month["points"]] == [("2024-12-01", 0), ("2025-01-01", 0), ("2025-02-01", 1)]


# ---------------------------------------------------------------- regressions from the independent code review


async def test_patch_with_explicit_null_is_a_422_not_a_500(client, make_product):
    product = await make_product()
    for url, body in (
        (f"/api/v1/inventory/{product.id}", {"quantity": None}),
        (f"/api/v1/inventory/{product.id}", {"quantity": 5, "reorder_level": None}),
        (f"/api/v1/products/{product.id}", {"price": None}),
        (f"/api/v1/products/{product.id}", {"name": None, "color": "Red"}),
    ):
        resp = await client.patch(url, json=body)
        assert resp.status_code == 422, body
        assert resp.json()["error"]["code"] == "validation_error"


async def test_nul_bytes_are_rejected_everywhere_text_reaches_postgres(client):
    nul = chr(0)
    assert (await client.post("/api/v1/products", json={**PRODUCT, "name": f"a{nul}b"})).status_code == 422
    assert (await client.post("/api/v1/customers", json={"name": f"a{nul}b"})).status_code == 422
    assert (await client.post("/api/v1/campaigns", json={"name": "n", "message": f"m{nul}", "discount_percent": "1"})).status_code == 422
    for path in ("products", "inventory", "inventory/low-stock", "customers", "sales"):
        for param in ("search", "category", "size", "color"):
            if path in ("sales", "customers") and param != ("category" if path == "sales" else "search"):
                continue
            resp = await client.get(f"/api/v1/{path}", params={param: f"x{nul}"})
            assert resp.status_code == 422, (path, param)
    assert (await client.get("/api/v1/products", params={"search": "x" * 101})).status_code == 422


async def test_absurd_pagination_and_dates_are_rejected_cleanly(client):
    assert (await client.get("/api/v1/products", params={"page": 10**20})).status_code == 422
    assert (await client.get("/api/v1/products", params={"page": 1_000_001})).status_code == 422
    for path in ("sales", "sales/summary", "sales/trends"):
        assert (await client.get(f"/api/v1/{path}", params={"date_to": "9999-12-31"})).status_code == 422
        assert (await client.get(f"/api/v1/{path}", params={"date_from": "0001-01-01"})).status_code == 422


async def test_trend_range_producing_too_many_buckets_is_rejected(client):
    huge = {"granularity": "day", "date_from": "2000-01-01", "date_to": "2100-01-01"}
    resp = await client.get("/api/v1/sales/trends", params=huge)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid_request"
    eighty_years_monthly = {**huge, "granularity": "month", "date_to": "2080-01-01"}  # 961 buckets: allowed
    assert (await client.get("/api/v1/sales/trends", params=eighty_years_monthly)).status_code == 200


async def test_sale_total_beyond_numeric_range_is_a_422_and_changes_nothing(client, make_product, session_factory):
    from tests.test_sales import sale_count, stock_of

    product = await make_product(price="1000000.00", quantity=20_000)
    resp = await client.post("/api/v1/sales", json={"product_id": str(product.id), "quantity": 10_000})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid_request"
    assert await stock_of(session_factory, product) == 20_000 and await sale_count(session_factory) == 0
    ok = await client.post("/api/v1/sales", json={"product_id": str(product.id), "quantity": 9_999})
    assert ok.status_code == 201 and ok.json()["total_amount"] == "9999000000.00"


async def test_sale_for_product_without_inventory_row_is_404(client, make_product, session):
    from sqlalchemy import text

    product = await make_product()
    await session.execute(text("DELETE FROM inventory WHERE product_id = :id"), {"id": product.id})
    await session.commit()
    resp = await client.post("/api/v1/sales", json={"product_id": str(product.id), "quantity": 1})
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


async def test_sale_sees_fresh_stock_even_when_the_session_cached_the_row(session_factory, make_product):
    """A long-lived session (e.g. inside an AI tool) must not sell against a stale cached quantity."""
    import pytest
    from sqlalchemy import text

    from app.core.errors import InsufficientStockError
    from app.repositories.inventory import InventoryRepository
    from app.schemas.sale import SaleCreate
    from app.services.sale import SaleService

    product = await make_product(quantity=10)
    async with session_factory() as long_lived, session_factory() as other:
        cached, _ = await InventoryRepository(long_lived).get_with_product(product.id)
        assert cached.quantity == 10
        await other.execute(text("UPDATE inventory SET quantity = 2 WHERE product_id = :id"), {"id": product.id})
        await other.commit()
        with pytest.raises(InsufficientStockError):
            await SaleService(long_lived).create(SaleCreate(product_id=product.id, quantity=5))


async def test_repository_refuses_unknown_granularity_before_it_reaches_sql(session):
    import pytest

    from app.repositories.sale import SaleRepository, SalesFilters

    with pytest.raises(ValueError):
        await SaleRepository(session).totals_by_period(SalesFilters(), "day'); DROP TABLE sales; --")

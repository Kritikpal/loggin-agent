import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import Inventory, Sale
from app.repositories.sale import SaleRepository
from app.schemas.sale import SaleCreate
from app.services.sale import SaleService


async def stock_of(session_factory, product) -> int:
    async with session_factory() as s:
        return await s.scalar(select(Inventory.quantity).where(Inventory.product_id == product.id))


async def sale_count(session_factory) -> int:
    async with session_factory() as s:
        return await s.scalar(select(func.count()).select_from(Sale))


def at(day: str, clock: str = "12:00:00") -> datetime:
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=UTC)


# ---------------------------------------------------------------- creating sales


async def test_sale_decrements_stock_and_uses_catalogue_price(client, make_product, session_factory):
    product = await make_product(price="19.99", quantity=10)
    resp = await client.post("/api/v1/sales", json={"product_id": str(product.id), "quantity": 3})
    assert resp.status_code == 201
    body = resp.json()
    assert body["unit_price"] == "19.99" and body["total_amount"] == "59.97" and body["quantity"] == 3
    assert body["sku"] == product.sku and body["category"] == "Jeans"
    assert await stock_of(session_factory, product) == 7


async def test_selling_exactly_the_remaining_stock_succeeds_then_next_sale_is_rejected(client, make_product, session_factory):
    product = await make_product(quantity=2)
    payload = {"product_id": str(product.id), "quantity": 2}
    assert (await client.post("/api/v1/sales", json=payload)).status_code == 201
    assert await stock_of(session_factory, product) == 0
    again = await client.post("/api/v1/sales", json={**payload, "quantity": 1})
    assert again.status_code == 409 and again.json()["error"]["code"] == "insufficient_stock"


async def test_insufficient_stock_is_rejected_and_changes_nothing(client, make_product, session_factory):
    product = await make_product(quantity=2)
    resp = await client.post("/api/v1/sales", json={"product_id": str(product.id), "quantity": 3})
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "insufficient_stock"
    assert error["details"] == {"product_id": str(product.id), "requested": 3, "available": 2}
    assert await stock_of(session_factory, product) == 2
    assert await sale_count(session_factory) == 0


async def test_invalid_sale_requests(client, make_product):
    product = await make_product()
    pid = str(product.id)
    for bad in (
        {"product_id": pid, "quantity": 0}, {"product_id": pid, "quantity": -1}, {"product_id": pid},
        {"product_id": "nope", "quantity": 1}, {"quantity": 1},
        {"product_id": pid, "quantity": 1, "unit_price": "0.01"},  # clients cannot set the price
    ):
        resp = await client.post("/api/v1/sales", json=bad)
        assert resp.status_code == 422, bad
        assert resp.json()["error"]["code"] == "validation_error"
    unknown = await client.post("/api/v1/sales", json={"product_id": str(uuid.uuid4()), "quantity": 1})
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found"


async def test_concurrent_sales_never_oversell(client, make_product, session_factory):
    product = await make_product(quantity=5)
    payload = {"product_id": str(product.id), "quantity": 1}

    responses = await asyncio.gather(*(client.post("/api/v1/sales", json=payload) for _ in range(25)))

    codes = sorted(r.status_code for r in responses)
    assert codes.count(201) == 5 and codes.count(409) == 20
    assert await stock_of(session_factory, product) == 0
    assert await sale_count(session_factory) == 5


async def test_concurrent_sales_of_mixed_sizes_stay_consistent(client, make_product, session_factory):
    """Stock 10, requests of 3 x 6: exactly 3 fit (9 units); the ledger and the stock must agree."""
    product = await make_product(quantity=10)
    payload = {"product_id": str(product.id), "quantity": 3}
    responses = await asyncio.gather(*(client.post("/api/v1/sales", json=payload) for _ in range(6)))
    assert sorted(r.status_code for r in responses) == [201, 201, 201, 409, 409, 409]
    async with session_factory() as s:
        sold = await s.scalar(select(func.sum(Sale.quantity)))
    assert sold == 9 and await stock_of(session_factory, product) == 1


async def test_failure_midway_rolls_back_both_stock_and_sale(session_factory, make_product, monkeypatch):
    """The stock decrement is flushed to the database, then saving the sale blows up: nothing may persist."""
    product = await make_product(quantity=5)

    async def exploding_add(self, sale):
        await self.session.flush()  # sends the inventory UPDATE inside the open transaction
        raise RuntimeError("simulated failure while recording the sale")

    monkeypatch.setattr(SaleRepository, "add", exploding_add)
    async with session_factory() as s:
        with pytest.raises(RuntimeError):
            await SaleService(s).create(SaleCreate(product_id=product.id, quantity=2))

    assert await stock_of(session_factory, product) == 5
    assert await sale_count(session_factory) == 0


async def test_database_constraint_violation_rolls_back_stock(session_factory, make_product, monkeypatch):
    """Same guarantee when the failure comes from PostgreSQL itself (CHECK total = quantity * price)."""
    from sqlalchemy.exc import IntegrityError

    product = await make_product(price="10.00", quantity=5)
    original_add = SaleRepository.add

    async def corrupt_total(self, sale):
        sale.total_amount = Decimal("1.00")
        return await original_add(self, sale)

    monkeypatch.setattr(SaleRepository, "add", corrupt_total)
    async with session_factory() as s:
        with pytest.raises(IntegrityError):
            await SaleService(s).create(SaleCreate(product_id=product.id, quantity=2))
    assert await stock_of(session_factory, product) == 5
    assert await sale_count(session_factory) == 0


# ---------------------------------------------------------------- listing, summary, trends


async def test_list_sales_newest_first_with_filters_and_pagination(client, make_product, add_sale):
    jeans = await make_product(category="Jeans")
    shirt = await make_product(category="Shirts")
    await add_sale(jeans, at("2025-03-01"))
    await add_sale(shirt, at("2025-03-02"))
    await add_sale(jeans, at("2025-03-03"))

    everything = (await client.get("/api/v1/sales")).json()
    assert everything["total"] == 3
    assert [s["sold_at"][:10] for s in everything["items"]] == ["2025-03-03", "2025-03-02", "2025-03-01"]

    by_category = (await client.get("/api/v1/sales", params={"category": "jeans"})).json()
    assert by_category["total"] == 2
    by_product = (await client.get("/api/v1/sales", params={"product_id": str(shirt.id)})).json()
    assert by_product["total"] == 1
    by_dates = (await client.get("/api/v1/sales", params={"date_from": "2025-03-02", "date_to": "2025-03-02"})).json()
    assert by_dates["total"] == 1

    page = (await client.get("/api/v1/sales", params={"page": 2, "page_size": 2})).json()
    assert page["total"] == 3 and len(page["items"]) == 1


async def test_summary_revenue_is_exact(client, make_product, add_sale):
    jeans = await make_product(category="Jeans", price="10.10")
    shirt = await make_product(category="Shirts", price="25.55")
    await add_sale(jeans, at("2025-03-01"), quantity=2)  # 20.20
    await add_sale(jeans, at("2025-03-02"), quantity=1)  # 10.10
    await add_sale(shirt, at("2025-03-02"), quantity=3)  # 76.65

    body = (await client.get("/api/v1/sales/summary")).json()
    assert body["total_revenue"] == "106.95"
    assert body["total_units"] == 6 and body["sale_count"] == 3
    assert body["average_sale_value"] == "35.65"  # 106.95 / 3
    assert body["by_category"] == [
        {"category": "Shirts", "revenue": "76.65", "units": 3, "sale_count": 1},
        {"category": "Jeans", "revenue": "30.30", "units": 3, "sale_count": 2},
    ]

    jeans_only = (await client.get("/api/v1/sales/summary", params={"category": "Jeans"})).json()
    assert jeans_only["total_revenue"] == "30.30" and len(jeans_only["by_category"]) == 1
    one_day = (await client.get("/api/v1/sales/summary", params={"date_from": "2025-03-02", "date_to": "2025-03-02"})).json()
    assert one_day["total_revenue"] == "86.75" and one_day["date_from"] == "2025-03-02"
    one_product = (await client.get("/api/v1/sales/summary", params={"product_id": str(shirt.id)})).json()
    assert one_product["total_revenue"] == "76.65"


async def test_summary_of_nothing_is_zero(client):
    body = (await client.get("/api/v1/sales/summary")).json()
    assert body["total_revenue"] == "0" or Decimal(body["total_revenue"]) == 0
    assert body["total_units"] == 0 and body["sale_count"] == 0
    assert Decimal(body["average_sale_value"]) == 0 and body["by_category"] == []


async def test_summary_average_rounds_half_up_to_cents(client, make_product, add_sale):
    product = await make_product(price="10.00")
    for day, qty in (("2025-03-01", 1), ("2025-03-02", 1), ("2025-03-03", 1)):
        await add_sale(product, at(day), quantity=qty)
    await add_sale(product, at("2025-03-04"), quantity=1)  # 40.00 / 4 = 10.00
    assert (await client.get("/api/v1/sales/summary")).json()["average_sale_value"] == "10.00"
    await add_sale(product, at("2025-03-05"), quantity=2)  # 60.00 / 5 = 12.00
    assert (await client.get("/api/v1/sales/summary")).json()["average_sale_value"] == "12.00"


async def test_date_range_is_inclusive_of_whole_end_day_in_utc(client, make_product, add_sale):
    product = await make_product()
    await add_sale(product, at("2025-03-01", "00:00:00"))  # first instant of date_from
    await add_sale(product, at("2025-03-01", "23:59:59"))  # last second of the same day
    await add_sale(product, at("2025-03-02", "00:00:00"))  # next day: excluded
    await add_sale(product, at("2025-02-28", "23:59:59"))  # previous day: excluded
    body = (await client.get("/api/v1/sales/summary", params={"date_from": "2025-03-01", "date_to": "2025-03-01"})).json()
    assert body["sale_count"] == 2


async def test_trends_by_day_fill_gaps_with_zeros(client, make_product, add_sale):
    product = await make_product(price="10.00")
    await add_sale(product, at("2025-03-03"), quantity=2)
    await add_sale(product, at("2025-03-03", "18:00:00"), quantity=1)
    await add_sale(product, at("2025-03-05"), quantity=4)

    body = (await client.get("/api/v1/sales/trends", params={"granularity": "day"})).json()
    assert body["granularity"] == "day"
    assert [(p["period"], p["revenue"], p["units"], p["sale_count"]) for p in body["points"]] == [
        ("2025-03-03", "30.00", 3, 2), ("2025-03-04", "0.00", 0, 0), ("2025-03-05", "40.00", 4, 1),
    ]

    padded = (await client.get("/api/v1/sales/trends", params={"date_from": "2025-03-02", "date_to": "2025-03-06"})).json()
    assert [p["period"] for p in padded["points"]] == [f"2025-03-0{d}" for d in range(2, 7)]
    assert [p["units"] for p in padded["points"]] == [0, 3, 0, 4, 0]


async def test_trends_by_week_start_on_monday(client, make_product, add_sale):
    product = await make_product(price="5.00")
    await add_sale(product, at("2025-01-06"))  # Monday  -> week of 01-06
    await add_sale(product, at("2025-01-12", "23:00:00"))  # Sunday -> still week of 01-06
    await add_sale(product, at("2025-01-14"))  # Tuesday -> week of 01-13
    await add_sale(product, at("2025-01-29"))  # skips the week of 01-20
    body = (await client.get("/api/v1/sales/trends", params={"granularity": "week"})).json()
    assert [(p["period"], p["units"]) for p in body["points"]] == [
        ("2025-01-06", 2), ("2025-01-13", 1), ("2025-01-20", 0), ("2025-01-27", 1),
    ]


async def test_trends_by_month_cross_year_and_utc_boundaries(client, make_product, add_sale):
    product = await make_product(price="2.00")
    await add_sale(product, at("2024-12-31", "23:59:59"))
    await add_sale(product, at("2025-01-01", "00:00:00"))
    await add_sale(product, at("2025-03-15"))
    body = (await client.get("/api/v1/sales/trends", params={"granularity": "month"})).json()
    assert [(p["period"], p["units"]) for p in body["points"]] == [
        ("2024-12-01", 1), ("2025-01-01", 1), ("2025-02-01", 0), ("2025-03-01", 1),
    ]


async def test_trends_respect_category_filter_and_show_a_decline(client, make_product, add_sale):
    jeans = await make_product(category="Jeans", price="10.00")
    tees = await make_product(category="T-Shirts", price="10.00")
    for day, jeans_qty in (("2025-03-03", 5), ("2025-03-10", 3), ("2025-03-17", 1)):  # jeans fade out
        await add_sale(jeans, at(day), quantity=jeans_qty)
        await add_sale(tees, at(day), quantity=2)  # tees stay flat
    jeans_trend = (await client.get("/api/v1/sales/trends", params={"granularity": "week", "category": "Jeans"})).json()
    tee_trend = (await client.get("/api/v1/sales/trends", params={"granularity": "week", "category": "T-Shirts"})).json()
    assert [p["units"] for p in jeans_trend["points"]] == [5, 3, 1]
    assert [p["units"] for p in tee_trend["points"]] == [2, 2, 2]


async def test_trends_with_no_data_is_empty(client):
    assert (await client.get("/api/v1/sales/trends")).json()["points"] == []


async def test_invalid_date_filters(client):
    reversed_range = await client.get("/api/v1/sales/summary", params={"date_from": "2025-03-05", "date_to": "2025-03-01"})
    assert reversed_range.status_code == 422 and reversed_range.json()["error"]["code"] == "invalid_request"
    for path in ("sales", "sales/summary", "sales/trends"):
        assert (await client.get(f"/api/v1/{path}", params={"date_from": "not-a-date"})).status_code == 422
    assert (await client.get("/api/v1/sales/trends", params={"granularity": "hour"})).status_code == 422
    assert (await client.get("/api/v1/sales", params={"product_id": "nope"})).status_code == 422

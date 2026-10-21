"""The seed script must be repeatable and must plant the scenarios later AI agents will be asked to find."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.models import Campaign, Customer, Inventory, Product, Sale
from scripts.seed_data import build_dataset, seed

AS_OF = date(2026, 10, 21)
ALL_MODELS = (Product, Inventory, Sale, Customer, Campaign)


async def counts(session) -> dict[str, int]:
    return {m.__tablename__: await session.scalar(select(func.count()).select_from(m)) for m in ALL_MODELS}


async def test_seed_is_complete_and_idempotent(session):
    first = await seed(session, AS_OF)
    assert (first["products"], first["inventory"], first["customers"], first["campaigns"]) == (150, 150, 100, 5)
    assert 1900 <= first["sales"] <= 2100

    assert await seed(session, AS_OF) == first  # second run: nothing duplicated
    assert await seed(session, AS_OF + timedelta(days=30)) == first  # different date: still no mixed timelines
    assert await counts(session) == first


async def test_reset_demo_rebuilds_identical_data(session):
    first = await seed(session, AS_OF)
    assert await seed(session, AS_OF, reset_demo=True) == first


async def test_seed_leaves_unrelated_rows_alone(session, make_product):
    mine = await make_product(sku="MY-OWN-SKU")
    await seed(session, AS_OF, reset_demo=True)
    assert await session.scalar(select(Product.id).where(Product.sku == "MY-OWN-SKU")) == mine.id


async def test_seed_data_is_valid_and_fictional(session):
    await seed(session, AS_OF)
    assert await session.scalar(select(func.count()).select_from(Sale).where(Sale.total_amount != Sale.quantity * Sale.unit_price)) == 0
    assert await session.scalar(select(func.count()).select_from(Campaign).where(Campaign.status != "draft")) == 0
    emails = (await session.scalars(select(Customer.email))).all()
    assert all(e.endswith("@example.com") for e in emails) and len(set(emails)) == 100
    phones = [p for p in (await session.scalars(select(Customer.phone))).all() if p]
    assert all("-555-01" in p for p in phones)  # 555-01xx numbers are reserved for fiction


def _units(dataset, category, start, end):
    cat = {p["sku"]: p["category"] for p in dataset["products"]}
    return sum(s["quantity"] for s in dataset["sales"] if cat[s["sku"]] == category and start <= s["sold_at"].date() < end)


def test_dataset_is_deterministic():
    assert build_dataset(AS_OF) == build_dataset(AS_OF)


def test_planted_business_scenarios():
    data = build_dataset(AS_OF)
    recent_start, prior_start = AS_OF - timedelta(days=14), AS_OF - timedelta(days=28)

    def change(category):  # latest 14 days vs the 14 before
        return _units(data, category, recent_start, AS_OF) / _units(data, category, prior_start, recent_start)

    assert change("Jeans") < 0.55  # 1. jeans decline sharply
    assert 0.9 <= change("T-Shirts") <= 1.1  # 4. t-shirts are stable

    def revenue(start, end):
        return sum(s["total_amount"] for s in data["sales"] if start <= s["sold_at"].date() < end)

    assert revenue(recent_start, AS_OF) < Decimal("0.9") * revenue(prior_start, recent_start)  # 5. overall decline

    stock = {s["sku"]: s for s in data["stock"]}
    popular = [p["sku"] for p in data["products"] if p["category"] == "Jeans" and p["size"] in ("32", "34")]
    assert sum(stock[sku]["quantity"] == 0 for sku in popular) >= 6  # 2. popular jeans sizes are out of stock
    assert all(stock[sku]["quantity"] <= stock[sku]["reorder_level"] for sku in popular)

    linen = [p["sku"] for p in data["products"] if p["name"] == "Linen Summer Shirt"]
    assert linen and all(stock[sku]["quantity"] > 10 * stock[sku]["reorder_level"] for sku in linen)  # 3. overstock
    others = [s for sku, s in stock.items() if sku not in linen]
    assert all(s["quantity"] <= 10 * s["reorder_level"] for s in others)

"""Deterministic, idempotent demo data for the clothing store.

    python -m scripts.seed_data                      # seed (no-op for rows that already exist)
    python -m scripts.seed_data --as-of 2026-10-21   # pin "today" (sales cover the 90 days before it)
    python -m scripts.seed_data --reset-demo         # delete the demo products (+ their sales) first, then re-seed

Every row gets an ID derived from a fixed namespace, and inserts use ON CONFLICT DO NOTHING, so running the script
twice never duplicates anything. Randomness uses a fixed seed, so the same --as-of always yields the same data.

Business scenarios planted for later AI agents to discover (see `_trend` / `_stock` below):
  1. Jeans sales decline over the latest 14 days.
  2. The popular jeans sizes (32, 34) run out of stock - which is part of why (1) happens.
  3. One shirt line (Linen Summer Shirt) is heavily overstocked and slow-moving.
  4. T-shirt sales stay flat.
  5. Because jeans are the biggest category, overall sales fall in the latest two weeks.
All names, e-mail addresses (example.com) and phone numbers (fictional 555-01xx range) are made up.
"""

import argparse
import asyncio
import math
import random
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocal, engine
from app.models import Campaign, Customer, Inventory, Product, Sale

NAMESPACE = uuid.UUID("5e1ed000-0000-4000-8000-0000000000a1")
RNG_SEED = 284  # picked so only jeans decline for --as-of 2026-10-21 (other categories' Poisson noise stays small)
HISTORY_DAYS = 90
RECENT_DAYS = 14  # the "latest two weeks" the scenarios are about
STOCKOUT_DAYS = 10  # popular jeans are unavailable for the last N days
CUSTOMER_COUNT = 100
CHUNK = 500

LETTER_SIZES = ["XS", "S", "M", "L", "XL"]
JEAN_SIZES = ["30", "32", "34", "36", "38"]
LETTER_WEIGHTS = {"XS": 0.08, "S": 0.22, "M": 0.30, "L": 0.25, "XL": 0.15}
JEAN_WEIGHTS = {"30": 0.15, "32": 0.30, "34": 0.30, "36": 0.15, "38": 0.10}
POPULAR_JEAN_SIZES = {"32", "34"}
SLOW_LINE = "Linen Summer Shirt"
WEEKDAY_FACTOR = [0.85, 0.85, 0.90, 0.95, 1.15, 1.40, 1.10]  # Mon..Sun

# category -> (code, sizes, size weights, colors, [(line name, code, price, cost)], avg sales/day, reorder level)
CATALOG = {
    "Jeans": ("JNS", JEAN_SIZES, JEAN_WEIGHTS, ["Indigo", "Black", "Light Wash"],
              [("Slim Fit Jeans", "SLM", "89.00", "31.00"), ("Straight Fit Jeans", "STR", "79.00", "27.00")], 7.0, 15),
    "T-Shirts": ("TSH", LETTER_SIZES, LETTER_WEIGHTS, ["White", "Black", "Navy"],
                 [("Classic Crew T-Shirt", "CRW", "24.00", "7.00"), ("V-Neck T-Shirt", "VNK", "26.00", "8.00")], 6.0, 20),
    "Shirts": ("SHT", LETTER_SIZES, LETTER_WEIGHTS, ["White", "Sky Blue", "Olive"],
               [("Oxford Button-Down Shirt", "OXF", "59.00", "20.00"), (SLOW_LINE, "LIN", "64.00", "21.00")], 4.0, 15),
    "Hoodies": ("HOD", LETTER_SIZES, LETTER_WEIGHTS, ["Grey", "Black"],
                [("Pullover Hoodie", "PUL", "54.00", "18.00"), ("Zip-Up Hoodie", "ZIP", "62.00", "21.00")], 2.5, 10),
    "Dresses": ("DRS", LETTER_SIZES, LETTER_WEIGHTS, ["Black", "Emerald"],
                [("Wrap Dress", "WRP", "78.00", "26.00"), ("Midi Dress", "MID", "85.00", "29.00")], 1.5, 8),
    "Jackets": ("JKT", LETTER_SIZES, LETTER_WEIGHTS, ["Black", "Khaki"],
                [("Bomber Jacket", "BMB", "110.00", "38.00"), ("Denim Jacket", "DNM", "98.00", "34.00")], 1.2, 8),
}

FIRST_NAMES = ["Aarav", "Maya", "Liam", "Sofia", "Noah", "Zara", "Ethan", "Priya", "Lucas", "Amara",
               "Mateo", "Ivy", "Omar", "Chloe", "Kenji", "Nadia", "Felix", "Rosa", "Hugo", "Leila"]
LAST_NAMES = ["Anderson", "Brooks", "Chen", "Dalton", "Ellis", "Foster", "Garcia", "Hayes", "Ibrahim", "Jensen",
              "Kapoor", "Lopez", "Morgan", "Nguyen", "Okafor", "Patel", "Quinn", "Reyes", "Stone", "Turner"]

CAMPAIGNS = [
    ("Denim Win-Back", "We miss you! Enjoy a little something off your next pair of jeans.", "15.00"),
    ("Summer Shirt Clearance", "Linen and Oxford shirts are marked down - stock up before they're gone.", "30.00"),
    ("Weekend Layering Sale", "Hoodies and jackets for the cooler days ahead.", "10.00"),
    ("Loyalty Thank-You", "A thank-you discount for our regulars on any full-price item.", "12.50"),
    ("New Arrivals Preview", "Be the first to see the new season. Early access discount inside.", "5.00"),
]


def sid(kind: str, key: str | int) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{kind}:{key}")


def _poisson(rng: random.Random, lam: float) -> int:
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _trend(category: str, days_ago: int) -> float:
    """Scenario 1: jeans demand slides from ~98% to 70% of normal across the latest 14 days."""
    if category == "Jeans" and days_ago <= RECENT_DAYS:
        return 1.0 - 0.3 * (RECENT_DAYS + 1 - days_ago) / RECENT_DAYS
    return 1.0


def build_products() -> list[dict]:
    """150 SKUs: 30 jeans, 30 t-shirts, 30 shirts, 20 hoodies, 20 dresses, 20 jackets."""
    rows = []
    for category, (code, sizes, _, colors, lines, _, _) in CATALOG.items():
        for line_name, line_code, price, cost in lines:
            for color in colors:
                for size in sizes:
                    sku = f"{code}-{line_code}-{color.replace(' ', '')[:3].upper()}-{size}"
                    rows.append({
                        "key": sku, "name": line_name, "category": category, "size": size, "color": color,
                        "sku": sku, "price": Decimal(price), "cost_price": Decimal(cost),
                    })
    return rows


def _stock(products: list[dict], rng: random.Random) -> dict[str, tuple[int, int]]:
    """sku -> (quantity, reorder_level). Scenarios 2 and 3 live here."""
    popular_jeans = sorted(p["sku"] for p in products if p["category"] == "Jeans" and p["size"] in POPULAR_JEAN_SIZES)
    sold_out = set(popular_jeans[:8])  # scenario 2: 8 SKUs at zero, the other 4 nearly gone
    stock = {}
    for p in products:
        reorder = CATALOG[p["category"]][6]
        if p["sku"] in sold_out:
            qty = 0
        elif p["sku"] in popular_jeans:
            qty = rng.randint(2, 5)
        elif p["name"] == SLOW_LINE:  # scenario 3: far more than the slow line can sell
            qty = rng.randint(320, 520)
        else:  # healthy: 2-5x the reorder level, so only the planted problems stand out
            qty = rng.randint(reorder * 2, reorder * 5)
        stock[p["sku"]] = (qty, reorder)
    return stock


def build_sales(products: list[dict], stock: dict[str, tuple[int, int]], as_of: date, rng: random.Random) -> list[dict]:
    by_category: dict[str, list[dict]] = {}
    for p in products:
        by_category.setdefault(p["category"], []).append(p)

    def weight(p: dict, days_ago: int) -> float:
        w = CATALOG[p["category"]][2][p["size"]] * (0.3 if p["name"] == SLOW_LINE else 1.0)
        if p["category"] == "Jeans" and days_ago <= STOCKOUT_DAYS and p["size"] in POPULAR_JEAN_SIZES:
            qty = stock[p["sku"]][0]
            w *= 0.0 if qty == 0 else 0.25  # sold-out SKUs can't sell; nearly-gone ones barely do
        return w

    sales = []
    for days_ago in range(HISTORY_DAYS, 0, -1):
        day = as_of - timedelta(days=days_ago)
        for category, items in by_category.items():
            weights = [weight(p, days_ago) for p in items]
            full = sum(CATALOG[category][2][p["size"]] * (0.3 if p["name"] == SLOW_LINE else 1.0) for p in items)
            availability = sum(weights) / full
            rate = CATALOG[category][5] * WEEKDAY_FACTOR[day.weekday()] * _trend(category, days_ago) * availability
            for _ in range(_poisson(rng, rate)):
                product = rng.choices(items, weights)[0]
                qty = rng.choices([1, 2, 3], [0.70, 0.22, 0.08])[0]
                sold_at = datetime.combine(day, time(10, 0), tzinfo=UTC) + timedelta(seconds=rng.randint(0, 11 * 3600))
                sales.append({
                    "key": len(sales), "sku": product["sku"], "quantity": qty,
                    "unit_price": product["price"], "total_amount": product["price"] * qty, "sold_at": sold_at,
                })
    return sales


def build_customers(as_of: date, rng: random.Random) -> list[dict]:
    rows = []
    for i in range(CUSTOMER_COUNT):
        first, last = FIRST_NAMES[i % 20], LAST_NAMES[(i * 7 + i // 20) % 20]  # 100 distinct pairs
        rows.append({
            "key": i, "name": f"{first} {last}",
            "email": f"{first}.{last}.{i:03d}@example.com".lower(),
            "phone": f"+1-202-555-{100 + i:04d}" if i % 4 else None,  # 555-0100..0199 is reserved for fiction
            "marketing_consent": rng.random() < 0.65,
            "created_at": datetime.combine(as_of - timedelta(days=rng.randint(1, 365)), time(9, 0), tzinfo=UTC),
        })
    return rows


def build_dataset(as_of: date) -> dict[str, list[dict]]:
    rng = random.Random(RNG_SEED)
    products = build_products()
    stock = _stock(products, rng)
    return {
        "products": products,
        "stock": [{"sku": sku, "quantity": q, "reorder_level": r} for sku, (q, r) in stock.items()],
        "sales": build_sales(products, stock, as_of, rng),
        "customers": build_customers(as_of, rng),
        "campaigns": [{"key": i, "name": n, "message": m, "discount_percent": Decimal(d)}
                      for i, (n, m, d) in enumerate(CAMPAIGNS)],
    }


async def _insert(session: AsyncSession, model, rows: list[dict]) -> None:
    for i in range(0, len(rows), CHUNK):
        await session.execute(insert(model).values(rows[i : i + CHUNK]).on_conflict_do_nothing())


async def seed(session: AsyncSession, as_of: date, *, reset_demo: bool = False) -> dict[str, int]:
    """Insert the demo data in one transaction. Returns the number of rows in each table afterwards."""
    data = build_dataset(as_of)
    created_at = datetime.combine(as_of - timedelta(days=120), time(9, 0), tzinfo=UTC)
    product_ids = {p["sku"]: sid("product", p["key"]) for p in data["products"]}
    demo_product_ids = list(product_ids.values())

    if reset_demo:  # only rows tied to the demo catalogue / fixed demo IDs are touched
        await session.execute(delete(Sale).where(Sale.product_id.in_(demo_product_ids)))
        await session.execute(delete(Product).where(Product.id.in_(demo_product_ids)))  # inventory cascades
        await session.execute(delete(Customer).where(Customer.id.in_([sid("customer", c["key"]) for c in data["customers"]])))
        await session.execute(delete(Campaign).where(Campaign.id.in_([sid("campaign", c["key"]) for c in data["campaigns"]])))

    await _insert(session, Product, [
        {"id": product_ids[p["sku"]], "name": p["name"], "category": p["category"], "size": p["size"],
         "color": p["color"], "sku": p["sku"], "price": p["price"], "cost_price": p["cost_price"],
         "created_at": created_at}
        for p in data["products"]])
    await _insert(session, Inventory, [
        {"id": sid("inventory", s["sku"]), "product_id": product_ids[s["sku"]], "quantity": s["quantity"],
         "reorder_level": s["reorder_level"], "updated_at": datetime.combine(as_of, time(8, 0), tzinfo=UTC)}
        for s in data["stock"]])
    # Sales are dated relative to --as-of, so topping up an older run with a different date would mix two
    # timelines. Only add them when the demo products have none yet (same --as-of re-runs are then a no-op).
    has_demo_sales = await session.scalar(
        select(func.count()).select_from(Sale).where(Sale.product_id.in_(demo_product_ids)))
    if not has_demo_sales:
        await _insert(session, Sale, [
            {"id": sid("sale", s["key"]), "product_id": product_ids[s["sku"]], "quantity": s["quantity"],
             "unit_price": s["unit_price"], "total_amount": s["total_amount"], "sold_at": s["sold_at"]}
            for s in data["sales"]])
    await _insert(session, Customer, [
        {"id": sid("customer", c["key"]), **{k: c[k] for k in ("name", "email", "phone", "marketing_consent", "created_at")}}
        for c in data["customers"]])
    await _insert(session, Campaign, [
        {"id": sid("campaign", c["key"]), "name": c["name"], "message": c["message"],
         "discount_percent": c["discount_percent"], "status": "draft",
         "created_at": datetime.combine(as_of - timedelta(days=c["key"] + 1), time(11, 0), tzinfo=UTC)}
        for c in data["campaigns"]])
    await session.commit()

    return {model.__tablename__: await session.scalar(select(func.count()).select_from(model))
            for model in (Product, Inventory, Sale, Customer, Campaign)}


async def main(as_of: date, reset_demo: bool) -> None:
    async with SessionLocal() as session:
        counts = await seed(session, as_of, reset_demo=reset_demo)
    await engine.dispose()
    print(f"Seeded demo data as of {as_of} - rows per table: " + ", ".join(f"{t}={n}" for t, n in counts.items()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", type=date.fromisoformat, default=datetime.now(UTC).date(),
                        help="treat this UTC date as 'today' (default: today)")
    parser.add_argument("--reset-demo", action="store_true", help="delete existing demo rows first, then re-seed")
    args = parser.parse_args()
    asyncio.run(main(args.as_of, args.reset_demo))

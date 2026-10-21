import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.ai.tools.sales import get_sales_trends


def at(day: str, clock: str = "12:00:00") -> datetime:
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=UTC)


pytestmark = pytest.mark.usefixtures("tool_session")


async def test_ainvoke_returns_json_with_gaps_filled_and_inclusive_days(make_product, add_sale):
    product = await make_product(price="10.00")
    await add_sale(product, at("2025-03-03", "00:00:00"), quantity=2)  # first instant of start_date
    await add_sale(product, at("2025-03-05", "23:59:59"), quantity=4)  # last second of end_date
    await add_sale(product, at("2025-03-06", "00:00:00"))  # next day: excluded

    raw = await get_sales_trends.ainvoke({"start_date": "2025-03-03", "end_date": "2025-03-05"})

    body = json.loads(raw)
    assert body["granularity"] == "day"
    assert [(p["period"], p["revenue"], p["units"], p["sale_count"]) for p in body["points"]] == [
        ("2025-03-03", "20.00", 2, 1), ("2025-03-04", "0.00", 0, 0), ("2025-03-05", "40.00", 4, 1),
    ]


async def test_category_filter_is_applied(make_product, add_sale):
    jeans = await make_product(category="Jeans")
    tees = await make_product(category="T-Shirts")
    await add_sale(jeans, at("2025-03-03"), quantity=3)
    await add_sale(tees, at("2025-03-03"), quantity=7)

    raw = await get_sales_trends.ainvoke({"start_date": "2025-03-03", "end_date": "2025-03-03", "category": "jeans"})

    assert [p["units"] for p in json.loads(raw)["points"]] == [3]


async def test_no_sales_in_range_yields_zero_points():
    raw = await get_sales_trends.ainvoke({"start_date": "2025-03-03", "end_date": "2025-03-04"})
    assert [p["sale_count"] for p in json.loads(raw)["points"]] == [0, 0]


@pytest.mark.parametrize(
    "args",
    [
        {"start_date": "not-a-date", "end_date": "2025-03-05"},
        {"start_date": "2025-02-30", "end_date": "2025-03-05"},
        {"start_date": "1999-12-31", "end_date": "2025-03-05"},
        {"start_date": "2025-03-05", "end_date": "2100-01-02"},
        {"start_date": "2025-03-06", "end_date": "2025-03-05"},
        {"start_date": "2025-03-05"},
        {"start_date": "2025-03-05", "end_date": "2025-03-05", "category": "bad\x00"},
        {"start_date": "2025-03-05", "end_date": "2025-03-05", "category": "x" * 101},
    ],
)
async def test_invalid_input_is_rejected(args):
    with pytest.raises(ValidationError):
        await get_sales_trends.ainvoke(args)


async def test_date_bounds_are_inclusive():
    raw = await get_sales_trends.ainvoke({"start_date": "2000-01-01", "end_date": "2000-01-02"})
    assert len(json.loads(raw)["points"]) == 2


def test_schema_exposes_iso_dates_to_the_model():
    props = get_sales_trends.get_input_schema().model_json_schema()["properties"]
    assert props["start_date"]["format"] == "date"
    assert props["end_date"]["format"] == "date"
    assert "category" in props


async def test_range_too_long_returns_message_instead_of_raising():
    result = await get_sales_trends.ainvoke({"start_date": "2020-01-01", "end_date": "2025-01-01"})
    assert "narrow it" in result
    assert "1000" in result

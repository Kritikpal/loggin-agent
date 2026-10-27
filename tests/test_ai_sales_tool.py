import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.ai.tools.sales import compare_sales_periods, get_sales_summary, get_sales_trends


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


async def test_summary_totals_and_per_category_breakdown(make_product, add_sale):
    jeans = await make_product(category="Jeans", price="50.00")
    tees = await make_product(category="T-Shirts", price="10.00")
    await add_sale(jeans, at("2025-03-03"), quantity=2)
    await add_sale(tees, at("2025-03-04"), quantity=3)
    await add_sale(tees, at("2025-03-06"))  # after end_date: excluded

    body = json.loads(await get_sales_summary.ainvoke({"start_date": "2025-03-03", "end_date": "2025-03-05"}))

    assert (body["total_revenue"], body["total_units"], body["sale_count"], body["average_sale_value"]) == (
        "130.00", 5, 2, "65.00"
    )
    assert [(c["category"], c["revenue"]) for c in body["by_category"]] == [("Jeans", "100.00"), ("T-Shirts", "30.00")]


async def test_summary_category_filter_and_empty_range(make_product, add_sale):
    jeans = await make_product(category="Jeans", price="50.00")
    await add_sale(jeans, at("2025-03-03"))

    only_tees = json.loads(await get_sales_summary.ainvoke(
        {"start_date": "2025-03-03", "end_date": "2025-03-03", "category": "t-shirts"}
    ))

    assert (only_tees["total_revenue"], only_tees["sale_count"], only_tees["by_category"]) == ("0", 0, [])


async def test_summary_rejects_reversed_dates():
    with pytest.raises(ValidationError):
        await get_sales_summary.ainvoke({"start_date": "2025-03-06", "end_date": "2025-03-05"})


WEEKS = {"a_start": "2025-03-03", "a_end": "2025-03-09", "b_start": "2025-03-10", "b_end": "2025-03-16"}


async def test_compare_sales_periods_reports_both_and_change(make_product, add_sale):
    p = await make_product(price="10.00")
    await add_sale(p, at("2025-03-03"), quantity=4)  # A: 40.00
    await add_sale(p, at("2025-03-10"), quantity=3)  # B: 30.00

    body = json.loads(await compare_sales_periods.ainvoke(WEEKS))

    assert (body["period_a"]["revenue"], body["period_b"]["revenue"]) == ("40.00", "30.00")
    assert (body["period_a"]["units"], body["period_b"]["units"]) == (4, 3)
    assert (body["revenue_change"], body["revenue_change_percent"]) == ("-10.00", "-25.00")


async def test_compare_sales_periods_zero_base_has_null_percent(make_product, add_sale):
    p = await make_product(price="10.00")
    await add_sale(p, at("2025-03-10"))

    body = json.loads(await compare_sales_periods.ainvoke(WEEKS))

    assert body["revenue_change_percent"] is None


async def test_compare_sales_periods_rejects_a_reversed_period():
    with pytest.raises(ValidationError):
        await compare_sales_periods.ainvoke({**WEEKS, "a_start": "2025-03-10"})


async def test_summary_rows_carry_share_percent(make_product, add_sale):
    jeans = await make_product(category="Jeans", price="30.00")
    tees = await make_product(category="T-Shirts", price="10.00")
    await add_sale(jeans, at("2025-03-03"))
    await add_sale(tees, at("2025-03-03"))

    raw = await get_sales_summary.ainvoke({"start_date": "2025-03-03", "end_date": "2025-03-03"})

    rows = json.loads(raw)["by_category"]
    assert {r["category"]: r["share_percent"] for r in rows} == {"Jeans": "75.00", "T-Shirts": "25.00"}


async def test_summary_keeps_non_ascii_text_so_the_injection_check_sees_it(make_product, add_sale):
    p = await make_product(category="Ｊｅａｎｓ​")  # fullwidth letters and a zero-width space
    await add_sale(p, at("2025-03-03"))

    raw = await get_sales_summary.ainvoke({"start_date": "2025-03-03", "end_date": "2025-03-03"})

    assert "Ｊｅａｎｓ​" in raw  # escaped "\uff2a..." would slip past injection()

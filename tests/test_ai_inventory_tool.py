import json

import pytest
from pydantic import ValidationError

from app.ai.tools.inventory import MAX_ITEMS, get_inventory_status

pytestmark = pytest.mark.usefixtures("tool_session")


async def run(**args) -> dict:
    return json.loads(await get_inventory_status.ainvoke(args))


async def test_returns_structured_status_for_each_sku(make_product):
    await make_product(sku="OUT", quantity=0, reorder_level=5)
    await make_product(sku="LOW", quantity=5, reorder_level=5)  # at the reorder level counts as low
    await make_product(sku="OK", quantity=6, reorder_level=5)

    body = await run()

    assert {i["sku"]: (i["quantity"], i["reorder_level"], i["stock_status"]) for i in body["items"]} == {
        "OUT": (0, 5, "out_of_stock"), "LOW": (5, 5, "low_stock"), "OK": (6, 5, "in_stock"),
    }
    assert (body["total_matching"], body["returned"], body["truncated"]) == (3, 3, False)
    assert {"product_name", "category", "size", "color"} <= set(body["items"][0])
    assert body["as_of"]


async def test_low_stock_only_lists_most_urgent_first(make_product):
    await make_product(sku="OK", quantity=50, reorder_level=5)
    await make_product(sku="LOW", quantity=4, reorder_level=5)
    await make_product(sku="OUT", quantity=0, reorder_level=5)

    body = await run(low_stock_only=True)

    assert [i["sku"] for i in body["items"]] == ["OUT", "LOW"]


async def test_category_filter_is_case_insensitive(make_product):
    await make_product(category="Jeans", sku="JN")
    await make_product(category="T-Shirts", sku="TS")

    assert [i["sku"] for i in (await run(category="jEaNs"))["items"]] == ["JN"]
    assert (await run(category="Hats"))["items"] == []


async def test_output_is_capped_and_reports_truncation(make_product):
    for _ in range(MAX_ITEMS + 1):
        await make_product(quantity=1, reorder_level=5)

    body = await run(low_stock_only=True)

    assert (body["returned"], body["total_matching"], body["truncated"]) == (MAX_ITEMS, MAX_ITEMS + 1, True)


@pytest.mark.parametrize("args", [{"category": "bad\x00"}, {"category": "x" * 101}, {"low_stock_only": "maybe"}])
async def test_invalid_input_is_rejected(args):
    with pytest.raises(ValidationError):
        await get_inventory_status.ainvoke(args)


def test_schema_exposes_optional_filters_to_the_model():
    schema = get_inventory_status.get_input_schema().model_json_schema()
    assert schema.get("required", []) == []
    assert {"category", "low_stock_only"} <= set(schema["properties"])

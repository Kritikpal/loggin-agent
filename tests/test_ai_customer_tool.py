import json

import pytest
from pydantic import ValidationError

from app.ai.tools.customers import MAX_CUSTOMERS, find_customers
from app.models import Customer

pytestmark = pytest.mark.usefixtures("tool_session")


async def run(**args) -> dict:
    return json.loads(await find_customers.ainvoke(args))


@pytest.fixture
def add_customers(session):
    async def _add(*rows: tuple[str, str | None, bool]) -> None:
        session.add_all(
            Customer(name=n, email=e, phone="+1 202 555 0100", marketing_consent=c) for n, e, c in rows
        )
        await session.commit()

    return _add


async def test_filters_by_consent_and_search_without_leaking_contact_details(add_customers):
    await add_customers(("Maya Brooks", "maya@example.com", True), ("Ali Khan", "ali@shop.io", False),
                        ("Zoe Park", None, True))

    consenting = await run(marketing_consent=True)
    by_email = await run(search="SHOP.IO")

    assert [c["name"] for c in consenting["customers"]] == ["Maya Brooks", "Zoe Park"]
    assert consenting["total_matching"] == 2 and consenting["truncated"] is False
    assert [c["name"] for c in by_email["customers"]] == ["Ali Khan"]
    assert set(consenting["customers"][0]) == {"name", "marketing_consent", "customer_since"}
    raw = await find_customers.ainvoke({})
    assert "@" not in raw and "555" not in raw  # no email or phone ever reaches the model


async def test_output_is_capped_but_reports_the_true_total(add_customers):
    await add_customers(*((f"C{i:03}", None, False) for i in range(MAX_CUSTOMERS + 1)))

    body = await run()

    assert (body["returned"], body["total_matching"], body["truncated"]) == (MAX_CUSTOMERS, MAX_CUSTOMERS + 1, True)


async def test_no_match_is_an_empty_list():
    assert await run(search="nobody") == {"total_matching": 0, "returned": 0, "truncated": False, "customers": []}


@pytest.mark.parametrize("args", [{"search": "x" * 101}, {"search": "bad\x00"}, {"marketing_consent": "maybe"}])
async def test_invalid_input_is_rejected(args):
    with pytest.raises(ValidationError):
        await find_customers.ainvoke(args)

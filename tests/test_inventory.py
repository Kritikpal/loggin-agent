import uuid


async def test_list_and_get_inventory(client, make_product):
    jeans = await make_product(category="Jeans", quantity=12, reorder_level=5)
    await make_product(category="Shirts", quantity=3, reorder_level=5)

    listing = (await client.get("/api/v1/inventory")).json()
    assert listing["total"] == 2
    only_jeans = (await client.get("/api/v1/inventory", params={"category": "jeans"})).json()
    assert [i["product_id"] for i in only_jeans["items"]] == [str(jeans.id)]

    one = (await client.get(f"/api/v1/inventory/{jeans.id}")).json()
    assert one["quantity"] == 12 and one["is_low_stock"] is False and one["sku"] == jeans.sku


async def test_low_stock_returns_items_at_or_below_reorder_level_worst_first(client, make_product):
    healthy = await make_product(quantity=20, reorder_level=5)
    at_limit = await make_product(quantity=5, reorder_level=5)  # boundary counts as low
    out = await make_product(quantity=0, reorder_level=5)
    low_shirt = await make_product(category="Shirts", quantity=1, reorder_level=5)

    body = (await client.get("/api/v1/inventory/low-stock")).json()
    ids = [i["product_id"] for i in body["items"]]
    assert body["total"] == 3
    assert ids == [str(out.id), str(low_shirt.id), str(at_limit.id)]  # shortfall: -5, -4, 0
    assert str(healthy.id) not in ids
    assert all(i["is_low_stock"] for i in body["items"])

    shirts_only = (await client.get("/api/v1/inventory/low-stock", params={"category": "Shirts"})).json()
    assert [i["product_id"] for i in shirts_only["items"]] == [str(low_shirt.id)]


async def test_patch_inventory(client, make_product):
    product = await make_product(quantity=2, reorder_level=5)
    url = f"/api/v1/inventory/{product.id}"

    resp = await client.patch(url, json={"quantity": 50})
    assert resp.status_code == 200
    assert resp.json()["quantity"] == 50 and resp.json()["reorder_level"] == 5  # unspecified field kept
    assert resp.json()["is_low_stock"] is False

    resp = await client.patch(url, json={"reorder_level": 60})
    assert resp.json()["is_low_stock"] is True


async def test_invalid_inventory_requests(client, make_product):
    product = await make_product()
    url = f"/api/v1/inventory/{product.id}"
    for bad in ({}, {"quantity": -1}, {"reorder_level": -3}, {"quantity": "abc"}, {"bogus": 1}):
        resp = await client.patch(url, json=bad)
        assert resp.status_code == 422, bad
        assert resp.json()["error"]["code"] == "validation_error"
    missing = str(uuid.uuid4())
    assert (await client.get(f"/api/v1/inventory/{missing}")).status_code == 404
    assert (await client.patch(f"/api/v1/inventory/{missing}", json={"quantity": 1})).status_code == 404

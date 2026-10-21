import uuid

PAYLOAD = {
    "name": "Slim Fit Jeans", "category": "Jeans", "size": "32", "color": "Indigo",
    "sku": "JNS-1", "price": "89.00", "cost_price": "31.00",
}


async def test_create_product_also_creates_inventory(client):
    resp = await client.post("/api/v1/products", json={**PAYLOAD, "initial_quantity": 7, "reorder_level": 3})
    assert resp.status_code == 201
    body = resp.json()
    assert body["sku"] == "JNS-1" and body["price"] == "89.00"
    inv = await client.get(f"/api/v1/inventory/{body['id']}")
    assert inv.json()["quantity"] == 7 and inv.json()["reorder_level"] == 3


async def test_get_and_patch_product(client):
    created = (await client.post("/api/v1/products", json=PAYLOAD)).json()
    assert (await client.get(f"/api/v1/products/{created['id']}")).json()["name"] == "Slim Fit Jeans"

    patched = await client.patch(f"/api/v1/products/{created['id']}", json={"price": "79.50", "color": "Black"})
    assert patched.status_code == 200
    assert patched.json()["price"] == "79.50" and patched.json()["color"] == "Black"
    assert patched.json()["sku"] == "JNS-1"  # untouched


async def test_duplicate_sku_is_rejected_with_conflict_envelope(client):
    await client.post("/api/v1/products", json=PAYLOAD)
    resp = await client.post("/api/v1/products", json=PAYLOAD)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


async def test_unknown_product_returns_404_envelope(client):
    resp = await client.get(f"/api/v1/products/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert (await client.patch(f"/api/v1/products/{uuid.uuid4()}", json={"name": "x"})).status_code == 404


async def test_list_filters_search_and_pagination(client):
    items = [
        ("Slim Jeans", "Jeans", "32", "Indigo", "A-1"), ("Slim Jeans", "Jeans", "34", "Black", "A-2"),
        ("Crew Tee", "T-Shirts", "M", "White", "B-1"), ("Crew Tee", "T-Shirts", "L", "White", "B_2"),
    ]
    for name, category, size, color, sku in items:
        await client.post("/api/v1/products", json={**PAYLOAD, "name": name, "category": category,
                                                    "size": size, "color": color, "sku": sku})

    def skus(resp):
        return sorted(p["sku"] for p in resp.json()["items"])

    assert skus(await client.get("/api/v1/products", params={"category": "jeans"})) == ["A-1", "A-2"]
    assert skus(await client.get("/api/v1/products", params={"size": "m"})) == ["B-1"]
    assert skus(await client.get("/api/v1/products", params={"color": "WHITE"})) == ["B-1", "B_2"]
    assert skus(await client.get("/api/v1/products", params={"search": "crew"})) == ["B-1", "B_2"]
    assert skus(await client.get("/api/v1/products", params={"category": "Jeans", "color": "Black"})) == ["A-2"]
    # LIKE wildcards in the search term are literal, not wildcards
    assert skus(await client.get("/api/v1/products", params={"search": "B_2"})) == ["B_2"]
    assert (await client.get("/api/v1/products", params={"search": "%"})).json()["total"] == 0

    page = (await client.get("/api/v1/products", params={"page": 2, "page_size": 3})).json()
    assert page["total"] == 4 and len(page["items"]) == 1 and page["page"] == 2


async def test_invalid_product_payloads_return_validation_envelope(client):
    bad_payloads = [
        {**PAYLOAD, "price": "-1"},
        {**PAYLOAD, "price": "0"},
        {**PAYLOAD, "price": "10.999"},
        {**PAYLOAD, "cost_price": "-0.01"},
        {**PAYLOAD, "name": "   "},
        {**PAYLOAD, "initial_quantity": -1},
        {**PAYLOAD, "unexpected": 1},
        {k: v for k, v in PAYLOAD.items() if k != "sku"},
    ]
    for payload in bad_payloads:
        resp = await client.post("/api/v1/products", json=payload)
        assert resp.status_code == 422, payload
        assert resp.json()["error"]["code"] == "validation_error"
        assert resp.json()["error"]["details"]


async def test_invalid_product_patch_and_query_params(client):
    created = (await client.post("/api/v1/products", json=PAYLOAD)).json()
    url = f"/api/v1/products/{created['id']}"
    assert (await client.patch(url, json={})).status_code == 422  # nothing to update
    assert (await client.patch(url, json={"sku": "NEW"})).status_code == 422  # SKU is immutable
    assert (await client.patch(url, json={"price": "-5"})).status_code == 422
    assert (await client.get("/api/v1/products/not-a-uuid")).status_code == 422
    assert (await client.get("/api/v1/products", params={"page": 0})).status_code == 422
    assert (await client.get("/api/v1/products", params={"page_size": 1000})).status_code == 422

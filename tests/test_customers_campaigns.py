import uuid

# ------------------------------------------------------------------ customers


async def test_create_and_list_customers(client):
    resp = await client.post("/api/v1/customers", json={
        "name": "  Maya Brooks ", "phone": "+1-202-555-0143", "email": "Maya@Example.com", "marketing_consent": True,
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Maya Brooks" and body["email"] == "maya@example.com" and body["marketing_consent"] is True

    minimal = (await client.post("/api/v1/customers", json={"name": "Walk-in"})).json()
    assert minimal["phone"] is None and minimal["email"] is None and minimal["marketing_consent"] is False

    everyone = (await client.get("/api/v1/customers")).json()
    assert everyone["total"] == 2
    consenting = (await client.get("/api/v1/customers", params={"marketing_consent": True})).json()
    assert [c["name"] for c in consenting["items"]] == ["Maya Brooks"]
    assert (await client.get("/api/v1/customers", params={"search": "maya@"})).json()["total"] == 1
    assert (await client.get("/api/v1/customers", params={"page_size": 1})).json()["items"].__len__() == 1


async def test_duplicate_customer_email_conflicts_case_insensitively(client):
    await client.post("/api/v1/customers", json={"name": "A", "email": "dup@example.com"})
    resp = await client.post("/api/v1/customers", json={"name": "B", "email": "DUP@example.com"})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "conflict"


async def test_invalid_customers_are_rejected(client):
    for bad in (
        {}, {"name": ""}, {"name": "x", "email": "not-an-email"}, {"name": "x", "phone": "abc"},
        {"name": "x", "phone": "1"}, {"name": "x" * 151}, {"name": "x", "marketing_consent": "maybe"},
        {"name": "x", "id": str(uuid.uuid4())},
    ):
        resp = await client.post("/api/v1/customers", json=bad)
        assert resp.status_code == 422, bad
        assert resp.json()["error"]["code"] == "validation_error"


# ------------------------------------------------------------------ campaigns


async def test_campaign_creation_only_persists_a_draft(client):
    resp = await client.post("/api/v1/campaigns", json={
        "name": "Denim Win-Back", "message": "Come back!", "discount_percent": "15.50",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "draft" and body["discount_percent"] == "15.50"
    assert body["category"] is None  # optional
    with_category = await client.post("/api/v1/campaigns", json={
        "name": "Denim", "message": "m", "category": " Jeans ", "discount_percent": "5",
    })
    assert with_category.json()["category"] == "Jeans"

    fetched = (await client.get(f"/api/v1/campaigns/{body['id']}")).json()
    assert fetched == body
    # a client cannot create a campaign in any other state
    forced = await client.post("/api/v1/campaigns", json={
        "name": "Sneaky", "message": "x", "discount_percent": "5", "status": "active",
    })
    assert forced.status_code == 422


async def test_list_campaigns_with_status_filter(client):
    for i in range(3):
        await client.post("/api/v1/campaigns", json={"name": f"C{i}", "message": "m", "discount_percent": "10"})
    listing = (await client.get("/api/v1/campaigns")).json()
    assert listing["total"] == 3 and listing["items"][0]["name"] == "C2"  # newest first
    assert (await client.get("/api/v1/campaigns", params={"status": "draft"})).json()["total"] == 3
    assert (await client.get("/api/v1/campaigns", params={"status": "active"})).json()["total"] == 0
    assert (await client.get("/api/v1/campaigns", params={"status": "bogus"})).status_code == 422
    assert (await client.get("/api/v1/campaigns", params={"page_size": 2})).json()["items"].__len__() == 2


async def test_invalid_campaigns_are_rejected(client):
    base = {"name": "N", "message": "m", "discount_percent": "10"}
    for bad in (
        {**base, "discount_percent": "-1"}, {**base, "discount_percent": "100.01"}, {**base, "discount_percent": "10.123"},
        {**base, "discount_percent": "abc"}, {**base, "name": ""}, {**base, "message": "  "},
        {k: v for k, v in base.items() if k != "message"},
    ):
        resp = await client.post("/api/v1/campaigns", json=bad)
        assert resp.status_code == 422, bad
        assert resp.json()["error"]["code"] == "validation_error"
    for ok in ("0", "100", "99.99"):
        assert (await client.post("/api/v1/campaigns", json={**base, "discount_percent": ok})).status_code == 201


async def test_unknown_campaign_and_route_use_the_error_envelope(client):
    missing = await client.get(f"/api/v1/campaigns/{uuid.uuid4()}")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert (await client.get("/api/v1/campaigns/not-a-uuid")).status_code == 422
    nowhere = await client.get("/api/v1/does-not-exist")
    assert nowhere.status_code == 404 and nowhere.json()["error"]["code"] == "not_found"
    assert (await client.delete("/api/v1/products")).status_code == 405

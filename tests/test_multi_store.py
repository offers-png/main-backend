"""Phase 1: one owner, several stores, no data leaking between them."""

from conftest import as_user

OWNER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
MEMBER = "33333333-3333-3333-3333-333333333333"


def make_first_store(client, user=OWNER, name="Store A"):
    r = client.post("/api/setup/business", json={"businessName": name, "businessAddress": "1 Main St", "ownerName": "Sam"}, headers=as_user(user))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def finish_setup(client, user, store_id):
    r = client.post("/api/setup/accountant", json={"accountantEmail": "cpa@example.com", "sendFrequency": "monthly", "sendDay": 1}, headers=as_user(user, store_id))
    assert r.status_code == 200, r.text


def add_store(client, name, user=OWNER):
    r = client.post("/api/stores", json={"businessName": name, "businessAddress": f"{name} Ave"}, headers=as_user(user))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def money_in(client, store_id, amount, user=OWNER):
    r = client.post("/api/money-entries", json={"entryDate": "2026-10-05", "entryType": "income", "category": "sales_cash", "amount": amount, "paymentMethod": "cash"}, headers=as_user(user, store_id))
    assert r.status_code == 200, r.text


def test_owner_creates_three_stores_and_data_never_leaks(client):
    a = make_first_store(client)
    b = add_store(client, "Store B")
    c = add_store(client, "Store C")
    for s in (a, b, c):
        finish_setup(client, OWNER, s)

    stores = client.get("/api/stores", headers=as_user(OWNER)).json()
    assert [s["businessName"] for s in stores] == ["Store A", "Store B", "Store C"]

    money_in(client, a, 100)
    money_in(client, b, 250)
    money_in(client, b, 50)

    def entries(store):
        return client.get("/api/money-entries", headers=as_user(OWNER, store)).json()

    assert [e["amount"] for e in entries(a)] == [100]
    assert sorted(e["amount"] for e in entries(b)) == [50, 250]
    assert entries(c) == []

    # mileage + inventory follow the active store too
    client.post("/api/mileage", json={"miles": 12, "date": "2026-10-01"}, headers=as_user(OWNER, c))
    assert client.get("/api/mileage", headers=as_user(OWNER, c)).json()["totalMiles"] == 12
    assert client.get("/api/mileage", headers=as_user(OWNER, a)).json()["totalMiles"] == 0
    client.post("/api/inventory", json={"name": "Soda", "quantity": 3}, headers=as_user(OWNER, b))
    assert len(client.get("/api/inventory", headers=as_user(OWNER, b)).json()) == 1
    assert client.get("/api/inventory", headers=as_user(OWNER, a)).json() == []

    # /api/business reflects the active store
    assert client.get("/api/business", headers=as_user(OWNER, b)).json()["businessName"] == "Store B"
    assert client.get("/api/business", headers=as_user(OWNER, b)).json()["storeCount"] == 3

    # summary lists every store with its own totals
    summary = client.get("/api/stores/summary?start=2026-10-01&end=2026-10-31", headers=as_user(OWNER)).json()
    by_name = {s["businessName"]: s for s in summary["stores"]}
    assert by_name["Store A"]["totalIn"] == 100
    assert by_name["Store B"]["totalIn"] == 300
    assert by_name["Store C"]["totalIn"] == 0
    assert summary["totals"]["totalIn"] == 400


def test_cannot_use_someone_elses_store(client):
    a = make_first_store(client)
    make_first_store(client, user=OTHER, name="Other Shop")
    r = client.get("/api/money-entries", headers=as_user(OTHER, a))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "store_access_denied"
    r = client.post("/api/money-entries", json={"entryDate": "2026-10-05", "entryType": "income", "category": "sales_cash", "amount": 5}, headers=as_user(OTHER, a))
    assert r.status_code == 403


def test_existing_single_store_account_works_without_header(client, db):
    a = make_first_store(client)
    finish_setup(client, OWNER, a)
    money_in(client, a, 42)
    # No X-Business-Id at all (old app build) -> first owned store
    r = client.get("/api/money-entries", headers=as_user(OWNER))
    assert [e["amount"] for e in r.json()] == [42]
    assert client.get("/api/business", headers=as_user(OWNER)).json()["id"] == a


def test_team_member_scoped_to_their_store(client, db):
    a = make_first_store(client)
    b = add_store(client, "Store B")
    db.tables.setdefault("business_users", []).append({
        "id": "m1", "business_id": b, "user_id": MEMBER, "email": "m@test.com",
        "role": "employee", "status": "active", "modules": ["money_box"],
    })
    money_in(client, b, 10, user=MEMBER)
    assert client.get("/api/business", headers=as_user(MEMBER)).json()["id"] == b
    assert client.get("/api/money-entries", headers=as_user(MEMBER, a)).status_code == 403
    assert [s["id"] for s in client.get("/api/stores", headers=as_user(MEMBER)).json()] == [b]
    # employee can't add stores
    r = client.post("/api/stores", json={"businessName": "X", "businessAddress": "Y"}, headers=as_user(MEMBER))
    assert r.status_code == 400


def test_team_invites_go_to_the_active_store(client, db):
    a = make_first_store(client)
    b = add_store(client, "Store B")
    r = client.post("/api/team/invite", json={"email": "new@test.com", "role": "employee", "modules": ["receipts"]}, headers=as_user(OWNER, b))
    assert r.status_code == 200, r.text
    assert r.json()["businessId"] == b
    assert client.get("/api/team", headers=as_user(OWNER, a)).json() == []
    assert len(client.get("/api/team", headers=as_user(OWNER, b)).json()) == 1

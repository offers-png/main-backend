"""Settings -> Export my data / Delete my account."""

import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from conftest import as_user
from test_multi_store import OWNER, OTHER, MEMBER, make_first_store, finish_setup, paid_owner  # noqa: F401


def seed(client, db, paid_owner):
    db.unique["rv_bank_transactions"] = [("business_id", "dedupe_key")]
    a = make_first_store(client)
    b = client.post("/api/stores", json={"businessName": "Store B", "businessAddress": "x"}, headers=as_user(OWNER)).json()["id"]
    for s in (a, b):
        finish_setup(client, OWNER, s)
    r = client.post("/api/receipts", json={"imageBase64": "aGVsbG8=", "filename": "r.jpg"}, headers=as_user(OWNER, a)).json()
    client.post("/api/money-entries", json={"entryDate": "2026-10-02", "entryType": "income", "category": "sales_cash", "amount": 50}, headers=as_user(OWNER, b))
    inv = client.post("/api/invoices", json={"customerName": "Joe", "items": [{"description": "Sandwiches", "quantity": 2, "unitPrice": 5}]}, headers=as_user(OWNER, a))
    assert inv.status_code == 200, inv.text
    db.tables.setdefault("rv_bank_transactions", []).append({"id": "t1", "business_id": a, "description": "SYSCO", "amount": -20, "status": "needs_receipt", "posted_date": "2026-10-03", "dedupe_key": "k"})
    db.tables.setdefault("rv_dropbox_tokens", []).append({"id": "d1", "business_id": a, "token": "SECRET-TOKEN", "uploaded_files": []})
    db.tables.setdefault("rv_inbound_addresses", []).append({"id": "i1", "business_id": a, "token": "abcdefgh2345"})
    db.tables.setdefault("business_users", []).append({"id": "m1", "business_id": a, "user_id": MEMBER, "email": "m@test.com", "role": "employee", "status": "active", "modules": ["money_box"]})
    # someone else's store must survive everything
    o = make_first_store(client, user=OTHER, name="Other Shop")
    client.post("/api/money-entries", json={"entryDate": "2026-10-02", "entryType": "income", "category": "sales_cash", "amount": 7}, headers=as_user(OTHER, o))
    return a, b, o, r


def test_export_zip(client, db, paid_owner):
    a, b, o, r = seed(client, db, paid_owner)
    res = client.get("/api/account/export", headers=as_user(OWNER))
    assert res.status_code == 200 and res.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(res.content))
    names = z.namelist()
    assert "README.txt" in names and "account.json" in names
    assert json.loads(z.read("account.json"))["user"]["id"] == OWNER
    folders = {n.split("/")[1] for n in names if n.startswith("stores/")}
    assert len(folders) == 2
    a_folder = next(f for f in folders if f.endswith(a[:8]))
    store = json.loads(z.read(f"stores/{a_folder}/store.json"))
    assert store["store"]["business_name"] == "Store A"
    assert len(store["receipts"]) == 1 and len(store["invoices"]) == 1 and len(store["invoice_items"]) == 1
    assert len(store["rv_bank_transactions"]) == 1
    blob = z.read(f"stores/{a_folder}/store.json").decode()
    assert "SECRET-TOKEN" not in blob and "r-abcdefgh2345@" in blob
    assert f"stores/{a_folder}/expenses.csv" in names and f"stores/{a_folder}/receipts.csv" in names
    assert "Other Shop" not in "".join(z.read(n).decode(errors="ignore") for n in names)


def test_team_member_export_is_their_own_entries(client, db, paid_owner):
    a, b, o, r = seed(client, db, paid_owner)
    client.post("/api/money-entries", json={"entryDate": "2026-10-04", "entryType": "income", "category": "sales_cash", "amount": 3}, headers=as_user(MEMBER, a))
    z = zipfile.ZipFile(io.BytesIO(client.get("/api/account/export", headers=as_user(MEMBER)).content))
    assert not any(n.startswith("stores/") for n in z.namelist())
    mine = json.loads(z.read(f"my-entries/store-{a[:8]}/my-entries.json"))
    assert [e["amount"] for e in mine["rv_money_entries"]] == [3] and mine["receipts"] == []


def test_delete_needs_confirmation(client, db, paid_owner):
    seed(client, db, paid_owner)
    assert client.request("DELETE", "/api/account", json={"confirm": "yes"}, headers=as_user(OWNER)).status_code == 400
    assert client.request("DELETE", "/api/account", headers=as_user(OWNER)).status_code == 422
    assert len([x for x in db.tables["businesses"] if x["user_id"] == OWNER]) == 2


def test_owner_delete_removes_everything_of_theirs_only(client, db, paid_owner, http_calls, monkeypatch):
    a, b, o, r = seed(client, db, paid_owner)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    files_before = len(db.storage_files)
    assert files_before >= 1

    res = client.request("DELETE", "/api/account", json={"confirm": "DELETE"}, headers=as_user(OWNER))
    assert res.status_code == 200, res.text
    assert res.json()["storesDeleted"] == 2 and res.json()["loginDeleted"] is False
    # Stripe subscription cancelled
    assert any(m == "DELETE" and "subscriptions/sub_test" in u for m, u, _ in http_calls.calls)

    mine = {a, b}
    for table, rows in db.tables.items():
        for row in rows:
            assert row.get("business_id") not in mine, (table, row)
            assert row.get("id") not in mine
    assert not [x for x in db.tables.get("rv_accounts", []) if x["owner_user_id"] == OWNER]
    assert not db.tables.get("invoice_items")
    assert len(db.storage_files) == files_before - 1
    # the other owner is untouched
    assert [x["id"] for x in db.tables["businesses"]] == [o]
    assert len([e for e in db.tables["rv_money_entries"] if e["business_id"] == o]) == 1
    # after deletion they're a brand-new user again
    assert client.get("/api/business", headers=as_user(OWNER)).json() == {"setupRequired": "business"}


def test_nothing_deleted_if_stripe_cancel_fails(client, db, paid_owner, http_calls, monkeypatch):
    seed(client, db, paid_owner)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    http_calls.state["responder"] = lambda m, u, kw: http_calls.Response(500, {"error": {"message": "down"}})
    res = client.request("DELETE", "/api/account", json={"confirm": "DELETE"}, headers=as_user(OWNER))
    assert res.status_code == 502 and "nothing was deleted" in res.json()["detail"]
    assert len([x for x in db.tables["businesses"] if x["user_id"] == OWNER]) == 2


def test_team_member_delete_leaves_the_store(client, db, paid_owner):
    a, b, o, r = seed(client, db, paid_owner)
    res = client.request("DELETE", "/api/account", json={"confirm": "DELETE"}, headers=as_user(MEMBER))
    assert res.status_code == 200 and res.json()["storesDeleted"] == 0
    assert not [m for m in db.tables["business_users"] if m["user_id"] == MEMBER]
    assert any(x["id"] == a for x in db.tables["businesses"])


def test_login_deleted_only_when_switched_on(client, db, paid_owner, monkeypatch):
    seed(client, db, paid_owner)
    deleted = []
    db.auth = SimpleNamespace(admin=SimpleNamespace(delete_user=lambda uid: deleted.append(uid)))
    monkeypatch.setenv("RK_DELETE_AUTH_LOGIN", "true")
    res = client.request("DELETE", "/api/account", json={"confirm": "DELETE"}, headers=as_user(MEMBER))
    assert res.json()["loginDeleted"] is True and deleted == [MEMBER]

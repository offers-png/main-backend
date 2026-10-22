"""Phase 8 regression: signup -> free -> scan -> Money Box -> upgrade ->
add store -> upload statement -> connect bank (test) -> match receipts ->
mark personal -> accountant PDF -> CSV export -> downgrade.
Stripe, the bank feed and the AI are faked; everything else is real code."""

import asyncio
import csv
import io
import json

import pytest

from conftest import as_user
from test_multi_store import OWNER, make_first_store, finish_setup
from test_billing import FakeStripe, send_event, stripe_env  # noqa: F401
from test_connect import FakeFC, fc_account, T0
from test_statements import load


@pytest.fixture
def world(http_calls, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    stripe, fc = FakeStripe(), FakeFC()
    ai = {"merchant": "Sysco", "amount": 210.0, "receipt_date": "2026-10-02", "category": "Inventory"}

    def responder(method, url, kw):
        R = http_calls.Response
        if "anthropic" in url:
            body = json.dumps(kw.get("json") or {})
            if "sort bank and credit card charges" in body:
                return R(200, {"content": [{"text": "{}"}]})
            return R(200, {"content": [{"text": json.dumps(ai)}]})
        if "financial_connections" in url:
            return fc.respond(method, url, kw, R)
        if "api.stripe.com" in url:
            if "customers" in url and method == "POST":
                return R(200, {"id": "cus_1"})
            return stripe.respond(method, url, kw, R)
        return R(200, {})
    http_calls.state["responder"] = responder
    return stripe, fc, ai


def test_full_regression(client, db, world):
    stripe, fc, ai = world
    db.unique["rv_bank_transactions"] = [("business_id", "dedupe_key")]
    H = lambda s=None: as_user(OWNER, s)  # noqa: E731

    # signup -> Free, no trial
    a = make_first_store(client)
    finish_setup(client, OWNER, a)
    st = client.get("/api/billing/status", headers=H(a)).json()
    assert st["plan"] == "free" and st["usage"]["scansThisMonth"] == 0

    # scan a receipt
    r = client.post("/api/receipts", json={"imageBase64": "aGVsbG8=", "filename": "sysco.jpg"}, headers=H(a))
    assert r.status_code == 201 and r.json()["merchant"] == "Sysco" and r.json()["scheduleCLine"] == "Line 4"

    # Money Box
    assert client.post("/api/money-entries", json={"entryDate": "2026-10-02", "entryType": "income", "category": "sales_cash", "amount": 900, "paymentMethod": "cash"}, headers=H(a)).status_code == 200

    # Free can't upload statements -> friendly prompt
    files = {"file": ("bofa.csv", load("bank_of_america.csv"), "text/csv")}
    assert client.post("/api/bank/statements/preview", files=files, headers=H(a)).status_code == 402

    # upgrade to Growth
    url = client.post("/api/billing/checkout", json={"plan": "growth"}, headers=H(a)).json()["url"]
    assert url
    sub = stripe.new_sub("price_growth")
    send_event(client, "checkout.session.completed", {"mode": "subscription", "customer": "cus_1", "subscription": sub["id"], "metadata": {"owner_user_id": OWNER}})
    assert client.get("/api/billing/status", headers=H(a)).json()["plan"] == "growth"

    # add a store (billed as add-on)
    b = client.post("/api/stores", json={"businessName": "Store B", "businessAddress": "2 Main"}, headers=H()).json()["id"]
    finish_setup(client, OWNER, b)
    assert any(i["price"]["id"] == "price_store_growth" for i in sub["items"]["data"])

    # upload a statement into store A -> Sysco charge matches the scanned receipt
    p = client.post("/api/bank/statements/preview", files=files, headers=H(a)).json()
    c = client.post(f"/api/bank/statements/{p['uploadId']}/confirm", json={"accountName": "BofA checking"}, headers=H(a)).json()
    assert c["imported"] == 3 and c["matched"] == 1

    # connect a bank (test mode) -> transactions arrive
    fc.session_accounts = [fc_account(1)]
    fc.add_txn("fca_1", "fctxn_1", -1549, "NETFLIX.COM", T0, "fctxnref_1")
    fc.add_txn("fca_1", "fctxn_2", -6000, "CON EDISON", T0, "fctxnref_1")
    assert client.post("/api/bank/connect/session", headers=H(a)).status_code == 200
    done = client.post("/api/bank/connect/complete", json={"sessionId": "fcsess_1"}, headers=H(a)).json()
    assert done["imported"] == 2

    # mark Netflix personal
    txns = client.get("/api/bank/transactions", headers=H(a)).json()["transactions"]
    netflix = next(t for t in txns if "NETFLIX" in t["description"])
    assert client.patch(f"/api/bank/transactions/{netflix['id']}", json={"isPersonal": True}, headers=H(a)).json()["status"] == "personal"

    # data never leaks into store B
    assert client.get("/api/bank/transactions", headers=H(b)).json()["transactions"] == []
    assert client.get("/api/receipts", headers=H(b)).json() == []

    # accountant PDF renders with bank charges, personal left out
    from pdf_generator import generate_cover_pdf
    from routes.receiptvault.bank_core import business_transactions
    charges = [t for t in business_transactions(db, a, "2000-01-01", "2100-01-01") if float(t["amount"]) < 0]
    assert "NETFLIX.COM" not in [t["description"] for t in charges]
    receipts = client.get("/api/receipts", headers=H(a)).json()
    pdf = asyncio.run(generate_cover_pdf("Store A", "Sam", "cpa@example.com", receipts, "October 2026", bank_transactions=charges))
    assert pdf[:4] == b"%PDF" and len(pdf) > 1500

    # CSV export: Sysco once, with its receipt; Netflix left out
    out = list(csv.DictReader(io.StringIO(client.get("/api/export/expenses.csv?start=2026-01-01&end=2026-12-31", headers=H(a)).text.lstrip("﻿"))))
    sysco = [r for r in out if "SYSCO" in r["Vendor"].upper()]
    assert len(sysco) == 1 and sysco[0]["Has receipt (Y/N)"] == "Y"
    assert not any("NETFLIX" in r["Vendor"] for r in out)

    # downgrade (cancel) -> Free; store B view-only, nothing deleted
    send_event(client, "customer.subscription.deleted", sub)
    st = client.get("/api/billing/status", headers=H(b)).json()
    assert st["plan"] == "free" and st["activeStoreReadOnly"] is True
    assert client.post("/api/money-entries", json={"entryDate": "2026-10-05", "entryType": "income", "category": "sales_cash", "amount": 1}, headers=H(b)).status_code == 402
    assert len(client.get("/api/bank/transactions", headers=H(a)).json()["transactions"]) == 5
    assert client.patch(f"/api/bank/transactions/{netflix['id']}", json={"isPersonal": False}, headers=H(a)).status_code == 402

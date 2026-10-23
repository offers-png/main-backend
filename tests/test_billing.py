"""Phase 2: plans, limits, upgrade prompts, Stripe flow, downgrades."""

import hashlib
import hmac
import json
import time

import pytest

from conftest import as_user
from test_multi_store import OWNER, make_first_store, finish_setup

PRICES = {
    "STRIPE_PRICE_BASIC": "price_basic",
    "STRIPE_PRICE_GROWTH": "price_growth",
    "STRIPE_PRICE_BUSINESS": "price_business",
    "STRIPE_PRICE_STORE_BASIC": "price_store_basic",
    "STRIPE_PRICE_STORE_GROWTH": "price_store_growth",
    "STRIPE_PRICE_STORE_BUSINESS": "price_store_business",
}
SECRET = "whsec_test"


@pytest.fixture(autouse=True)
def stripe_env(monkeypatch):
    for k, v in PRICES.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_RECEIPTKEPT", SECRET)


class FakeStripe:
    """Just enough of Stripe's subscription API for the plan-change flow."""

    def __init__(self):
        self.subs = {}
        self.n = 0

    def respond(self, method, url, kw, Response):
        data = kw.get("data") or {}
        path = url.split("/v1/", 1)[1]
        if path == "customers":
            return Response(200, {"id": "cus_1"})
        if path == "checkout/sessions":
            self.last_checkout = data
            return Response(200, {"id": "cs_1", "url": "https://checkout.stripe.test/cs_1"})
        if path == "billing_portal/sessions":
            return Response(200, {"url": "https://portal.stripe.test"})
        if path.startswith("subscriptions/"):
            sid = path.split("/")[1].split("?")[0]
            sub = self.subs[sid]
            if method == "POST":
                items = sub["items"]["data"]
                idx = 0
                while f"items[{idx}][id]" in data or f"items[{idx}][price]" in data:
                    iid = data.get(f"items[{idx}][id]")
                    if iid:
                        item = next(i for i in items if i["id"] == iid)
                        if data.get(f"items[{idx}][deleted]"):
                            items.remove(item)
                        else:
                            item["price"]["id"] = data.get(f"items[{idx}][price]", item["price"]["id"])
                            item["quantity"] = int(data.get(f"items[{idx}][quantity]", item["quantity"]))
                    else:
                        self.n += 1
                        items.append({"id": f"si_{self.n}", "price": {"id": data[f"items[{idx}][price]"]}, "quantity": int(data.get(f"items[{idx}][quantity]", 1))})
                    idx += 1
            return Response(200, sub)
        if path.startswith("subscription_items"):
            parts = path.split("?")[0].split("/")
            if len(parts) == 1:  # create
                sub = self.subs[data["subscription"]]
                self.n += 1
                item = {"id": f"si_{self.n}", "price": {"id": data["price"]}, "quantity": int(data["quantity"])}
                sub["items"]["data"].append(item)
                return Response(200, item)
            iid = parts[1]
            for sub in self.subs.values():
                for item in list(sub["items"]["data"]):
                    if item["id"] == iid:
                        if method == "DELETE":
                            sub["items"]["data"].remove(item)
                        else:
                            item["quantity"] = int(data["quantity"])
                            item["price"]["id"] = data.get("price", item["price"]["id"])
                        return Response(200, item)
        return Response(200, {})

    def new_sub(self, plan_price):
        self.n += 1
        sid = f"sub_{self.n}"
        self.subs[sid] = {"id": sid, "customer": "cus_1", "status": "active", "metadata": {"owner_user_id": OWNER},
                          "items": {"data": [{"id": f"si_main_{self.n}", "price": {"id": plan_price}, "quantity": 1}]}}
        return self.subs[sid]


@pytest.fixture
def stripe(http_calls):
    fs = FakeStripe()
    http_calls.state["responder"] = lambda m, u, kw: fs.respond(m, u, kw, http_calls.Response)
    return fs


def signed(event):
    payload = json.dumps(event)
    ts = int(time.time())
    sig = hmac.new(SECRET.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return payload, {"stripe-signature": f"t={ts},v1={sig}", "content-type": "application/json"}


def send_event(client, etype, obj):
    payload, headers = signed({"type": etype, "data": {"object": obj}})
    r = client.post("/api/billing/webhook", content=payload, headers=headers)
    assert r.status_code == 200, r.text


def status(client, store=None):
    return client.get("/api/billing/status", headers=as_user(OWNER, store)).json()


def upload(client, store=None):
    return client.post("/api/receipts", json={"imageBase64": "aGVsbG8=", "filename": f"r{time.time_ns()}.jpg"}, headers=as_user(OWNER, store))


def test_plans_endpoint_is_the_source_of_limits(client):
    plans = {p["plan"]: p for p in client.get("/api/billing/plans").json()}
    assert [p for p in plans] == ["free", "basic", "growth", "business"]
    assert plans["free"]["price"] == 0 and plans["free"]["limits"]["scansPerMonth"] == 10
    assert plans["basic"]["price"] == 5.99 and plans["basic"]["limits"]["scansPerMonth"] == 50
    assert plans["basic"]["limits"]["statementUploadsPerMonth"] == 2
    assert plans["growth"]["limits"]["scansPerMonth"] is None and plans["growth"]["limits"]["bankAccounts"] == 2
    assert plans["growth"]["limits"]["teamMembers"] == 3
    assert plans["business"]["limits"]["bankAccounts"] is None
    assert [plans[p]["extraStorePrice"] for p in plans] == [None, 4.99, 9.99, 14.99]
    assert "invoices" not in plans["free"]["features"] and "invoices" in plans["basic"]["features"]
    assert "email_forwarding" not in plans["basic"]["features"] and "email_forwarding" in plans["growth"]["features"]


def test_new_signup_is_free_with_no_trial(client, db):
    s = make_first_store(client)
    biz = db.tables["businesses"][0]
    assert biz.get("trial_ends_at") is None
    st = status(client, s)
    assert st["plan"] == "free" and st["is_active"] is True and st["in_trial"] is False


def test_free_scan_limit_and_friendly_prompt(client):
    s = make_first_store(client)
    for _ in range(10):
        assert upload(client, s).status_code == 201
    r = upload(client, s)
    assert r.status_code == 402
    d = r.json()["detail"]
    assert d["code"] == "limit_reached"
    assert d["message"] == "You've used your 10 free scans this month. Upgrade to Basic for $5.99 to keep scanning."
    assert d["requiredPlan"] == "basic"
    assert status(client, s)["usage"]["scansThisMonth"] == 10


def test_free_plan_feature_gates(client):
    s = make_first_store(client)
    finish_setup(client, OWNER, s)
    r = client.post("/api/invoices", json={"customerName": "Joe"}, headers=as_user(OWNER, s))
    assert r.status_code == 402 and r.json()["detail"]["requiredPlan"] == "basic"
    assert client.get("/api/invoices", headers=as_user(OWNER, s)).status_code == 200  # viewing is fine
    assert client.get("/api/tax/quarterly-estimate", headers=as_user(OWNER, s)).status_code == 402
    assert client.post("/api/send-now", headers=as_user(OWNER, s)).status_code == 402
    assert client.post("/api/stores", json={"businessName": "B", "businessAddress": "x"}, headers=as_user(OWNER)).status_code == 402
    # Free things never hit a wall
    assert client.get("/api/pnl", headers=as_user(OWNER, s)).status_code == 200
    assert client.post("/api/money-entries", json={"entryDate": "2026-10-05", "entryType": "expense", "category": "other_expense", "amount": 3}, headers=as_user(OWNER, s)).status_code == 200


def test_full_upgrade_add_store_downgrade_flow(client, db, stripe):
    s = make_first_store(client)
    finish_setup(client, OWNER, s)

    # Free -> Basic through Checkout
    r = client.post("/api/billing/checkout", json={"plan": "basic"}, headers=as_user(OWNER, s))
    assert r.status_code == 200 and r.json()["url"].startswith("https://checkout.stripe.test")
    assert stripe.last_checkout["line_items[0][price]"] == "price_basic"
    sub = stripe.new_sub("price_basic")
    send_event(client, "checkout.session.completed", {"mode": "subscription", "customer": "cus_1", "subscription": sub["id"], "metadata": {"owner_user_id": OWNER}})
    assert status(client, s)["plan"] == "basic"
    assert client.post("/api/invoices", json={"customerName": "Joe"}, headers=as_user(OWNER, s)).status_code == 200

    # Basic -> Growth -> Business change the subscription in place
    assert client.post("/api/billing/checkout", json={"plan": "growth"}, headers=as_user(OWNER, s)).json()["updated"]
    assert status(client, s)["plan"] == "growth"
    assert client.post("/api/billing/checkout", json={"plan": "business"}, headers=as_user(OWNER, s)).json()["updated"]
    assert status(client, s)["plan"] == "business"

    # Add a store -> add-on item at the Business add-on price
    r = client.post("/api/stores", json={"businessName": "Store B", "businessAddress": "2 Main"}, headers=as_user(OWNER))
    assert r.status_code == 201, r.text
    b = r.json()["id"]
    addon = [i for i in sub["items"]["data"] if i["price"]["id"] == "price_store_business"]
    assert addon and addon[0]["quantity"] == 1
    finish_setup(client, OWNER, b)  # usable immediately
    assert status(client, b)["activeStoreReadOnly"] is False

    # Business -> Basic moves the add-on to the Basic add-on price
    client.post("/api/billing/checkout", json={"plan": "basic"}, headers=as_user(OWNER, s))
    prices = sorted(i["price"]["id"] for i in sub["items"]["data"])
    assert prices == ["price_basic", "price_store_basic"]
    send_event(client, "customer.subscription.updated", sub)
    st = status(client, b)
    assert st["plan"] == "basic" and st["extraStores"] == 1 and st["activeStoreReadOnly"] is False

    # Cancel -> Free: nothing deleted, extra store goes view-only
    send_event(client, "customer.subscription.deleted", sub)
    st = status(client, b)
    assert st["plan"] == "free" and st["activeStoreReadOnly"] is True
    assert client.get("/api/money-entries", headers=as_user(OWNER, b)).status_code == 200
    r = client.post("/api/money-entries", json={"entryDate": "2026-10-05", "entryType": "income", "category": "sales_cash", "amount": 3}, headers=as_user(OWNER, b))
    assert r.status_code == 402 and r.json()["detail"]["code"] == "store_read_only"
    # first store stays editable
    assert client.post("/api/money-entries", json={"entryDate": "2026-10-05", "entryType": "income", "category": "sales_cash", "amount": 3}, headers=as_user(OWNER, s)).status_code == 200
    assert len(db.tables["businesses"]) == 2
    assert len(db.tables["invoices"]) == 1


def test_payment_failed_flags_account(client, stripe):
    s = make_first_store(client)
    client.post("/api/billing/checkout", json={"plan": "basic"}, headers=as_user(OWNER, s))
    send_event(client, "invoice.payment_failed", {"customer": "cus_1"})
    assert status(client, s)["paymentPastDue"] is True
    send_event(client, "invoice.paid", {"customer": "cus_1"})
    assert status(client, s)["paymentPastDue"] is False


def test_webhook_rejects_bad_signature(client):
    payload, headers = signed({"type": "invoice.paid", "data": {"object": {}}})
    headers["stripe-signature"] = headers["stripe-signature"].replace("v1=", "v1=00")
    assert client.post("/api/billing/webhook", content=payload, headers=headers).status_code == 400


def test_month_start_respects_time_zone():
    from datetime import datetime, timezone
    from routes.receiptvault.entitlements import month_start_utc
    # 02:00 UTC on Nov 1 is still Oct 31 in New York -> October's counter
    now = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
    assert month_start_utc("America/New_York", now) == "2026-10-01T04:00:00"
    assert month_start_utc("UTC", now) == "2026-11-01T00:00:00"


def test_webhook_secret_with_pasted_whitespace_still_works(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_RECEIPTKEPT", f"  {SECRET}\n")
    s = make_first_store(client)
    send_event(client, "invoice.paid", {"customer": "cus_none"})  # asserts 200


def test_webhook_accepts_either_secret_name(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_RECEIPTKEPT", "whsec_wrong_one")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_ReceiptVault", SECRET)
    send_event(client, "invoice.paid", {"customer": "cus_none"})


def test_webhook_for_deleted_account_recreates_nothing(client, db):
    gone = "99999999-9999-9999-9999-999999999999"
    sub = {"id": "sub_gone", "customer": "cus_gone", "status": "active", "metadata": {"owner_user_id": gone},
           "items": {"data": [{"id": "si", "price": {"id": "price_basic"}, "quantity": 1}]}}
    send_event(client, "customer.subscription.created", sub)
    assert not [a for a in db.tables.get("rv_accounts", []) if a["owner_user_id"] == gone]

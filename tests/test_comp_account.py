"""Lifetime ("comp") accounts: every Business feature, no limits, no
upgrade prompts, never billed, never downgraded by Stripe."""

import asyncio

from conftest import as_user
from test_multi_store import OWNER, make_first_store, finish_setup
from test_billing import stripe, stripe_env, send_event, status, upload  # noqa: F401 (fixtures)
from routes.receiptvault import entitlements as ent


def acct(db):
    return next(a for a in db.tables["rv_accounts"] if a["owner_user_id"] == OWNER)


def paid_business(client, db, stripe):
    s = make_first_store(client)
    finish_setup(client, OWNER, s)
    client.post("/api/billing/checkout", json={"plan": "basic"}, headers=as_user(OWNER, s))
    sub = stripe.new_sub("price_business")
    send_event(client, "checkout.session.completed", {"mode": "subscription", "customer": "cus_1", "subscription": sub["id"], "metadata": {"owner_user_id": OWNER}})
    return s, sub


def test_comp_account_is_business_with_no_limits_and_never_billed(client, db, stripe, http_calls):
    s, sub = paid_business(client, db, stripe)
    assert acct(db)["plan"] == "business" and acct(db)["stripe_subscription_id"] == sub["id"]
    acct(db)["comp_plan"] = "business"          # set by the database administrator

    st = status(client, s)
    assert st["plan"] == "business" and st["isComp"] is True and st["price"] == 0
    assert st["limits"] == {"scansPerMonth": None, "statementUploadsPerMonth": None, "bankAccounts": None, "teamMembers": None, "includedStores": None}
    assert st["extraStorePrice"] is None and not st["readOnlyStoreIds"]

    # Nothing to buy; adding stores is free and never touches Stripe
    r = client.post("/api/billing/checkout", json={"plan": "growth"}, headers=as_user(OWNER, s))
    assert r.status_code == 400 and "for life" in r.json()["detail"]
    calls_before = len(http_calls.calls)
    for i in range(4):
        r = client.post("/api/stores", json={"businessName": f"Shop {i}", "businessAddress": "x"}, headers=as_user(OWNER))
        assert r.status_code == 201, r.text
    assert not [c for c in http_calls.calls[calls_before:] if "stripe.com" in c[1]]
    assert not status(client, s)["readOnlyStoreIds"]

    # The subscription is canceled with no final invoice and no proration
    done = asyncio.run(ent.cancel_comp_subscriptions(db))
    assert done == [sub["id"]]
    cancel = [c for c in http_calls.calls if c[0] == "DELETE" and f"subscriptions/{sub['id']}" in c[1]]
    assert cancel and "invoice_now=false" in cancel[0][1] and "prorate=false" in cancel[0][1]

    # Stripe's cancel webhook arrives: stored plan goes to free, account still Business
    sub["status"] = "canceled"
    send_event(client, "customer.subscription.deleted", sub)
    assert acct(db)["plan"] == "free" and acct(db)["subscription_status"] == "canceled"
    st = status(client, s)
    assert st["plan"] == "business" and st["isComp"] and not st["paymentPastDue"]
    assert asyncio.run(ent.cancel_comp_subscriptions(db)) == []      # nothing left to cancel

    # A failed-payment event can't flag it either
    send_event(client, "invoice.payment_failed", {"customer": "cus_1"})
    assert status(client, s)["paymentPastDue"] is False

    # Every gated feature works with no upgrade prompt
    for _ in range(60):
        assert upload(client, s).status_code == 201
    assert client.post("/api/invoices", json={"customerName": "Joe"}, headers=as_user(OWNER, s)).status_code == 200
    assert client.get("/api/inbound/address", headers=as_user(OWNER, s)).status_code == 200


def test_backend_never_writes_the_flag(client, db, stripe):
    s, sub = paid_business(client, db, stripe)
    ent.update_account(db, OWNER, {"plan": "basic"})
    assert "comp_plan" not in acct(db)
    sub["status"] = "canceled"
    send_event(client, "customer.subscription.deleted", sub)
    assert "comp_plan" not in acct(db) and status(client, s)["plan"] == "free"

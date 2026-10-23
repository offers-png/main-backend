"""The API-key checkout app's webhook (/api/webhook) shares the Stripe
account with ReceiptKept: it must skip ReceiptKept checkouts but keep
issuing keys for its own."""

import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fake_supabase import FakeSupabase

SECRET = "whsec_checkout_app_test"


@pytest.fixture
def setup(monkeypatch):
    from routes.checkout import routes as checkout
    db = FakeSupabase()
    monkeypatch.setattr(checkout, "WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(checkout, "get_supabase", lambda: db)
    app = FastAPI()
    app.include_router(checkout.checkout_routes, prefix="/api/checkout")

    @app.post("/api/webhook")  # same alias as app.py
    async def alias(request: __import__("fastapi").Request):
        return await checkout.stripe_webhook(request)

    return TestClient(app), db


def post(client, session):
    payload = json.dumps({"id": "evt_x", "object": "event", "type": "checkout.session.completed", "data": {"object": session}})
    ts = int(time.time())
    sig = hmac.new(SECRET.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return client.post("/api/webhook", content=payload, headers={"stripe-signature": f"t={ts},v1={sig}", "content-type": "application/json"})


def test_receiptkept_checkout_is_skipped(setup):
    client, db = setup
    r = post(client, {"id": "cs_rk", "object": "checkout.session", "payment_status": "paid", "mode": "subscription",
                      "metadata": {"app": "receiptkept", "owner_user_id": "u1", "plan": "basic"}})
    assert r.status_code == 200 and r.json() == {"received": True, "skipped": "receiptkept"}
    assert not db.tables.get("api_keys")


def test_own_checkout_still_issues_a_key(setup):
    client, db = setup
    r = post(client, {"id": "cs_own", "object": "checkout.session", "payment_status": "paid", "mode": "payment", "metadata": {"plan": "7d"}})
    assert r.status_code == 200
    assert [k["session_id"] for k in db.tables["api_keys"]] == ["cs_own"]


def test_session_without_metadata_still_works(setup):
    client, db = setup
    r = post(client, {"id": "cs_nometa", "object": "checkout.session", "payment_status": "paid", "mode": "payment", "metadata": None})
    assert r.status_code == 200 and len(db.tables["api_keys"]) == 1

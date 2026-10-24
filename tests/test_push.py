"""Android app: phone tokens, notification switches and the FCM sender."""

import asyncio
import json
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from conftest import as_user
from test_multi_store import OWNER, OTHER, make_first_store, finish_setup  # noqa: F401


@pytest.fixture
def firebase(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT", json.dumps({
        "project_id": "rk-test", "client_email": "push@rk-test.iam.gserviceaccount.com", "private_key": pem, "private_key_id": "k1",
    }))
    from routes.receiptvault import push_routes
    push_routes._token_cache.update(token=None, exp=0)
    return push_routes


def _fcm_responder(http_calls, fail_token=None):
    def responder(method, url, kw):
        if "oauth2.googleapis.com" in url:
            return http_calls.Response(200, {"access_token": "AT", "expires_in": 3600})
        if "fcm.googleapis.com" in url:
            if fail_token and kw["json"]["message"]["token"] == fail_token:
                return http_calls.Response(404, {"error": {"status": "NOT_FOUND"}}, text='{"error":{"status":"NOT_FOUND","details":[{"errorCode":"UNREGISTERED"}]}}')
            return http_calls.Response(200, {"name": "projects/rk-test/messages/1"})
        return http_calls.Response(200, {})
    http_calls.state["responder"] = responder


def test_register_refresh_and_delete_device(client, db):
    store = make_first_store(client)
    r = client.post("/api/devices", json={"token": "tok-1", "platform": "android"}, headers=as_user(OWNER, store))
    assert r.status_code == 200
    rows = db.tables["rv_device_tokens"]
    assert len(rows) == 1 and rows[0]["user_id"] == OWNER and rows[0]["store_id"] == store
    # same phone again: no duplicate
    client.post("/api/devices", json={"token": "tok-1"}, headers=as_user(OWNER, store))
    assert len(db.tables["rv_device_tokens"]) == 1
    # another login can't delete my phone
    client.request("DELETE", "/api/devices", json={"token": "tok-1"}, headers=as_user(OTHER))
    assert len(db.tables["rv_device_tokens"]) == 1
    client.request("DELETE", "/api/devices", json={"token": "tok-1"}, headers=as_user(OWNER))
    assert db.tables["rv_device_tokens"] == []


def test_device_requires_login_and_token(client):
    assert client.post("/api/devices", json={"token": "x"}).status_code == 401
    make_first_store(client)
    assert client.post("/api/devices", json={"token": "  "}, headers=as_user(OWNER)).status_code == 400


def test_preferences_default_on_and_update(client):
    make_first_store(client)
    assert client.get("/api/notifications/preferences", headers=as_user(OWNER)).json() == {
        "weeklyMissingReceipts": True, "receiptSaved": True, "bankRelogin": True, "accountantSent": True}
    r = client.put("/api/notifications/preferences", json={"weeklyMissingReceipts": False}, headers=as_user(OWNER))
    assert r.json()["weeklyMissingReceipts"] is False and r.json()["receiptSaved"] is True


def test_send_respects_switch_and_drops_dead_tokens(client, db, http_calls, firebase):
    store = make_first_store(client)
    for t in ("good", "dead"):
        client.post("/api/devices", json={"token": t}, headers=as_user(OWNER, store))
    _fcm_responder(http_calls, fail_token="dead")
    n = asyncio.run(firebase.send_push(db, OWNER, "receipt_saved", "Receipt from A-1 Wholesale saved", "x", "/receipts/1"))
    assert n == 1
    assert [d["fcm_token"] for d in db.tables["rv_device_tokens"]] == ["good"]
    sent = [kw["json"]["message"] for m, u, kw in http_calls.calls if "fcm.googleapis.com" in u]
    assert sent[0]["data"] == {"url": "/receipts/1", "kind": "receipt_saved"}
    assert sent[0]["android"]["notification"]["channel_id"] == "receipts"
    # turned off -> nothing goes out
    client.put("/api/notifications/preferences", json={"receiptSaved": False}, headers=as_user(OWNER))
    before = len(http_calls.calls)
    assert asyncio.run(firebase.send_push(db, OWNER, "receipt_saved", "t", "b")) == 0
    assert len(http_calls.calls) == before


def test_no_firebase_key_sends_nothing(client, db, http_calls, monkeypatch):
    monkeypatch.delenv("FIREBASE_SERVICE_ACCOUNT", raising=False)
    from routes.receiptvault import push_routes
    store = make_first_store(client)
    client.post("/api/devices", json={"token": "t"}, headers=as_user(OWNER, store))
    assert asyncio.run(push_routes.send_push(db, OWNER, "accountant_sent", "t", "b")) == 0
    assert not [c for c in http_calls.calls if "fcm" in c[1]]


def test_weekly_summary_monday_9am_once(client, db, http_calls, firebase):
    store = make_first_store(client)
    client.post("/api/devices", json={"token": "t"}, headers=as_user(OWNER, store))
    db.tables.setdefault("rv_bank_transactions", []).extend([
        {"id": "b1", "business_id": store, "amount": -120, "status": "needs_receipt", "description": "SYSCO"},
        {"id": "b2", "business_id": store, "amount": -20, "status": "needs_receipt", "description": "DUNKIN"},
    ])
    _fcm_responder(http_calls)
    monday_9am_ny = datetime(2026, 10, 26, 13, 15, tzinfo=timezone.utc)  # 9:15 New York
    sunday = datetime(2026, 10, 25, 13, 15, tzinfo=timezone.utc)
    assert asyncio.run(firebase.run_weekly_missing_receipts(db, sunday))["sent"] == 0
    assert asyncio.run(firebase.run_weekly_missing_receipts(db, monday_9am_ny))["sent"] == 1
    msg = [kw["json"]["message"] for m, u, kw in http_calls.calls if "fcm" in u][-1]
    assert msg["notification"]["title"] == "1 big charge needs a receipt"
    # the next hourly run that morning doesn't send again
    later = datetime(2026, 10, 26, 14, 15, tzinfo=timezone.utc)
    assert asyncio.run(firebase.run_weekly_missing_receipts(db, later))["sent"] == 0


def test_account_delete_and_export_cover_push_data(client, db):
    store = make_first_store(client)
    client.post("/api/devices", json={"token": "secret-token"}, headers=as_user(OWNER, store))
    client.put("/api/notifications/preferences", json={"bankRelogin": False}, headers=as_user(OWNER))
    import io, zipfile
    z = zipfile.ZipFile(io.BytesIO(client.get("/api/account/export", headers=as_user(OWNER)).content))
    acct = json.loads(z.read("account.json"))
    assert acct["notificationPreferences"]["bankRelogin"] is False
    assert acct["devices"] and "fcm_token" not in acct["devices"][0]
    assert "secret-token" not in z.read("account.json").decode()
    assert client.request("DELETE", "/api/account", json={"confirm": "DELETE"}, headers=as_user(OWNER)).status_code == 200
    assert db.tables.get("rv_device_tokens") == [] and db.tables.get("rv_notification_prefs") == []


def test_bank_deactivated_flags_and_pushes_once(client, db, http_calls, firebase):
    store = make_first_store(client)
    client.post("/api/devices", json={"token": "t"}, headers=as_user(OWNER, store))
    db.tables.setdefault("rv_bank_accounts", []).append({"id": "ba1", "business_id": store, "source": "stripe", "status": "active",
                                                         "stripe_account_id": "fca_1", "institution": "Chase"})
    _fcm_responder(http_calls)
    from routes.receiptvault.connect_routes import handle_account_inactive
    asyncio.run(handle_account_inactive(db, {"id": "fca_1"}, "financial_connections.account.deactivated"))
    asyncio.run(handle_account_inactive(db, {"id": "fca_1"}, "financial_connections.account.deactivated"))
    assert db.tables["rv_bank_accounts"][0]["status"] == "inactive"
    msgs = [kw["json"]["message"] for m, u, kw in http_calls.calls if "fcm" in u]
    assert len(msgs) == 1 and msgs[0]["notification"]["title"] == "Chase needs you to sign in again"
    assert msgs[0]["data"]["url"] == "/settings?tab=banks"

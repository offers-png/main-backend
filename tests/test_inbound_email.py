"""Phase 6: forwarding a supplier invoice email creates a filled-in receipt."""

import base64
import hashlib
import hmac
import json
import time

import pytest

from conftest import as_user
from test_multi_store import OWNER, make_first_store, finish_setup

SECRET = "whsec_" + base64.b64encode(b"inbound-test-secret").decode()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("RESEND_INBOUND_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")


@pytest.fixture
def store(client, db):
    db.unique["rv_bank_transactions"] = [("business_id", "dedupe_key")]
    s = make_first_store(client)
    finish_setup(client, OWNER, s)
    next(a for a in db.tables["rv_accounts"] if a["owner_user_id"] == OWNER)["plan"] = "growth"
    return s


def post_event(client, event, secret=SECRET):
    body = json.dumps(event).encode()
    msg_id, ts = "msg_1", str(int(time.time()))
    key = base64.b64decode(secret.split("_", 1)[1])
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return client.post("/api/inbound/resend", content=body, headers={"svix-id": msg_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}", "content-type": "application/json"})


def fake_world(http_calls, email_id="em_1", attachments=None, html=""):
    ai = {"merchant": "Heffron Distributing", "amount": 412.50, "receipt_date": None, "category": "Inventory"}

    def responder(method, url, kw):
        if "anthropic" in url:
            return http_calls.Response(200, {"content": [{"text": json.dumps(ai)}]})
        if url.endswith(f"emails/receiving/{email_id}/attachments"):
            return http_calls.Response(200, {"data": attachments or []})
        if url.endswith(f"emails/receiving/{email_id}"):
            return http_calls.Response(200, {"id": email_id, "html": html, "text": ""})
        if "inbound-cdn" in url:
            r = http_calls.Response(200, {})
            r.content = b"%PDF-1.4 invoice"
            return r
        return http_calls.Response(200, {})
    http_calls.state["responder"] = responder


def event(to, email_id="em_1", attachments=None):
    return {"type": "email.received", "data": {"email_id": email_id, "from": "billing@heffron.com", "to": [to],
                                               "subject": "Invoice 8812", "attachments": attachments or []}}


def test_address_and_supplier_invoice_becomes_receipt(client, db, store, http_calls):
    addr = client.get("/api/inbound/address", headers=as_user(OWNER, store)).json()["address"]
    assert addr.startswith("r-") and addr.endswith("@in.receiptkept.com")
    meta = [{"id": "att1", "filename": "invoice-8812.pdf", "content_type": "application/pdf", "size": 52000}]
    fake_world(http_calls, attachments=[dict(meta[0], download_url="https://inbound-cdn.resend.com/x", content_disposition="attachment")])
    r = post_event(client, event(f"Store A <{addr}>", attachments=meta))
    assert r.status_code == 200, r.text
    receipts = db.tables["receipts"]
    assert len(receipts) == 1
    rec = receipts[0]
    assert rec["merchant"] == "Heffron Distributing" and float(rec["amount"]) == 412.5
    assert rec["category"] == "Inventory" and rec["schedule_c_line"] == "Line 4"
    assert rec["business_id"] == store
    # same email again (webhook retry) -> ignored
    post_event(client, event(addr, attachments=meta))
    assert len(db.tables["receipts"]) == 1
    recent = client.get("/api/inbound/recent", headers=as_user(OWNER, store)).json()
    assert recent[0]["status"] == "processed" and recent[0]["receiptsCreated"] == 1


def test_no_attachment_uses_email_body(client, db, store, http_calls):
    addr = client.get("/api/inbound/address", headers=as_user(OWNER, store)).json()["address"]
    fake_world(http_calls, email_id="em_2", html="<html><style>x{}</style><p>Thanks for your order</p><p>Total $412.50</p></html>")
    post_event(client, event(addr, email_id="em_2"))
    rec = db.tables["receipts"][0]
    assert rec["original_name"].endswith(".txt") and float(rec["amount"]) == 412.5
    stored = [v for (b, p), v in db.storage_files.items() if p.endswith(".txt")][0]
    assert b"<p>" not in stored and b"Total $412.50" in stored


def test_rejects_bad_signature_unknown_token_big_mail_and_free_plan(client, db, store, http_calls):
    addr = client.get("/api/inbound/address", headers=as_user(OWNER, store)).json()["address"]
    fake_world(http_calls)
    bad = "whsec_" + base64.b64encode(b"wrong").decode()
    assert post_event(client, event(addr), secret=bad).status_code == 400
    assert post_event(client, event("r-zzzzzzzzzzzz@in.receiptkept.com")).json()["ignored"] == "unknown address"
    big = [{"id": "a", "filename": "x.pdf", "content_type": "application/pdf", "size": 21 * 1024 * 1024}]
    assert post_event(client, event(addr, email_id="em_big", attachments=big)).json()["ignored"] == "too large"
    next(a for a in db.tables["rv_accounts"] if a["owner_user_id"] == OWNER)["plan"] = "basic"
    assert post_event(client, event(addr, email_id="em_3")).json()["ignored"] == "plan"
    assert client.get("/api/inbound/address", headers=as_user(OWNER, store)).status_code == 402
    assert not db.tables.get("receipts")


def test_rate_limit(client, db, store, http_calls):
    from routes.receiptvault import inbound_email_routes as ier
    addr = client.get("/api/inbound/address", headers=as_user(OWNER, store)).json()["address"]
    fake_world(http_calls)
    from datetime import datetime, timezone
    db.tables["rv_inbound_emails"] = [{"id": str(i), "business_id": store, "resend_email_id": f"old{i}", "status": "processed",
                                       "created_at": datetime.now(timezone.utc).isoformat()} for i in range(ier.RATE_PER_HOUR)]
    assert post_event(client, event(addr, email_id="em_9")).json()["ignored"] == "rate limited"


def test_rotate_address(client, store, http_calls):
    a1 = client.get("/api/inbound/address", headers=as_user(OWNER, store)).json()["address"]
    a2 = client.post("/api/inbound/address/rotate", headers=as_user(OWNER, store)).json()["address"]
    assert a1 != a2
    fake_world(http_calls)
    assert post_event(client, event(a1, email_id="em_old")).json()["ignored"] == "unknown address"

"""Inbound email: its own Resend key, receipts read from the email text, a
clear 'No receipt found' when there isn't one, and replays after a failure."""

import json

from conftest import as_user
from test_multi_store import OWNER
from test_inbound_email import env, store, post_event, event  # noqa: F401 (fixtures)


def world(http_calls, ai, html="", text="", email_id="em_9"):
    seen = {"auth": [], "ai_calls": 0}

    def responder(method, url, kw):
        if "anthropic" in url:
            seen["ai_calls"] += 1
            return http_calls.Response(200, {"content": [{"text": json.dumps(ai)}]})
        if "api.resend.com" in url:
            seen["auth"].append((kw.get("headers") or {}).get("Authorization"))
            if url.endswith("/attachments"):
                return http_calls.Response(200, {"data": []})
            return http_calls.Response(200, {"id": email_id, "html": html, "text": text})
        return http_calls.Response(200, {})
    http_calls.state["responder"] = responder
    return seen


def address(client, s):
    return client.get("/api/inbound/address", headers=as_user(OWNER, s)).json()["address"]


def test_receipt_read_from_plain_email_text_with_one_ai_call(client, db, store, http_calls):
    seen = world(http_calls, {"merchant": "Corona Beverage", "amount": 320.25, "receipt_date": None, "category": "Inventory"},
                 text="Order confirmation\nCorona Beverage\nTotal: $320.25")
    post_event(client, event(address(client, store), email_id="em_9"))
    rec = db.tables["receipts"][0]
    assert rec["merchant"] == "Corona Beverage" and float(rec["amount"]) == 320.25 and rec["original_name"].endswith(".txt")
    assert seen["ai_calls"] == 1                                   # read once, not twice
    assert seen["auth"] and all(a == "Bearer re_test" for a in seen["auth"])   # the inbound workspace key
    assert db.tables["rv_inbound_emails"][0]["status"] == "processed"


def test_no_attachment_and_no_receipt_in_text(client, db, store, http_calls):
    world(http_calls, {"merchant": None, "amount": None, "receipt_date": None, "category": None},
          text="hey, can you call me back tomorrow?")
    post_event(client, event(address(client, store), email_id="em_9"))
    assert not db.tables.get("receipts")                           # no empty receipt
    recent = client.get("/api/inbound/recent", headers=as_user(OWNER, store)).json()
    assert recent[0]["status"] == "no_receipt"
    assert recent[0]["message"] == "No receipt found — attach a PDF or photo."


def test_missing_key_fails_clearly_and_a_replay_works(client, db, store, http_calls, monkeypatch):
    monkeypatch.delenv("RESEND_INBOUND_API_KEY", raising=False)
    world(http_calls, {"merchant": "Pepsi", "amount": 50.0, "receipt_date": None, "category": "Inventory"}, text="Pepsi total $50.00")
    addr = address(client, store)
    post_event(client, event(addr, email_id="em_9"))
    log = db.tables["rv_inbound_emails"][0]
    assert log["status"] == "failed" and "RESEND_INBOUND_API_KEY" in log["error"]
    recent = client.get("/api/inbound/recent", headers=as_user(OWNER, store)).json()
    assert "forward it again" in recent[0]["message"]

    # key added on Render, Resend replays the same email -> processed, same log row
    monkeypatch.setenv("RESEND_INBOUND_API_KEY", "re_test")
    post_event(client, event(addr, email_id="em_9"))
    assert len(db.tables["rv_inbound_emails"]) == 1
    assert db.tables["rv_inbound_emails"][0]["status"] == "processed" and len(db.tables["receipts"]) == 1
    # a third delivery of a processed email is still a duplicate
    post_event(client, event(addr, email_id="em_9"))
    assert len(db.tables["receipts"]) == 1


def test_email_list_and_delete_never_touches_receipts(client, db, store, http_calls):
    world(http_calls, {"merchant": "Pepsi", "amount": 50.0, "receipt_date": None, "category": "Inventory"}, text="Pepsi total $50.00")
    addr = address(client, store)
    for i in range(3):
        post_event(client, event(addr, email_id=f"em_{i}"))
    h = as_user(OWNER, store)
    items = client.get("/api/inbound/emails", headers=h).json()
    assert len(items) == 3 and len(db.tables["receipts"]) == 3

    assert client.delete(f"/api/inbound/emails/{items[0]['id']}", headers=h).json() == {"deleted": 1}
    r = client.post("/api/inbound/emails/delete", json={"ids": [items[1]["id"], items[2]["id"], "not-mine"]}, headers=h)
    assert r.json() == {"deleted": 2}
    assert client.get("/api/inbound/emails", headers=h).json() == []
    assert len(db.tables["receipts"]) == 3                       # receipts stay
    assert client.delete("/api/inbound/emails/nope", headers=h).status_code == 404
    assert client.post("/api/inbound/emails/delete", json={"ids": []}, headers=h).status_code == 400

    # another store's entries can't be deleted
    db.tables["rv_inbound_emails"].append({"id": "other", "business_id": "other-store", "status": "processed"})
    assert client.post("/api/inbound/emails/delete", json={"ids": ["other"]}, headers=h).json() == {"deleted": 0}
    assert any(e["id"] == "other" for e in db.tables["rv_inbound_emails"])

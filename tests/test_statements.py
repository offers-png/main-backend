"""Phase 4: statement parsing for common US banks, preview -> confirm,
no duplicates on re-upload, plan limit, column picker, PDF via AI."""

import json
import os

import pytest

from conftest import as_user
from test_multi_store import OWNER, make_first_store, finish_setup
from routes.receiptvault import statement_parser as sp

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "statements")


def load(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


def amounts(name, **kw):
    return [(t["posted_date"], round(t["amount"], 2)) for t in sp.parse_csv(load(name), **kw)["transactions"]]


def test_chase_checking():
    out = amounts("chase_checking.csv")
    assert ("2026-10-03", -310.25) in out and ("2026-10-01", 1250.0) in out and len(out) == 4


def test_chase_credit_card():
    assert amounts("chase_credit.csv") == [("2026-10-05", -45.10), ("2026-10-04", -89.99), ("2026-10-02", 500.0)]


def test_bank_of_america_skips_summary_block():
    out = amounts("bank_of_america.csv")
    assert out == [("2026-10-02", -210.0), ("2026-10-05", -200.0), ("2026-10-06", 2000.0)]


def test_wells_fargo_headerless_keeps_same_day_twins():
    out = amounts("wells_fargo.csv")
    assert out.count(("2026-10-03", -4.5)) == 2 and ("2026-10-05", 850.0) in out


def test_citi_and_capital_one_debit_credit_columns():
    assert amounts("citi.csv") == [("2026-10-04", -154.20), ("2026-10-02", 300.0), ("2026-10-01", -65.0)]
    assert amounts("capital_one.csv") == [("2026-10-05", -32.11), ("2026-10-03", 100.0)]


def test_amex_positive_charges_are_flipped():
    res = sp.parse_csv(load("amex.csv"))
    assert res["flipped"] is True
    d = {t["description"]: t["amount"] for t in res["transactions"]}
    assert d["UBER TRIP"] == -18.40 and d["AUTOPAY PAYMENT - THANK YOU"] == 500.0


def test_keybank_mt_credit_union():
    assert amounts("keybank.csv") == [("2026-10-02", -75.0), ("2026-10-03", 400.0)]
    assert amounts("mt_bank.csv") == [("2026-10-02", -95.40), ("2026-10-03", 600.0)]
    assert amounts("credit_union.csv") == [("2026-10-02", -55.0), ("2026-10-03", 300.0)]


def test_unknown_layout_asks_for_columns_then_works_with_mapping():
    with pytest.raises(sp.MappingNeeded) as e:
        sp.parse_csv(load("weird.csv"))
    assert e.value.headers == ["Col A", "Col B", "Col C"]
    raw = b"When,What,How much\n10/09/2026,PAPER CO,-12.00\n"
    with pytest.raises(sp.MappingNeeded):
        sp.parse_csv(raw)
    out = sp.parse_csv(raw, mapping={"headerRow": 0, "date": 0, "description": 1, "amount": 2})
    assert out["transactions"] == [{"posted_date": "2026-10-09", "description": "PAPER CO", "amount": -12.0}]


def test_value_parsers():
    assert sp.parse_amount("($1,234.56)") == -1234.56
    assert sp.parse_amount("12.00 CR") == 12.0
    assert sp.parse_amount("-$5") == -5.0
    assert sp.parse_date("2026-10-01") == "2026-10-01"
    assert sp.parse_date("10/1/26") == "2026-10-01"
    assert sp.guess_bank("Chase1234_Activity_20261001.CSV") == "Chase"


# ── endpoints ───────────────────────────────────────────────────────────────

@pytest.fixture
def store(client, db):
    db.unique["rv_bank_transactions"] = [("business_id", "dedupe_key")]
    s = make_first_store(client)
    finish_setup(client, OWNER, s)
    return s


def set_plan(db, plan):
    acct = next(a for a in db.tables["rv_accounts"] if a["owner_user_id"] == OWNER)
    acct["plan"] = plan


def upload(client, store, name, data=None, **extra):
    files = {"file": (name, data if data is not None else load(name), "text/csv")}
    return client.post("/api/bank/statements/preview", files=files, data=extra, headers=as_user(OWNER, store))


def confirm(client, store, upload_id, **body):
    return client.post(f"/api/bank/statements/{upload_id}/confirm", json=body, headers=as_user(OWNER, store))


def test_free_plan_gets_upgrade_prompt(client, db, store):
    r = upload(client, store, "chase_checking.csv")
    assert r.status_code == 402 and r.json()["detail"]["requiredPlan"] == "basic"


def test_preview_confirm_and_no_duplicates_on_reupload(client, db, store):
    set_plan(db, "growth")
    for name in ("chase_checking.csv", "bank_of_america.csv", "wells_fargo.csv"):
        p = upload(client, store, name)
        assert p.status_code == 200, p.text
        p = p.json()
        assert p["needsMapping"] is False and p["alreadySaved"] == 0
        assert not db.tables.get("rv_bank_transactions") or all(t["statement_upload_id"] != p["uploadId"] for t in db.tables["rv_bank_transactions"])
        c = confirm(client, store, p["uploadId"]).json()
        assert c["imported"] == p["count"] and c["duplicates"] == 0

        again = upload(client, store, name).json()
        assert again["alreadySaved"] == again["count"]
        c2 = confirm(client, store, again["uploadId"]).json()
        assert c2["imported"] == 0 and c2["duplicates"] == again["count"]

    assert len(db.tables["rv_bank_transactions"]) == 4 + 3 + 4
    hist = client.get("/api/bank/statements", headers=as_user(OWNER, store)).json()
    assert len(hist) == 6


def test_basic_plan_two_uploads_a_month(client, db, store):
    set_plan(db, "basic")
    for name in ("chase_checking.csv", "citi.csv"):
        p = upload(client, store, name).json()
        assert confirm(client, store, p["uploadId"]).status_code == 200
    r = upload(client, store, "amex.csv")
    assert r.status_code == 402
    assert r.json()["detail"]["message"] == "You've uploaded your 2 statements for this month. Upgrade to Growth for $14.99 for unlimited uploads."


def test_column_picker_round_trip(client, db, store):
    set_plan(db, "growth")
    raw = b"When,What,How much\n10/09/2026,PAPER CO,-12.00\n"
    r = upload(client, store, "odd.csv", data=raw).json()
    assert r["needsMapping"] is True and r["headers"] == ["When", "What", "How much"]
    r = upload(client, store, "odd.csv", data=raw, mapping=json.dumps({"headerRow": 0, "date": 0, "description": 1, "amount": 2})).json()
    assert r["count"] == 1
    assert confirm(client, store, r["uploadId"]).json()["imported"] == 1


def test_flip_signs_on_confirm(client, db, store):
    set_plan(db, "growth")
    p = upload(client, store, "keybank.csv").json()
    confirm(client, store, p["uploadId"], flip=True)
    amts = sorted(float(t["amount"]) for t in db.tables["rv_bank_transactions"])
    assert amts == [-400.0, 75.0]


def test_pdf_statement_via_ai(client, db, store, http_calls, monkeypatch):
    set_plan(db, "growth")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    ai = {"bank": "Chase", "account_last4": "9876", "transactions": [
        {"date": "2026-10-02", "description": "SYSCO", "amount": -210.0},
        {"date": "2026-10-03", "description": "DEPOSIT", "amount": 900},
    ]}

    def responder(method, url, kw):
        if "anthropic" in url:
            return http_calls.Response(200, {"content": [{"text": json.dumps(ai)}]})
        return http_calls.Response(200, {})
    http_calls.state["responder"] = responder

    files = {"file": ("october.pdf", b"%PDF-1.4 fake", "application/pdf")}
    p = client.post("/api/bank/statements/preview", files=files, headers=as_user(OWNER, store)).json()
    assert p["fileType"] == "pdf" and p["count"] == 2 and p["accountName"] == "Chase …9876"
    assert confirm(client, store, p["uploadId"]).json()["imported"] == 2
    p2 = client.post("/api/bank/statements/preview", files=files, headers=as_user(OWNER, store)).json()
    assert p2["alreadySaved"] == 2

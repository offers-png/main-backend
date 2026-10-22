"""Phase 3: dedupe, vendor rules, receipt matching, Personal button, and
transactions flowing into P&L / tax / accountant report."""

import asyncio

import pytest

from conftest import as_user
from test_multi_store import OWNER, make_first_store, finish_setup
from routes.receiptvault import bank_core


@pytest.fixture
def basic_store(client, db):
    db.unique["rv_bank_transactions"] = [("business_id", "dedupe_key")]
    s = make_first_store(client)
    finish_setup(client, OWNER, s)
    db.tables.setdefault("rv_accounts", [])
    acct = next((a for a in db.tables["rv_accounts"] if a["owner_user_id"] == OWNER), None)
    acct["plan"] = "basic"
    return s


def biz(db, store_id):
    return next(b for b in db.tables["businesses"] if b["id"] == store_id)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_normalize_and_vendor_key():
    assert bank_core.normalize_description("POS DEBIT SHELL OIL 57444 10/03") == "shell oil"
    assert bank_core.vendor_key("POS DEBIT SHELL OIL 57444 10/03") == "shell oil"
    assert bank_core.normalize_description("SQ *JOES COFFEE #123") == "joes coffee"
    assert bank_core.normalize_description("PURCHASE AUTHORIZED ON 10/02 HOME DEPOT #1234") == "home depot"
    assert bank_core.is_transfer("ONLINE TRANSFER TO SAV 1234")
    assert bank_core.is_transfer("PAYMENT THANK YOU-MOBILE")


def test_dedupe_keeps_real_same_day_twins_and_skips_reupload(db, basic_store):
    b = biz(db, basic_store)
    txns = [
        {"posted_date": "2026-10-02", "description": "JOES COFFEE", "amount": -4.50},
        {"posted_date": "2026-10-02", "description": "JOES COFFEE", "amount": -4.50},
        {"posted_date": "2026-10-03", "description": "CON EDISON", "amount": -120.00},
    ]
    r1 = run(bank_core.import_transactions(db, b, txns, source="upload", account_ref="chase-1234", auto=False))
    assert r1["imported"] == 3 and r1["duplicates"] == 0
    r2 = run(bank_core.import_transactions(db, b, txns, source="upload", account_ref="chase-1234", auto=False))
    assert r2["imported"] == 0 and r2["duplicates"] == 3
    # same charges from a different account are different money
    r3 = run(bank_core.import_transactions(db, b, txns[2:], source="upload", account_ref="citi-9999", auto=False))
    assert r3["imported"] == 1


def test_ai_categorize_and_rules(db, basic_store, http_calls, monkeypatch, client):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    b = biz(db, basic_store)

    def responder(method, url, kw):
        if "anthropic" in url:
            return http_calls.Response(200, {"content": [{"text": '{"0": "Inventory", "1": "Utilities"}'}]})
        return http_calls.Response(200, {})
    http_calls.state["responder"] = responder

    res = run(bank_core.import_transactions(db, b, [
        {"posted_date": "2026-10-02", "description": "RESTAURANT DEPOT 0042", "amount": -310.25},
        {"posted_date": "2026-10-03", "description": "CON EDISON BILL", "amount": -120.00},
        {"posted_date": "2026-10-04", "description": "ONLINE TRANSFER TO SAV", "amount": -500.00},
        {"posted_date": "2026-10-05", "description": "DEPOSIT", "amount": 900.00},
    ], source="upload", account_ref="a"))
    rows = {r["description"]: r for r in db.tables["rv_bank_transactions"]}
    assert rows["RESTAURANT DEPOT 0042"]["category"] == "Inventory"
    assert rows["RESTAURANT DEPOT 0042"]["schedule_c_line"] == "Line 4"
    assert rows["CON EDISON BILL"]["category"] == "Utilities"
    assert rows["ONLINE TRANSFER TO SAV"]["status"] == "ignored"
    assert rows["DEPOSIT"]["status"] == "ignored"  # deposits aren't income unless told so
    assert res["imported"] == 4

    # Owner fixes Con Ed -> rule saved; next Con Ed charge uses it (no AI)
    txn_id = rows["CON EDISON BILL"]["id"]
    r = client.patch(f"/api/bank/transactions/{txn_id}", json={"category": "Rent"}, headers=as_user(OWNER, basic_store))
    assert r.status_code == 200, r.text
    assert any(x["vendor_pattern"] == "con edison" for x in db.tables["rv_vendor_rules"])
    run(bank_core.import_transactions(db, b, [{"posted_date": "2026-11-03", "description": "CON EDISON BILL", "amount": -99.0}], source="upload", account_ref="a"))
    newest = [t for t in db.tables["rv_bank_transactions"] if t["posted_date"] == "2026-11-03"][0]
    assert newest["category"] == "Rent" and newest["category_source"] == "rule"


def test_matching_both_directions_and_unlink(db, basic_store, client):
    b = biz(db, basic_store)
    db.tables.setdefault("receipts", []).append({"id": "r1", "business_id": basic_store, "merchant": "Home Depot", "amount": 52.10, "receipt_date": "2026-10-01"})
    db.tables["receipts"].append({"id": "r2", "business_id": basic_store, "merchant": "Lowes", "amount": 52.10, "receipt_date": "2026-10-03"})
    run(bank_core.import_transactions(db, b, [
        {"posted_date": "2026-10-04", "description": "THE HOME DEPOT #6543", "amount": -52.10},
        {"posted_date": "2026-10-20", "description": "STAPLES", "amount": -19.99},
    ], source="upload", account_ref="a", auto=False))
    txns = {t["description"]: t for t in db.tables["rv_bank_transactions"]}
    # vendor match preferred over the closer-in-time Lowes receipt
    rows = [t for t in db.tables["rv_bank_transactions"] if t["status"] == "needs_receipt"]
    assert bank_core.match_transactions(db, basic_store, rows) == 1
    hd = next(t for t in db.tables["rv_bank_transactions"] if "HOME DEPOT" in t["description"])
    assert hd["matched_receipt_id"] == "r1" and hd["status"] == "matched"

    # a new receipt scanned later matches the Staples charge (±$0.01, within 5 days)
    staples_receipt = {"id": "r3", "business_id": basic_store, "merchant": "Staples", "amount": 20.05, "receipt_date": "2026-10-24"}
    db.tables["receipts"].append(staples_receipt)
    assert bank_core.match_receipt(db, basic_store, staples_receipt) is None  # off by more than a cent
    staples_receipt["amount"] = 20.00  # within ±$0.01
    assert bank_core.match_receipt(db, basic_store, staples_receipt) == txns["STAPLES"]["id"]

    # unlink -> back to needs_receipt, not auto re-matched
    r = client.delete(f"/api/bank/transactions/{hd['id']}/match", headers=as_user(OWNER, basic_store))
    assert r.json()["status"] == "needs_receipt"
    again = [t for t in db.tables["rv_bank_transactions"] if t["id"] == hd["id"]]
    assert bank_core.match_transactions(db, basic_store, again) == 0
    # manual re-link
    r = client.post(f"/api/bank/transactions/{hd['id']}/match", json={"receiptId": "r2"}, headers=as_user(OWNER, basic_store))
    assert r.json()["status"] == "matched" and r.json()["receipt"]["id"] == "r2"


def test_personal_excluded_from_pnl_tax_and_report(db, basic_store, client):
    b = biz(db, basic_store)
    db.tables.setdefault("receipts", []).append({"id": "rr", "business_id": basic_store, "merchant": "Shell", "amount": 40.0, "receipt_date": "2026-10-02", "category": "Vehicle & Fuel"})
    run(bank_core.import_transactions(db, b, [
        {"posted_date": "2026-10-02", "description": "SHELL OIL 1234", "amount": -40.00},
        {"posted_date": "2026-10-03", "description": "NETFLIX", "amount": -15.49},
        {"posted_date": "2026-10-04", "description": "SYSCO FOODS", "amount": -200.00},
        {"posted_date": "2026-10-05", "description": "CARD SETTLEMENT", "amount": 1000.00},
    ], source="upload", account_ref="a", auto=True))
    t = {x["description"]: x for x in db.tables["rv_bank_transactions"]}
    assert t["SHELL OIL 1234"]["status"] == "matched"
    client.patch(f"/api/bank/transactions/{t['NETFLIX']['id']}", json={"isPersonal": True}, headers=as_user(OWNER, basic_store))

    pnl = client.get("/api/pnl?start=2026-10-01&end=2026-10-31", headers=as_user(OWNER, basic_store)).json()
    # 40 (Shell, counted once though it has a receipt) + 200 Sysco; Netflix personal; deposit not income
    assert pnl["totalOut"] == 240.0 and pnl["totalIn"] == 0

    # count the deposit as income
    client.patch(f"/api/bank/transactions/{t['CARD SETTLEMENT']['id']}", json={"countsAsIncome": True}, headers=as_user(OWNER, basic_store))
    pnl = client.get("/api/pnl?start=2026-10-01&end=2026-10-31", headers=as_user(OWNER, basic_store)).json()
    assert pnl["totalIn"] == 1000.0 and pnl["net"] == 760.0

    tax = client.get("/api/tax/quarterly-estimate?start=2026-10-01&end=2026-10-31", headers=as_user(OWNER, basic_store)).json()
    assert tax["periodNetIncome"] == 760.0

    rows = bank_core.business_transactions(db, basic_store, "2026-10-01", "2026-10-31")
    assert "NETFLIX" not in [r["description"] for r in rows]

    missing = client.get("/api/bank/missing-receipts", headers=as_user(OWNER, basic_store)).json()
    assert missing["count"] == 1 and missing["transactions"][0]["description"] == "SYSCO FOODS"

    # Next Netflix charge is personal automatically (vendor rule)
    run(bank_core.import_transactions(db, b, [{"posted_date": "2026-11-03", "description": "NETFLIX", "amount": -15.49}], source="upload", account_ref="a"))
    nov = [x for x in db.tables["rv_bank_transactions"] if x["posted_date"] == "2026-11-03"][0]
    assert nov["is_personal"] and nov["status"] == "personal"


def test_scan_receipt_for_a_charge_links_it(db, basic_store, client):
    b = biz(db, basic_store)
    run(bank_core.import_transactions(db, b, [{"posted_date": "2026-10-04", "description": "SYSCO FOODS", "amount": -200.00}], source="upload", account_ref="a", auto=False))
    txn = db.tables["rv_bank_transactions"][0]
    r = client.post(f"/api/bank/transactions/{txn['id']}/receipt", json={"imageBase64": "aGVsbG8=", "filename": "x.jpg"}, headers=as_user(OWNER, basic_store))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["transaction"]["status"] == "matched"
    assert body["receipt"]["amount"] == 200.0 and body["receipt"]["receiptDate"] == "2026-10-04"


def test_free_plan_cannot_sort_charges(db, client):
    s = make_first_store(client)
    db.tables.setdefault("rv_bank_transactions", []).append({"id": "t1", "business_id": s, "description": "X", "amount": -1, "status": "needs_receipt", "posted_date": "2026-10-01"})
    r = client.patch("/api/bank/transactions/t1", json={"isPersonal": True}, headers=as_user(OWNER, s))
    assert r.status_code == 402
    assert client.get("/api/bank/transactions", headers=as_user(OWNER, s)).status_code == 200


def test_categories_endpoint_is_unified():
    from routes.receiptvault.categories import normalize_category, line_for
    assert normalize_category("Office Supplies") == "Supplies"
    assert normalize_category("Fuel/Vehicle") == "Vehicle & Fuel"
    assert line_for("Meals & Entertainment") == "Line 24b"
    assert normalize_category("nonsense") == "Other"

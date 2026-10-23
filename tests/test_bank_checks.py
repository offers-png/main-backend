"""Bank 'CHECK # 26471' charges link to the Money Box check with the same
number (amount within $1) and are marked already counted."""

from conftest import as_user
from test_multi_store import OWNER
from test_bank import basic_store, biz, run  # noqa: F401 (fixture)
from routes.receiptvault import bank_core, bank_routes


def bank(db, store, i, amount, desc, status="needs_receipt", source="ai", day="2026-10-16"):
    row = {"id": f"b{i}", "business_id": store, "description": desc, "amount": amount, "status": status, "category": "Other",
           "category_source": source, "posted_date": day, "is_personal": False, "matched_receipt_id": None}
    db.tables.setdefault("rv_bank_transactions", []).append(row)
    return row


def money(db, store, i, amount, note, category="vendor_payment", entry_type="expense"):
    row = {"id": f"m{i}", "business_id": store, "entry_type": entry_type, "category": category, "amount": amount,
           "payment_method": "check", "note": note, "entry_date": "2026-10-10"}
    db.tables.setdefault("rv_money_entries", []).append(row)
    return row


def rows(db):
    return {t["id"]: t for t in db.tables["rv_bank_transactions"]}


def test_check_number_parsing():
    assert bank_core.bank_check_number("CHECK # 26471") == "26471"
    assert bank_core.bank_check_number("CHK#00257") == "257"
    assert bank_core.bank_check_number("CHECKCARD 1234 SHELL") is None
    assert bank_core.money_check_number("Check #27341 · Corona ") == "27341"
    assert bank_core.money_check_payee("Check #27341 · Corona ") == "Corona"
    assert bank_core.money_check_number("Cigar") is None


def test_matches_same_number_within_a_dollar_and_stops_double_counting(db, basic_store, client):
    s = basic_store
    bank(db, s, 1, -320.25, "CHECK # 27110")       # Money Box typed $320 -> match
    bank(db, s, 2, -443.00, "CHECK # 26871")       # exact -> match
    bank(db, s, 3, -200.00, "CHECK # 26042")       # Money Box says $422 -> leave
    bank(db, s, 4, -500.00, "CHECK # 99999")       # no Money Box check -> leave
    bank(db, s, 5, -150.00, "CHECK # 27200", source="user")   # owner already handled -> leave
    bank(db, s, 6, -75.00, "CHECK # 30000")        # two Money Box checks #30000 -> ambiguous, leave
    money(db, s, 1, 320, "Check #27110 · Corona ")
    money(db, s, 2, 443, "Check #26871 · Budwiser ")
    money(db, s, 3, 422, "Check #26042 · Buffalo")
    money(db, s, 4, 150, "Check #27200 · Pepsi")
    money(db, s, 5, 75, "Check #30000 · A")
    money(db, s, 6, 75, "Check #30000 · B")
    money(db, s, 7, 320.25, "Check #27110 · Corona", entry_type="income")  # income never matches

    r = client.get("/api/bank/transactions", headers=as_user(OWNER, s))
    assert r.status_code == 200, r.text
    t = rows(db)
    assert t["b1"]["matched_money_entry_id"] == "m1" and t["b1"]["status"] == "ignored"
    assert t["b1"]["vendor"] == "Corona" and t["b1"]["category"] == "Inventory" and t["b1"]["schedule_c_line"] == "Line 4"
    assert t["b2"]["matched_money_entry_id"] == "m2" and t["b2"]["vendor"] == "Budwiser"
    for i in ("b3", "b4", "b5", "b6"):
        assert t[i]["status"] == "needs_receipt" and not t[i].get("matched_money_entry_id"), i
    api = {x["id"]: x for x in r.json()["transactions"]}
    assert api["b1"]["matchedMoneyEntryId"] == "m1" and api["b1"]["checkNumber"] == "27110" and api["b1"]["vendor"] == "Corona"

    # counted once: the matched bank checks are out of the books (Money Box has them)
    books = {x["id"] for x in bank_core.business_transactions(db, s, "2026-10-01", "2026-10-31")}
    assert "b1" not in books and "b2" not in books and "b3" in books
    # checks never sit on the "needs a receipt" list (an invoice is optional)
    assert not client.get("/api/bank/transactions?status=needs_receipt", headers=as_user(OWNER, s)).json()["transactions"]
    assert bank_routes._counts(db.tables["rv_bank_transactions"])["ignored"] == 2

    # a Money Box check can't be used twice
    bank(db, s, 7, -320.10, "CHECK # 27110", day="2026-10-20")
    bank_core.match_checks(db, s)
    assert rows(db)["b7"]["status"] == "needs_receipt"


def test_not_the_same_check_and_deleting_the_money_box_entry(db, basic_store, client):
    s = basic_store
    h = as_user(OWNER, s)
    bank(db, s, 1, -320.25, "CHECK # 27110")
    bank(db, s, 2, -100.00, "CHECK # 500")
    money(db, s, 1, 320, "Check #27110 · Corona")
    money(db, s, 2, 100, "Check #500 · Pepsi")
    client.get("/api/bank/transactions", headers=h)
    assert rows(db)["b1"]["status"] == "ignored" and rows(db)["b2"]["status"] == "ignored"

    r = client.delete("/api/bank/transactions/b1/money-match", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "needs_receipt" and r.json()["matchedMoneyEntryId"] is None
    client.get("/api/bank/transactions", headers=h)
    assert rows(db)["b1"]["status"] == "needs_receipt"   # owner said no: never re-matched

    client.delete("/api/money-entries/m2", headers=h)
    assert rows(db)["b2"]["status"] == "needs_receipt" and rows(db)["b2"]["matched_money_entry_id"] is None


def test_new_statement_and_new_money_box_check_both_match(db, basic_store, client):
    s = basic_store
    h = as_user(OWNER, s)
    money(db, s, 1, 647, "Check #26471 · Coke")
    res = run(bank_core.import_transactions(db, biz(db, s), [
        {"posted_date": "2026-10-21", "description": "CHECK # 26471", "amount": -647.34},
        {"posted_date": "2026-10-21", "description": "CHECK # 26472", "amount": -473.21},
    ], source="upload", account_ref="a"))
    assert res["checksMatched"] == 1
    by_desc = {t["description"]: t for t in db.tables["rv_bank_transactions"]}
    assert by_desc["CHECK # 26471"]["status"] == "ignored" and by_desc["CHECK # 26472"]["status"] == "needs_receipt"

    # owner writes the second check in Money Box later -> linked right away
    r = client.post("/api/money-entries", json={"entryDate": "2026-10-16", "entryType": "expense", "category": "vendor_payment",
                                                "amount": 473, "paymentMethod": "check", "note": "Check #26472 · Pepsi"}, headers=h)
    assert r.status_code == 200, r.text
    assert by_desc["CHECK # 26472"]["status"] == "ignored" and by_desc["CHECK # 26472"]["vendor"] == "Pepsi"


def test_unmatched_checks_dont_need_a_receipt(db, basic_store, client):
    s = basic_store
    bank(db, s, 1, -906.26, "CHECK # 26750")
    bank(db, s, 2, -906.26, "STAPLES")
    assert not bank_core.receipt_required(rows(db)["b1"]) and bank_core.receipt_required(rows(db)["b2"])
    r = client.get("/api/bank/transactions?status=needs_receipt", headers=as_user(OWNER, s)).json()
    assert [t["id"] for t in r["transactions"]] == ["b2"]
    assert r["counts"]["goal"]["bigCharges"] == 1

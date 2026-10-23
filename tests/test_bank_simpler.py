"""Less overwhelming bank list: $75 receipt rule, obvious vendors, learning
from edits, bulk actions, and a monthly goal instead of an all-time total."""

from datetime import date

from conftest import as_user
from test_multi_store import OWNER
from test_bank import basic_store, biz, run  # noqa: F401 (fixture)
from routes.receiptvault import bank_core, bank_routes


def add(db, store, i, amount, desc="SOME SHOP", day="2026-10-05", status="needs_receipt", category="Other", source="ai"):
    row = {"id": f"t{i}", "business_id": store, "description": desc, "amount": amount, "status": status,
           "category": category, "category_source": source, "posted_date": day, "is_personal": False}
    db.tables.setdefault("rv_bank_transactions", []).append(row)
    return row


def test_only_75_and_over_or_travel_needs_a_receipt():
    assert bank_core.receipt_required({"status": "needs_receipt", "amount": -75.00})
    assert not bank_core.receipt_required({"status": "needs_receipt", "amount": -74.99})
    assert bank_core.receipt_required({"status": "needs_receipt", "amount": -40, "category": "Travel"})  # hotels always
    assert not bank_core.receipt_required({"status": "matched", "amount": -500})
    assert not bank_core.receipt_required({"status": "personal", "amount": -500})


def test_list_and_counts_use_the_rule_and_this_months_goal(db, basic_store, client):
    s = basic_store
    add(db, s, 1, -120, day="2026-10-02")
    add(db, s, 2, -12.50, desc="DUNKIN", day="2026-10-03")
    add(db, s, 3, -80, day="2026-10-04", status="matched")
    add(db, s, 4, -300, day="2026-09-15")                          # earlier month backlog
    add(db, s, 5, -30, desc="HOTEL", day="2026-10-06", category="Travel")
    r = client.get("/api/bank/transactions?status=needs_receipt", headers=as_user(OWNER, s)).json()
    assert {t["id"] for t in r["transactions"]} == {"t1", "t4", "t5"}
    assert all(t["receiptRequired"] for t in r["transactions"])
    small = client.get("/api/bank/transactions?status=no_receipt_needed", headers=as_user(OWNER, s)).json()
    assert [t["id"] for t in small["transactions"]] == ["t2"] and not small["transactions"][0]["receiptRequired"]

    c = bank_routes._counts(db.tables["rv_bank_transactions"], today=date(2026, 10, 23))
    assert c["needsReceipt"] == 3 and c["noReceiptNeeded"] == 1
    g = c["goal"]
    assert (g["month"], g["needReceipt"], g["haveReceipt"], g["bigCharges"], g["earlier"]) == ("2026-10", 2, 1, 3, 1)
    assert g["needReceiptAmount"] == 150.0 and g["isCurrentMonth"]
    # nothing in November yet -> goal stays on the newest month with charges
    g2 = bank_routes._goal(db.tables["rv_bank_transactions"], today=date(2026, 11, 2))
    assert g2["month"] == "2026-10" and not g2["isCurrentMonth"]


def test_obvious_vendors_sorted_and_old_ai_guesses_fixed(db, basic_store, client, monkeypatch):
    s = basic_store
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    run(bank_core.import_transactions(db, biz(db, s), [
        {"posted_date": "2026-10-02", "description": "E-Z PASSNY PAYMENT NEW YORK NY", "amount": -25.0},
        {"posted_date": "2026-10-03", "description": "POS MAC SPEEDWAY 41234 LIVERPOOL NY", "amount": -40.0},
        {"posted_date": "2026-10-04", "description": "VERIZON BILL PAYMENT", "amount": -135.44},
        {"posted_date": "2026-10-05", "description": "STATE FARM INSURANCE", "amount": -140.0},
        {"posted_date": "2026-10-06", "description": "RENT PAYMENT 12 MAIN ST", "amount": -1800.0},
        {"posted_date": "2026-10-07", "description": "NGRID06 NGRID WEB", "amount": -300.0},
        {"posted_date": "2026-10-08", "description": "SHELLY CAFE", "amount": -9.0},
    ], source="upload", account_ref="a"))
    cats = {t["description"]: (t["category"], t["category_source"]) for t in db.tables["rv_bank_transactions"]}
    assert cats["E-Z PASSNY PAYMENT NEW YORK NY"] == ("Vehicle & Fuel", "known")
    assert cats["POS MAC SPEEDWAY 41234 LIVERPOOL NY"] == ("Vehicle & Fuel", "known")
    assert cats["VERIZON BILL PAYMENT"] == ("Utilities", "known")
    assert cats["STATE FARM INSURANCE"] == ("Insurance", "known")
    assert cats["RENT PAYMENT 12 MAIN ST"] == ("Rent", "known")
    assert cats["NGRID06 NGRID WEB"] == ("Utilities", "known")
    assert cats["SHELLY CAFE"][0] == "Other"  # not mistaken for Shell

    # Older AI guesses get fixed when the list is opened; the owner's own choices never change
    add(db, s, 90, -60, desc="EZPASS REBILL", category="Other", source="ai")
    add(db, s, 91, -60, desc="EZPASS REBILL 2", category="Meals", source="user")
    client.get("/api/bank/transactions", headers=as_user(OWNER, s))
    rows = {t["id"]: t for t in db.tables["rv_bank_transactions"]}
    assert rows["t90"]["category"] == "Vehicle & Fuel" and rows["t90"]["schedule_c_line"] == "Line 9"
    assert rows["t91"]["category"] == "Meals"


def test_ai_is_shown_the_owners_own_choices(db, basic_store, http_calls, monkeypatch, client):
    s = basic_store
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    prompts = []

    def responder(method, url, kw):
        if "anthropic" in url:
            prompts.append(kw["json"]["messages"][0]["content"])
            return http_calls.Response(200, {"content": [{"text": '{"0": "Inventory"}'}]})
        return http_calls.Response(200, {})
    http_calls.state["responder"] = responder
    t = add(db, s, 1, -50, desc="BYRNE DAIRY 123", category="Supplies")
    client.patch(f"/api/bank/transactions/{t['id']}", json={"category": "Inventory"}, headers=as_user(OWNER, s))
    run(bank_core.import_transactions(db, biz(db, s), [{"posted_date": "2026-10-09", "description": "MAC BYRNE DAIRY LIVERPOOL", "amount": -20.0}],
                                      source="upload", account_ref="a"))
    assert prompts and '"byrne dairy" -> Inventory' in prompts[-1]


def test_bulk_set_category_personal_and_already_counted(db, basic_store, client):
    s = basic_store
    add(db, s, 1, -100, desc="ALDI 111", category="Other")
    add(db, s, 2, -90, desc="ALDI 222", category="Other")
    add(db, s, 3, -200, desc="COSTCO WHSE", category="Other")
    add(db, s, 4, -95, desc="ALDI 333", day="2026-08-01", category="Other")   # not selected: learns from the rule
    add(db, s, 5, 500, desc="DEPOSIT", status="ignored", category="Deposit")
    add(db, s, 6, -150, desc="NETFLIX", category="Software & Subscriptions")
    add(db, s, 7, -110, desc="CHECK 101", category="Other")
    h = as_user(OWNER, s)

    r = client.post("/api/bank/transactions/bulk", json={"ids": ["t1", "t2", "t3", "t5"], "category": "Inventory"}, headers=h)
    assert r.status_code == 200, r.text
    rows = {t["id"]: t for t in db.tables["rv_bank_transactions"]}
    assert all(rows[i]["category"] == "Inventory" and rows[i]["category_source"] == "user" for i in ("t1", "t2", "t3"))
    assert rows["t5"]["category"] == "Deposit"            # money in skipped
    assert rows["t4"]["category"] == "Inventory"          # same vendor, learned
    assert r.json()["updated"] == 3 and r.json()["vendorsLearned"] == 2 and r.json()["alsoUpdated"] == 1
    assert {x["vendor_pattern"] for x in db.tables["rv_vendor_rules"]} >= {"aldi", "costco whse"}

    r = client.post("/api/bank/transactions/bulk", json={"ids": ["t6"], "isPersonal": True}, headers=h)
    assert rows["t6"]["status"] == "personal" and rows["t6"]["is_personal"]
    r = client.post("/api/bank/transactions/bulk", json={"ids": ["t7"], "ignored": True}, headers=h)
    assert rows["t7"]["status"] == "ignored"

    assert client.post("/api/bank/transactions/bulk", json={"ids": []}, headers=h).status_code == 400
    assert client.post("/api/bank/transactions/bulk", json={"ids": ["t1"]}, headers=h).status_code == 400
    # another store's charge can't be touched
    add(db, "other-store", 99, -100, desc="X")
    client.post("/api/bank/transactions/bulk", json={"ids": ["t99"], "isPersonal": True}, headers=h)
    assert not next(t for t in db.tables["rv_bank_transactions"] if t["id"] == "t99")["is_personal"]


def test_free_plan_cannot_bulk_edit(db, client):
    from test_multi_store import make_first_store
    s = make_first_store(client)
    add(db, s, 1, -100)
    r = client.post("/api/bank/transactions/bulk", json={"ids": ["t1"], "isPersonal": True}, headers=as_user(OWNER, s))
    assert r.status_code == 402

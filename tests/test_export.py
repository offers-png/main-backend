"""Phase 7: CSV export per store and for all stores; personal left out by
default; a matched receipt appears once."""

import asyncio
import csv
import io

from conftest import as_user
from test_multi_store import OWNER, make_first_store, finish_setup, paid_owner  # noqa: F401
from routes.receiptvault import bank_core


def rows(resp):
    assert resp.status_code == 200, resp.text
    return list(csv.DictReader(io.StringIO(resp.text.lstrip("﻿"))))


def test_export_store_and_all_stores(client, db, paid_owner):
    db.unique["rv_bank_transactions"] = [("business_id", "dedupe_key")]
    a = make_first_store(client)
    b = client.post("/api/stores", json={"businessName": "Store B", "businessAddress": "x"}, headers=as_user(OWNER)).json()["id"]
    for s in (a, b):
        finish_setup(client, OWNER, s)
    biz_a = next(x for x in db.tables["businesses"] if x["id"] == a)
    db.tables.setdefault("receipts", []).extend([
        {"id": "r1", "business_id": a, "merchant": "Home Depot", "amount": 52.10, "receipt_date": "2026-10-01", "category": "Supplies", "schedule_c_line": "Line 22", "file_path": "https://f/r1.jpg"},
        {"id": "r2", "business_id": a, "merchant": "=HYPERLINK(evil)", "amount": 9.00, "receipt_date": "2026-10-05", "category": "Other", "file_path": "https://f/r2.jpg"},
    ])
    asyncio.run(bank_core.import_transactions(db, biz_a, [
        {"posted_date": "2026-10-02", "description": "HOME DEPOT #1", "amount": -52.10},
        {"posted_date": "2026-10-03", "description": "NETFLIX", "amount": -15.49},
        {"posted_date": "2026-10-04", "description": "DEPOSIT", "amount": 300.0},
    ], source="upload", account_ref="chase", auto=False))
    netflix = next(t for t in db.tables["rv_bank_transactions"] if t["description"] == "NETFLIX")
    netflix.update(is_personal=True, status="personal")
    bank_core.match_transactions(db, a, [t for t in db.tables["rv_bank_transactions"] if t["status"] == "needs_receipt"])
    client.post("/api/money-entries", json={"entryDate": "2026-10-06", "entryType": "expense", "category": "vendor_payment", "amount": 80, "paymentMethod": "cash", "note": "Pepsi guy"}, headers=as_user(OWNER, a))
    client.post("/api/money-entries", json={"entryDate": "2026-10-06", "entryType": "expense", "category": "other_expense", "amount": 5, "paymentMethod": "cash"}, headers=as_user(OWNER, b))

    out = rows(client.get("/api/export/expenses.csv?start=2026-10-01&end=2026-10-31", headers=as_user(OWNER, a)))
    vendors = [r["Vendor"] for r in out]
    assert list(out[0].keys()) == ["Date", "Vendor", "Amount", "Category", "Schedule C line", "Payment method", "Has receipt (Y/N)", "Receipt link", "Notes"]
    assert "NETFLIX" not in vendors and "DEPOSIT" not in vendors
    hd = [r for r in out if "HOME DEPOT" in r["Vendor"].upper()]
    assert len(hd) == 1 and hd[0]["Has receipt (Y/N)"] == "Y" and hd[0]["Receipt link"] == "https://f/r1.jpg"
    assert any(r["Vendor"] == "'=HYPERLINK(evil)" for r in out)
    assert any(r["Vendor"] == "Pepsi guy" and r["Payment method"] == "Cash" for r in out)
    assert len(out) == 3

    with_personal = rows(client.get("/api/export/expenses.csv?start=2026-10-01&end=2026-10-31&includePersonal=true", headers=as_user(OWNER, a)))
    assert any(r["Vendor"] == "NETFLIX" and r["Notes"] == "Personal" for r in with_personal)

    both = rows(client.get("/api/export/expenses.csv?start=2026-10-01&end=2026-10-31&scope=all", headers=as_user(OWNER, a)))
    assert {r["Store"] for r in both} == {"Store A", "Store B"} and len(both) == 4

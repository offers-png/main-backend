"""Receipt History: month folders (receipt date, else upload date), search
and filters across months, paging, all stores, and Download month."""

import csv
import io
import zipfile

from conftest import as_user
from test_multi_store import OWNER, OTHER, make_first_store, finish_setup, add_store, paid_owner  # noqa: F401


def _seed(db, store_a, store_b):
    db.storage_files[("receipts", "a/r1.jpg")] = b"jpeg-1"
    db.storage_files[("receipts", "a/r2.pdf")] = b"%PDF-2"
    db.tables.setdefault("receipts", []).extend([
        {"id": "r1", "business_id": store_a, "merchant": "Home Depot", "amount": 52.10, "receipt_date": "2026-02-03", "category": "Supplies", "file_path": "a/r1.jpg", "uploaded_at": "2026-03-01T10:00:00"},
        {"id": "r2", "business_id": store_a, "merchant": "Verizon", "amount": 120.00, "receipt_date": "2026-02-20", "category": "Utilities", "notes": "phone bill", "file_path": "a/r2.pdf", "uploaded_at": "2026-03-01T10:00:00"},
        # no receipt date -> falls into the month it was uploaded
        {"id": "r3", "business_id": store_a, "merchant": "=EVIL()", "amount": 9.00, "receipt_date": None, "category": "Other", "file_path": "a/missing.jpg", "uploaded_at": "2025-12-15T08:00:00"},
        {"id": "r4", "business_id": store_b, "merchant": "Costco", "amount": 300.00, "receipt_date": "2026-02-10", "category": "Inventory", "file_path": None, "uploaded_at": "2026-02-10T08:00:00"},
    ])
    for r in db.tables["receipts"]:
        r["history_date"] = r["receipt_date"] or r["uploaded_at"][:10]


def _two_stores(client):
    a = make_first_store(client)
    b = add_store(client, "Store B")
    for s in (a, b):
        finish_setup(client, OWNER, s)
    return a, b


def test_month_folders_per_store_and_all_stores(client, db, paid_owner):
    a, b = _two_stores(client)
    _seed(db, a, b)
    months = client.get("/api/history/months", headers=as_user(OWNER, a)).json()["months"]
    assert months == [
        {"month": "2026-02", "count": 2, "total": 172.10},
        {"month": "2025-12", "count": 1, "total": 9.00},
    ]
    both = client.get("/api/history/months?scope=all", headers=as_user(OWNER, a)).json()["months"]
    assert both[0] == {"month": "2026-02", "count": 3, "total": 472.10}


def test_month_folders_use_database_grouping_when_available(client, db, paid_owner):
    a, b = _two_stores(client)
    seen = {}

    def rpc(params):
        seen.update(params)
        return [{"month": "2026-02", "receipt_count": 34, "total": "8412.50"}]

    db.rpc_handlers["rk_receipt_months"] = rpc
    months = client.get("/api/history/months", headers=as_user(OWNER, a)).json()["months"]
    assert months == [{"month": "2026-02", "count": 34, "total": 8412.5}]
    assert seen["p_business_ids"] == [a]


def test_open_month_search_and_filters(client, db, paid_owner):
    a, b = _two_stores(client)
    _seed(db, a, b)
    h = as_user(OWNER, a)

    feb = client.get("/api/history/receipts?month=2026-02", headers=h).json()
    assert [i["id"] for i in feb["items"]] == ["r2", "r1"] and feb["total"] == 2
    assert feb["items"][0]["month"] == "2026-02" and feb["items"][0]["filePath"].startswith("https://signed.example/")

    def ids(qs, headers=h):
        r = client.get(f"/api/history/receipts?{qs}", headers=headers)
        assert r.status_code == 200, r.text
        return sorted(i["id"] for i in r.json()["items"])

    assert ids("q=depot") == ["r1"]
    assert ids("q=phone") == ["r2"]          # notes
    assert ids("q=utilities") == ["r2"]      # category
    assert ids("q=$52.10") == ["r1"]         # amount
    assert ids("q=a%22,merchant.eq.x") == []  # can't inject extra filters
    assert ids("category=Other") == ["r3"]
    assert ids("min=50&max=100") == ["r1"]
    assert ids("start=2026-01-01&end=2026-02-10") == ["r1"]
    assert ids("q=costco") == []             # other store
    allr = client.get("/api/history/receipts?scope=all&q=costco", headers=h).json()["items"]
    assert [(i["id"], i["storeName"], i["month"]) for i in allr] == [("r4", "Store B", "2026-02")]

    assert client.get("/api/history/receipts?start=02/01/2026", headers=h).status_code == 400
    assert client.get("/api/history/receipts?month=2026-13", headers=h).status_code == 400


def test_paging(client, db, paid_owner):
    a, b = _two_stores(client)
    db.tables["receipts"] = [
        {"id": f"p{i:03d}", "business_id": a, "merchant": f"M{i}", "amount": 1, "receipt_date": f"2026-01-{(i % 28) + 1:02d}", "uploaded_at": "2026-02-01T00:00:00"}
        for i in range(120)
    ]
    for r in db.tables["receipts"]:
        r["history_date"] = r["receipt_date"]
    p1 = client.get("/api/history/receipts?month=2026-01&pageSize=50", headers=as_user(OWNER, a)).json()
    p3 = client.get("/api/history/receipts?month=2026-01&pageSize=50&page=3", headers=as_user(OWNER, a)).json()
    assert p1["total"] == 120 and len(p1["items"]) == 50 and len(p3["items"]) == 20
    assert not {i["id"] for i in p1["items"]} & {i["id"] for i in p3["items"]}
    big = client.get("/api/history/receipts?pageSize=5000", headers=as_user(OWNER, a)).json()
    assert big["pageSize"] == 100


def test_download_month_zip_and_pdf(client, db, paid_owner, monkeypatch):
    import pdf_generator
    calls = []

    async def fake_pdf(**kw):
        calls.append(kw)
        return b"%PDF-report"

    monkeypatch.setattr(pdf_generator, "generate_cover_pdf", fake_pdf)
    a, b = _two_stores(client)
    _seed(db, a, b)
    h = as_user(OWNER, a)

    r = client.get("/api/history/months/2026-02/download", headers=h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {"receipts/2026-02-03_Home-Depot_52.10.jpg", "receipts/2026-02-20_Verizon_120.00.pdf", "receipts-2026-02.csv", "accountant-report-2026-02.pdf"} == names
    assert z.read("receipts/2026-02-03_Home-Depot_52.10.jpg") == b"jpeg-1"
    rows = list(csv.DictReader(io.StringIO(z.read("receipts-2026-02.csv").decode("utf-8").lstrip("﻿"))))
    assert [x["Merchant"] for x in rows] == ["Home Depot", "Verizon"] and rows[0]["Amount"] == "52.10"
    assert calls[0]["period_label"] == "February 2026 Expenses" and len(calls[0]["receipts"]) == 2

    # missing file is listed, formula text is neutralised
    dec = zipfile.ZipFile(io.BytesIO(client.get("/api/history/months/2025-12/download", headers=h).content))
    row = list(csv.DictReader(io.StringIO(dec.read("receipts-2025-12.csv").decode("utf-8").lstrip("﻿"))))[0]
    assert row["File"] == "(file missing)" and row["Merchant"] == "'=EVIL()"

    pdf = client.get("/api/history/months/2026-02/download?kind=pdf", headers=h)
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content == b"%PDF-report"

    allz = zipfile.ZipFile(io.BytesIO(client.get("/api/history/months/2026-02/download?scope=all", headers=h).content))
    assert any(n.startswith("receipts/Store-A/") for n in allz.namelist())
    assert {"accountant-report-store-a-2026-02.pdf", "accountant-report-store-b-2026-02.pdf"} <= set(allz.namelist())

    assert client.get("/api/history/months/2024-01/download", headers=h).status_code == 404


def test_access(client, db, paid_owner):
    a, b = _two_stores(client)
    _seed(db, a, b)
    # someone else can't name this store
    assert client.get("/api/history/months", headers=as_user(OTHER, a)).status_code == 403
    # a team member without the receipts module, and a member asking for all stores
    db.tables.setdefault("business_users", []).append({"id": "m1", "business_id": a, "user_id": OTHER, "status": "active", "role": "manager", "modules": ["money_box"]})
    assert client.get("/api/history/months", headers=as_user(OTHER, a)).status_code == 403
    db.tables["business_users"][-1]["modules"] = ["receipts"]
    assert client.get("/api/history/months", headers=as_user(OTHER, a)).status_code == 200
    assert client.get("/api/history/months?scope=all", headers=as_user(OTHER, a)).status_code == 403

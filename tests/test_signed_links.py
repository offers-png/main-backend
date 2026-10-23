"""Receipt files live in a private bucket: every link handed out is signed."""

import csv
import io

from conftest import as_user
from test_multi_store import OWNER, make_first_store, finish_setup
from routes.receiptvault import files


def test_object_path_handles_old_and_new_formats():
    assert files.object_path("https://x.supabase.co/storage/v1/object/public/receipts/a/b.jpg") == "a/b.jpg"
    assert files.object_path("https://x.supabase.co/storage/v1/object/sign/receipts/a.pdf?token=t") == "a.pdf"
    assert files.object_path("statements/s1/x.csv") == "statements/s1/x.csv"
    assert files.object_path("https://elsewhere.com/x.jpg") is None


def test_receipts_come_back_with_signed_links(client, db):
    s = make_first_store(client)
    finish_setup(client, OWNER, s)
    up = client.post("/api/receipts", json={"imageBase64": "aGVsbG8=", "filename": "r.jpg"}, headers=as_user(OWNER, s)).json()
    stored = db.tables["receipts"][0]["file_path"]
    assert "/object/public/receipts/" in stored  # what the database keeps
    assert up["filePath"].startswith("https://signed.example/") and "expires=3600" in up["filePath"]
    lst = client.get("/api/receipts", headers=as_user(OWNER, s)).json()
    one = client.get(f"/api/receipts/{up['id']}", headers=as_user(OWNER, s)).json()
    assert "expires=3600" in lst[0]["filePath"] and "expires=3600" in one["filePath"]
    # a file that's missing from storage keeps its stored value instead of breaking the page
    db.storage_files.clear()
    assert client.get("/api/receipts", headers=as_user(OWNER, s)).json()[0]["filePath"] == stored


def test_csv_export_links_last_30_days(client, db):
    s = make_first_store(client)
    finish_setup(client, OWNER, s)
    client.post("/api/receipts", json={"imageBase64": "aGVsbG8=", "filename": "r.jpg"}, headers=as_user(OWNER, s))
    db.tables["receipts"][0].update(amount=12.5, receipt_date="2026-10-02")
    text = client.get("/api/export/expenses.csv?start=2026-10-01&end=2026-10-31", headers=as_user(OWNER, s)).text
    row = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))[0]
    assert row["Receipt link"].startswith("https://signed.example/") and f"expires={30 * 86400}" in row["Receipt link"]

"""
Bank / credit-card statement parsing for ReceiptKept.

CSV: finds the header row (skipping the summary lines some banks put on
top), auto-detects date / description / amount — or separate debit and
credit columns — and handles headerless exports (Wells Fargo). Works with
the common US layouts: Chase (checking + card), Bank of America, Wells
Fargo, Citi, Capital One, KeyBank, M&T, Amex, Discover and credit unions.
If detection fails, the caller gets the headers + sample rows back so the
person can pick "which column is the date / amount / description".

PDF: the existing AI provider reads the transactions; the caller shows a
preview before anything is saved.

Output rows: {"posted_date": "YYYY-MM-DD", "description": str, "amount": float}
with amount < 0 = money out.
"""

import base64
import csv
import io
import json
import os
import re
from datetime import date, datetime
from typing import Optional

import httpx

DATE_HEADERS = ["posting date", "post date", "posted date", "transaction date", "trans date", "trans. date", "date posted", "date"]
DESC_HEADERS = ["description", "transaction description", "payee", "merchant", "name", "details", "memo", "narrative", "transaction"]
AMOUNT_HEADERS = ["amount", "transaction amount", "amount ($)", "amt"]
DEBIT_HEADERS = ["debit", "debits", "withdrawal", "withdrawals", "debit amount", "money out", "charges", "charge", "amount debit", "payments and debits"]
CREDIT_HEADERS = ["credit", "credits", "deposit", "deposits", "credit amount", "money in", "amount credit", "deposits and credits"]
TYPE_HEADERS = ["type", "transaction type", "credit/debit", "debit/credit", "dr/cr", "details"]
BALANCE_HEADERS = ["balance", "running bal.", "running balance", "available balance"]

BANK_HINTS = [
    ("chase", "Chase"), ("bank of america", "Bank of America"), ("bofa", "Bank of America"),
    ("wells", "Wells Fargo"), ("citi", "Citi"), ("capital one", "Capital One"), ("capitalone", "Capital One"),
    ("keybank", "KeyBank"), ("key bank", "KeyBank"), ("m&t", "M&T Bank"), ("mtb", "M&T Bank"),
    ("amex", "American Express"), ("american express", "American Express"), ("discover", "Discover"),
    ("credit union", "Credit Union"), ("fcu", "Credit Union"), ("td bank", "TD Bank"), ("pnc", "PNC"),
]

MAX_ROWS = 5000


class MappingNeeded(Exception):
    def __init__(self, headers: list, sample: list, reason: str):
        super().__init__(reason)
        self.headers = headers
        self.sample = sample
        self.reason = reason


# ── value parsing ───────────────────────────────────────────────────────────

_DATE_FORMATS = ["%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%m-%d-%y", "%d-%b-%y", "%d-%b-%Y", "%b %d, %Y", "%Y/%m/%d", "%d %b %Y"]


def parse_date(v) -> Optional[str]:
    s = str(v or "").strip().strip('"')
    if not s:
        return None
    s = s.split(" ")[0] if re.match(r"^\d{1,4}[/-]\d{1,2}[/-]\d{1,4}\s", s) else s
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(s, fmt).date()
            if 2000 <= d.year <= date.today().year + 1:
                return d.isoformat()
        except ValueError:
            continue
    return None


def parse_amount(v) -> Optional[float]:
    s = str(v if v is not None else "").strip().strip('"')
    if not s:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    up = s.upper()
    if up.endswith("CR"):
        s = s[:-2]
    elif up.endswith("DR"):
        neg, s = True, s[:-2]
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    if s.startswith("-"):
        neg, s = not neg if s.count("-") == 1 else neg, s.lstrip("-")
    if s.startswith("+"):
        s = s[1:]
    try:
        val = float(s)
    except ValueError:
        return None
    return -val if neg else val


def guess_bank(*texts: str) -> Optional[str]:
    blob = " ".join(t for t in texts if t).lower()
    for needle, name in BANK_HINTS:
        if needle in blob:
            return name
    return None


# ── CSV ─────────────────────────────────────────────────────────────────────

def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            if enc == "utf-16" and "\x00" not in raw[:200].decode("latin-1"):
                continue
            return text
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _rows(text: str) -> list:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    return [[c.strip() for c in r] for r in rows if any(c.strip() for c in r)]


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower().replace("_", " "))


def _find(headers: list, names: list) -> Optional[int]:
    hs = [_norm(h) for h in headers]
    for name in names:  # exact first, in priority order
        if name in hs:
            return hs.index(name)
    for name in names:
        for i, h in enumerate(hs):
            if h.startswith(name) or (len(name) > 4 and name in h):
                return i
    return None


def _header_index(rows: list) -> Optional[int]:
    """The first row (in the top 25) that names a date column and an
    amount/debit/credit column."""
    for i, r in enumerate(rows[:25]):
        if _find(r, DATE_HEADERS) is not None and (
            _find(r, AMOUNT_HEADERS) is not None or _find(r, DEBIT_HEADERS) is not None or _find(r, CREDIT_HEADERS) is not None
        ):
            return i
    return None


def detect_mapping(headers: list) -> Optional[dict]:
    m = {
        "date": _find(headers, DATE_HEADERS),
        "description": _find(headers, DESC_HEADERS),
        "amount": _find(headers, AMOUNT_HEADERS),
        "debit": _find(headers, DEBIT_HEADERS),
        "credit": _find(headers, CREDIT_HEADERS),
        "type": _find(headers, TYPE_HEADERS),
        "balance": _find(headers, BALANCE_HEADERS),
    }
    if m["type"] in (m["description"], m["amount"], m["debit"], m["credit"]):
        m["type"] = None
    if m["date"] is None or m["description"] is None:
        return None
    if m["amount"] is None and m["debit"] is None and m["credit"] is None:
        return None
    if m["amount"] is not None and (m["debit"] is not None or m["credit"] is not None):
        m["debit"] = m["credit"] = None  # prefer the signed amount column
    return m


def _headerless_mapping(rows: list) -> Optional[dict]:
    """Wells Fargo style: "10/01/2026","-4.50","*","","COFFEE SHOP" — no
    header. Find the date column, the money column and the longest text."""
    sample = rows[:20]
    if not sample:
        return None
    ncols = max(len(r) for r in sample)
    def share(fn, i):
        vals = [r[i] for r in sample if i < len(r) and r[i]]
        return (sum(1 for v in vals if fn(v)) / len(vals)) if vals else 0
    date_col = next((i for i in range(ncols) if share(parse_date, i) > 0.8), None)
    if date_col is None:
        return None
    amt_col = next((i for i in range(ncols) if i != date_col and share(lambda v: parse_amount(v) is not None, i) > 0.8), None)
    if amt_col is None:
        return None
    text_cols = [i for i in range(ncols) if i not in (date_col, amt_col)]
    if not text_cols:
        return None
    desc_col = max(text_cols, key=lambda i: sum(len(r[i]) for r in sample if i < len(r)))
    return {"date": date_col, "description": desc_col, "amount": amt_col, "debit": None, "credit": None, "type": None, "balance": None}


def _apply(rows: list, m: dict, flip: Optional[bool]) -> tuple:
    out, skipped = [], 0
    for r in rows:
        def cell(key):
            i = m.get(key)
            return r[i] if i is not None and i < len(r) else ""
        d = parse_date(cell("date"))
        desc = cell("description")
        if not d:
            skipped += 1
            continue
        if m.get("amount") is not None:
            amt = parse_amount(cell("amount"))
            if amt is None:
                skipped += 1
                continue
            t = _norm(cell("type"))
            if t in ("debit", "dr", "withdrawal", "sale", "purchase") and amt > 0:
                amt = -amt
            elif t in ("credit", "cr", "deposit", "payment", "return", "refund") and amt < 0:
                amt = -amt
        else:
            debit = parse_amount(cell("debit"))
            credit = parse_amount(cell("credit"))
            if debit is None and credit is None:
                skipped += 1
                continue
            amt = (abs(credit) if credit else 0.0) - (abs(debit) if debit else 0.0)
        if amt == 0:
            skipped += 1
            continue
        out.append({"posted_date": d, "description": desc or "(no description)", "amount": round(amt, 2)})

    # Card exports from Amex/Discover show purchases as POSITIVE numbers and
    # have no balance column. If almost everything is "money in", flip.
    if flip is None and m.get("amount") is not None and m.get("balance") is None and out:
        positives = sum(1 for x in out if x["amount"] > 0)
        flip = positives / len(out) > 0.75
    if flip:
        for x in out:
            x["amount"] = -x["amount"]
    return out, skipped, bool(flip)


def parse_csv(raw: bytes, mapping: Optional[dict] = None, flip: Optional[bool] = None) -> dict:
    text = _decode(raw)
    rows = _rows(text)
    if not rows:
        raise ValueError("That file looks empty.")
    if mapping:
        start = int(mapping.get("headerRow", 0)) + (1 if mapping.get("hasHeader", True) else 0)
        m = {k: (int(v) if v is not None and str(v) != "" else None) for k, v in mapping.items() if k in ("date", "description", "amount", "debit", "credit")}
        m.setdefault("type", None)
        m.setdefault("balance", None)
        data_rows, headers = rows[start:], rows[int(mapping.get("headerRow", 0))]
    else:
        hi = _header_index(rows)
        if hi is not None:
            headers = rows[hi]
            m = detect_mapping(headers)
            data_rows = rows[hi + 1:]
        else:
            headers = []
            m = _headerless_mapping(rows)
            data_rows = rows
        if not m:
            first = rows[0]
            raise MappingNeeded(first, rows[1:6], "We couldn't tell which column is which.")
    txns, skipped, flipped = _apply(data_rows[:MAX_ROWS], m, flip)
    if not txns:
        raise MappingNeeded(headers or rows[0], data_rows[:5], "We couldn't find any transactions with those columns.")
    return {"transactions": txns, "skipped": skipped, "flipped": flipped, "mapping": m, "headers": headers}


# ── PDF (AI) ────────────────────────────────────────────────────────────────

async def parse_pdf(raw: bytes) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("Reading PDF statements isn't available right now. Try the CSV download from your bank instead.")
    prompt = """This is a bank or credit card statement. List EVERY transaction in it.
Return ONLY JSON: {"bank": "bank name or null", "account_last4": "1234 or null", "transactions": [{"date": "YYYY-MM-DD", "description": "...", "amount": -12.34}]}
Rules:
- amount is NEGATIVE for money going out (purchases, checks, fees, withdrawals, payments made) and POSITIVE for money coming in (deposits, refunds, credits).
- On a credit card statement, purchases are money out (negative) and payments to the card are money in (positive).
- Use the statement period to give every date a full year.
- Skip balances, totals, subtotals and interest-rate tables. Do not invent transactions."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        res = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 16000,
                "messages": [{"role": "user", "content": [
                    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(raw).decode()}},
                    {"type": "text", "text": prompt},
                ]}],
            },
        )
    if res.status_code != 200:
        print(f"[statement] AI error {res.status_code}: {res.text[:300]}")
        raise ValueError("We couldn't read that PDF. Try again, or upload the CSV file from your bank's website.")
    text = res.json()["content"][0]["text"]
    m = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    txns = []
    for t in data.get("transactions") or []:
        d = parse_date(t.get("date"))
        amt = parse_amount(t.get("amount"))
        if d and amt:
            txns.append({"posted_date": d, "description": str(t.get("description") or "(no description)")[:300], "amount": round(amt, 2)})
    if not txns:
        raise ValueError("We couldn't find any transactions in that PDF.")
    return {"transactions": txns[:MAX_ROWS], "bank": data.get("bank"), "last4": data.get("account_last4"), "skipped": 0, "flipped": False}

"""
Bank transactions core — shared by statement upload (Phase 4) and bank
connection (Phase 5).

Pipeline for every batch of incoming transactions:
  1. normalize + dedupe (unique business_id + dedupe_key)
  2. auto-categorize: the store's vendor rules first, then the AI for the rest
  3. match money-out charges to receipts (same amount ±$0.01, within 5 days,
     vendor name match preferred)

Amounts: negative = money out, positive = money in.
Statuses: needs_receipt | matched | personal | ignored.
"needs_receipt" means "business charge with no receipt yet". Only the ones
the IRS actually wants a receipt for ($75 and over, or any hotel/travel
charge) are flagged to the owner: see receipt_required().
Deposits (money in) are NOT counted as income unless the owner says so
(counts_as_income) — sales are already tracked in Money Box, and counting
both would double the income and the tax estimate.
"""

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

import httpx

from routes.receiptvault.categories import (
    CATEGORY_NAMES,
    TRANSFER,
    line_for,
    normalize_category,
    prompt_category_list,
)

# The IRS doesn't need a receipt for a business expense under $75 (the bank
# record is enough), except lodging, which always needs one. Travel charges
# are kept on the list at any amount so hotel stays aren't missed.
RECEIPT_THRESHOLD = 75.0
ALWAYS_NEEDS_RECEIPT = {"Travel"}

MATCH_AMOUNT_TOLERANCE = 0.01
MATCH_DAYS = 5
AI_BATCH = 60

# Noise banks put in front of / inside merchant names
_PREFIXES = [
    r"pos (debit|purchase|pur|withdrawal)", r"debit card purchase", r"debit purchase", r"purchase authorized on \d{1,2}/\d{1,2}",
    r"purchase", r"checkcard \d{4}", r"check card purchase", r"card purchase( with pin)?", r"recurring (payment|debit)",
    r"ach (debit|withdrawal|pmt)", r"preauthorized debit", r"visa (debit|purchase)", r"dbt crd \d+", r"debit", r"pos",
    r"sq \*", r"sq\*", r"tst\*", r"tst \*", r"paypal \*", r"pp\*", r"sp \*", r"sp\*", r"py \*", r"in \*", r"dd \*",
]
_PREFIX_RE = re.compile(r"^(?:" + "|".join(_PREFIXES) + r")\s*", re.I)
_TRANSFER_RE = re.compile(
    r"(online transfer|transfer (to|from)|xfer|payment thank you|autopay payment|credit card payment|"
    r"card payment|epay|zelle (to|from)|venmo cashout|internal transfer|mobile deposit transfer)", re.I)


def normalize_description(desc: Optional[str]) -> str:
    """Lower-case, strip bank noise, dates, card/store numbers and
    punctuation — so 'POS DEBIT SHELL OIL 57444 10/03' == 'Shell Oil'."""
    s = (desc or "").lower()
    s = re.sub(r"\s+", " ", s).strip()
    for _ in range(3):
        s2 = _PREFIX_RE.sub("", s).strip()
        if s2 == s:
            break
        s = s2
    s = re.sub(r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b", " ", s)       # dates
    s = re.sub(r"#\s*\d+", " ", s)                                # store numbers
    s = re.sub(r"\b(card|crd|x+)\s*\d{3,}\b", " ", s)             # card numbers
    s = re.sub(r"\b\d{4,}\b", " ", s)                              # long numbers / ids
    s = re.sub(r"[^a-z0-9& ]+", " ", s)
    s = re.sub(r"\b(inc|llc|co|corp|com|www|us|usa|ny|nj|ca|tx|fl)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def vendor_key(desc: Optional[str]) -> str:
    """First two meaningful words of the normalized description — what a
    vendor rule is saved under ('shell oil', 'home depot', 'con edison')."""
    words = [w for w in normalize_description(desc).split() if not w.isdigit()]
    return " ".join(words[:2])


def dedupe_key(posted_date: str, amount: float, description: str, account_ref: str, occurrence: int = 0) -> str:
    """business + date + amount + normalized description + source account.
    `occurrence` keeps two genuinely identical charges on the same day (two
    $4.50 coffees) as two rows, while re-uploading the same file still
    lands on the same keys and is skipped."""
    base = f"{posted_date}|{amount:.2f}|{normalize_description(description)}|{account_ref}"
    return base if occurrence == 0 else f"{base}|#{occurrence + 1}"


def is_transfer(description: str) -> bool:
    return bool(_TRANSFER_RE.search(description or ""))


# Obvious vendors, sorted without the AI (and fixing older AI guesses).
# Checked after the store's own rules, so the owner's choices always win.
# Each name must start a word; names in _WHOLE must also end one ("shell" but
# not "shelly", "rent" but not "rental"). Banks glue words together
# ("E-Z*PASSNY", "NGRID0000"), so the others may run on.
_KNOWN_VENDORS = [
    ("Insurance", ["insurance", "ins co", "geico", "state farm", "progressive", "allstate", "liberty mutual",
                   "nationwide", "travelers ins", "hiscox", "next insurance", "the hartford", "erie ins"]),
    ("Vehicle & Fuel", [r"e-?\s?z\W?\s?pass", "toll", "tolls", "thruway", "turnpike", "parking", "parkmobile",
                        "speedway", "fastrac", "citgo", "shell", "exxon", "mobil", "sunoco", "bp", "chevron",
                        "valero", "hess", "getty", "marathon petro", "kwik fill", "costco gas", "gas station",
                        "gasoline", "fuel"]),
    ("Utilities", ["ngrid", "national grid", "con ed", "coned", "consolidated edison", "pseg", "nyseg", "rg&e",
                   "eversource", "duke energy", r"water (dept|authority|bill|board)", "sewer", "verizon", "at&t",
                   r"t-?mobile", "spectrum", "charter comm", "optimum", "comcast", "xfinity", "cricket wireless",
                   "boost mobile", r"metro ?pcs", "frontier comm", "mint mobile", "vonage"]),
    ("Rent", ["rent", "rent payment", r"lease (pmt|payment)", "appfolio", "buildium", r"property (mgmt|management)"]),
]
_WHOLE = {"toll", "tolls", "shell", "mobil", "bp", "hess", "getty", "fuel", "rent", "parking", "optimum", "spectrum"}


def _vendor_re(names: list):
    parts = [rf"(?<![a-z0-9]){n}" + (r"(?![a-z0-9])" if n in _WHOLE else "") for n in names]
    return re.compile("|".join(parts), re.I)


_KNOWN_RES = [(cat, _vendor_re(names)) for cat, names in _KNOWN_VENDORS]


def known_vendor_category(description: Optional[str]) -> Optional[str]:
    text = description or ""
    for cat, rx in _KNOWN_RES:
        if rx.search(text):
            return cat
    return None


def receipt_required(row: dict) -> bool:
    """Does this charge need a receipt the IRS would ask for?"""
    if row.get("status") != "needs_receipt":
        return False
    return abs(float(row.get("amount") or 0)) >= RECEIPT_THRESHOLD or row.get("category") in ALWAYS_NEEDS_RECEIPT


def initial_status(amount: float, is_personal: bool = False, category: Optional[str] = None) -> str:
    if is_personal:
        return "personal"
    if category == TRANSFER or amount >= 0:
        return "ignored"
    return "needs_receipt"


def to_txn(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "businessId": row.get("business_id"),
        "source": row.get("source"),
        "bankAccountId": row.get("bank_account_id"),
        "postedDate": str(row.get("posted_date") or ""),
        "description": row.get("description"),
        "vendor": row.get("vendor") or row.get("description"),
        "amount": float(row.get("amount") or 0),
        "category": row.get("category"),
        "scheduleCLine": row.get("schedule_c_line"),
        "isPersonal": bool(row.get("is_personal")),
        "countsAsIncome": bool(row.get("counts_as_income")),
        "matchedReceiptId": row.get("matched_receipt_id"),
        "status": row.get("status"),
        "receiptRequired": receipt_required(row),
        "categorySource": row.get("category_source"),
        "createdAt": str(row.get("created_at") or ""),
    }


# ── Vendor rules ────────────────────────────────────────────────────────────

def load_rules(supabase, business_id: str) -> list:
    rows = supabase.table("rv_vendor_rules").select("*").eq("business_id", business_id).execute()
    # longest pattern first so "home depot pro" beats "home depot"
    return sorted(rows.data or [], key=lambda r: -len(r.get("vendor_pattern") or ""))


def find_rule(rules: list, description: str) -> Optional[dict]:
    norm = normalize_description(description)
    for r in rules:
        pat = r.get("vendor_pattern") or ""
        if pat and (norm == pat or norm.startswith(pat + " ") or norm.startswith(pat)):
            return r
    return None


def save_rule(supabase, business_id: str, description: str, category: Optional[str] = None, is_personal: Optional[bool] = None):
    """Called when the owner changes a charge's category or marks it
    personal — the same vendor gets the same treatment from now on."""
    pattern = vendor_key(description)
    if not pattern:
        return None
    existing = (
        supabase.table("rv_vendor_rules").select("*")
        .eq("business_id", business_id).eq("vendor_pattern", pattern).execute()
    )
    now = datetime.now(timezone.utc).isoformat()
    if existing.data:
        data = {"updated_at": now}
        if category is not None:
            data["category"] = category
            data["schedule_c_line"] = line_for(category) if category != TRANSFER else None
        if is_personal is not None:
            data["is_personal"] = is_personal
        supabase.table("rv_vendor_rules").update(data).eq("id", existing.data[0]["id"]).execute()
        return pattern
    supabase.table("rv_vendor_rules").insert({
        "business_id": business_id,
        "vendor_pattern": pattern,
        "category": category,
        "schedule_c_line": (line_for(category) if category and category != TRANSFER else None),
        "is_personal": bool(is_personal),
    }).execute()
    return pattern


def apply_rule_to_existing(supabase, business_id: str, pattern: str, category: Optional[str] = None,
                           is_personal: Optional[bool] = None, skip_id: Optional[str] = None) -> int:
    """Re-sort other charges from the same vendor that the owner hasn't
    hand-edited, so one correction fixes the whole list."""
    rows = supabase.table("rv_bank_transactions").select("*").eq("business_id", business_id).execute()
    n = 0
    for r in rows.data or []:
        if r["id"] == skip_id or r.get("category_source") == "user":
            continue
        if vendor_key(r.get("description")) != pattern:
            continue
        data = {"category_source": "rule", "updated_at": datetime.now(timezone.utc).isoformat()}
        if category is not None:
            data["category"] = category
            data["schedule_c_line"] = line_for(category) if category != TRANSFER else None
        if is_personal is not None:
            data["is_personal"] = is_personal
        amount = float(r.get("amount") or 0)
        new_personal = data.get("is_personal", r.get("is_personal"))
        if r.get("status") != "matched" or new_personal:
            data["status"] = initial_status(amount, bool(new_personal), data.get("category", r.get("category")))
        supabase.table("rv_bank_transactions").update(data).eq("id", r["id"]).execute()
        n += 1
    return n


# ── AI categorize ───────────────────────────────────────────────────────────

def learned_examples(rules: list, limit: int = 40) -> str:
    """The owner's own vendor choices, shown to the AI as examples so similar
    vendors are sorted the same way."""
    lines = []
    for r in rules[:limit]:
        if r.get("is_personal"):
            continue
        if r.get("vendor_pattern") and r.get("category"):
            lines.append(f'- "{r["vendor_pattern"]}" -> {r["category"]}')
    return "\n".join(lines)


async def ai_categorize(items: list, examples: str = "") -> dict:
    """items: [{"i": index, "description": str, "amount": float}] ->
    {index: category}. Uses the same AI provider/key as receipt reading.
    Returns {} (everything left as 'Other') if the AI isn't available."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or not items:
        return {}
    out = {}
    for start in range(0, len(items), AI_BATCH):
        chunk = items[start:start + AI_BATCH]
        lines = "\n".join(f'{it["i"]}\t{it["amount"]:.2f}\t{it["description"][:120]}' for it in chunk)
        prompt = f"""You sort bank and credit card charges for a small store owner (bodega, deli, smoke shop, contractor) into tax categories.

CATEGORIES (use the exact name):
{prompt_category_list()}
- {TRANSFER}: money moving between the owner's own accounts, credit card payments, transfers, loan principal. Not an expense.

{("This owner has sorted these vendors themselves. Follow the same choices for the same or similar vendors:" + chr(10) + examples + chr(10)) if examples else ""}
Each line below is: id<TAB>amount (negative = money out)<TAB>bank description.
Return ONLY a JSON object mapping each id to a category name, e.g. {{"0": "Inventory", "1": "Utilities"}}.

{lines}"""
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                res = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": "claude-sonnet-4-6", "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]},
                )
            if res.status_code != 200:
                print(f"[categorize] AI error {res.status_code}: {res.text[:200]}")
                continue
            text = res.json()["content"][0]["text"].strip()
            m = re.search(r"\{.*\}", text, re.S)
            parsed = json.loads(m.group(0)) if m else {}
            for k, v in parsed.items():
                try:
                    idx = int(k)
                except ValueError:
                    continue
                out[idx] = TRANSFER if v == TRANSFER else normalize_category(v)
        except Exception as e:
            print(f"[categorize] AI call failed: {e}")
    return out


# ── Import pipeline ─────────────────────────────────────────────────────────

def _existing_keys(supabase, business_id: str, keys: Iterable[str]) -> set:
    keys = list(keys)
    found = set()
    for i in range(0, len(keys), 200):
        rows = (
            supabase.table("rv_bank_transactions").select("dedupe_key")
            .eq("business_id", business_id).in_("dedupe_key", keys[i:i + 200]).execute()
        )
        found.update(r["dedupe_key"] for r in (rows.data or []))
    return found


def prepare_rows(business_id: str, txns: list, source: str, account_ref: str, bank_account_id: Optional[str] = None,
                 statement_upload_id: Optional[str] = None) -> list:
    """txns: [{"posted_date": "YYYY-MM-DD", "description": str, "amount": float, "external_id"?: str, "raw"?: dict}]"""
    seen: dict = {}
    rows = []
    for t in txns:
        amount = round(float(t["amount"]), 2)
        desc = (t.get("description") or "").strip() or "(no description)"
        if t.get("external_id"):
            key = f"{source}:{t['external_id']}"
        else:
            base = dedupe_key(t["posted_date"], amount, desc, account_ref)
            occ = seen.get(base, 0)
            seen[base] = occ + 1
            key = dedupe_key(t["posted_date"], amount, desc, account_ref, occ)
        rows.append({
            "business_id": business_id,
            "source": source,
            "bank_account_id": bank_account_id,
            "statement_upload_id": statement_upload_id,
            "posted_date": t["posted_date"],
            "description": desc[:500],
            "vendor": (t.get("vendor") or desc)[:200],
            "normalized_description": normalize_description(desc),
            "amount": amount,
            "external_id": t.get("external_id"),
            "dedupe_key": key,
            "raw": t.get("raw") or {},
        })
    return rows


async def import_transactions(supabase, business: dict, txns: list, *, source: str, account_ref: str,
                              bank_account_id: Optional[str] = None, statement_upload_id: Optional[str] = None,
                              auto: bool = True) -> dict:
    """Dedupe, categorize, insert and match. Returns counts."""
    business_id = business["id"]
    rows = prepare_rows(business_id, txns, source, account_ref, bank_account_id, statement_upload_id)
    existing = _existing_keys(supabase, business_id, [r["dedupe_key"] for r in rows])
    new_rows = [r for r in rows if r["dedupe_key"] not in existing]
    duplicates = len(rows) - len(new_rows)

    rules = load_rules(supabase, business_id) if auto else []
    ai_needed = []
    for i, r in enumerate(new_rows):
        rule = find_rule(rules, r["description"]) if auto else None
        known = known_vendor_category(r["description"]) if auto and r["amount"] < 0 else None
        if rule:
            r["category"] = rule.get("category")
            r["is_personal"] = bool(rule.get("is_personal"))
            r["category_source"] = "rule"
        elif known:
            r["category"] = known
            r["category_source"] = "known"
        elif is_transfer(r["description"]):
            r["category"] = TRANSFER
            r["category_source"] = "rule"
        elif r["amount"] < 0 and auto:
            ai_needed.append({"i": i, "description": r["description"], "amount": r["amount"]})
        if r["amount"] > 0 and not r.get("category"):
            r["category"] = "Deposit"
            r["category_source"] = r.get("category_source") or "none"

    if ai_needed:
        guesses = await ai_categorize(ai_needed, learned_examples(rules))
        for it in ai_needed:
            r = new_rows[it["i"]]
            r["category"] = guesses.get(it["i"], "Other")
            r["category_source"] = "ai" if it["i"] in guesses else "none"

    for r in new_rows:
        if r["amount"] < 0 and not r.get("category"):
            r["category"] = "Other"
            r["category_source"] = r.get("category_source") or "none"
        cat = r.get("category")
        r["schedule_c_line"] = line_for(cat) if cat in CATEGORY_NAMES else None
        r["is_personal"] = bool(r.get("is_personal"))
        r["status"] = initial_status(r["amount"], r["is_personal"], cat)

    inserted = []
    for i in range(0, len(new_rows), 200):
        chunk = new_rows[i:i + 200]
        try:
            res = supabase.table("rv_bank_transactions").insert(chunk).execute()
            inserted.extend(res.data or [])
        except Exception:
            # A row raced in (unique index) — fall back to one at a time
            for r in chunk:
                try:
                    res = supabase.table("rv_bank_transactions").insert(r).execute()
                    inserted.extend(res.data or [])
                except Exception:
                    duplicates += 1

    matched = match_transactions(supabase, business_id, inserted) if auto else 0
    return {"imported": len(inserted), "duplicates": duplicates, "matched": matched, "transactions": inserted}


# ── Receipt matching ────────────────────────────────────────────────────────

def _d(v) -> Optional[date]:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _vendor_score(txn_desc: str, merchant: Optional[str]) -> float:
    a = set(normalize_description(txn_desc).split())
    b = set(normalize_description(merchant).split())
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _score(txn: dict, receipt: dict) -> Optional[float]:
    amt = receipt.get("amount")
    if amt is None:
        return None
    if abs(abs(float(txn["amount"])) - float(amt)) > MATCH_AMOUNT_TOLERANCE + 1e-9:
        return None
    td, rd = _d(txn.get("posted_date")), _d(receipt.get("receipt_date") or receipt.get("uploaded_at"))
    if not td or not rd:
        return None
    days = abs((td - rd).days)
    if days > MATCH_DAYS:
        return None
    # vendor match matters most, then closeness in time
    return _vendor_score(txn.get("description", ""), receipt.get("merchant")) * 10 + (MATCH_DAYS - days)


def _matched_receipt_ids(supabase, business_id: str) -> set:
    rows = (
        supabase.table("rv_bank_transactions").select("matched_receipt_id")
        .eq("business_id", business_id).not_.is_("matched_receipt_id", "null").execute()
    )
    return {r["matched_receipt_id"] for r in (rows.data or []) if r.get("matched_receipt_id")}


def _link(supabase, txn_id: str, receipt_id: str):
    supabase.table("rv_bank_transactions").update({
        "matched_receipt_id": receipt_id, "status": "matched",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", txn_id).execute()


def match_transactions(supabase, business_id: str, txns: list) -> int:
    """Run when transactions come in: each charge needing a receipt gets
    the best unclaimed receipt (if any)."""
    candidates = [t for t in txns if t.get("status") == "needs_receipt" and not t.get("match_locked")]
    if not candidates:
        return 0
    dates = [_d(t["posted_date"]) for t in candidates]
    lo = (min(dates) - timedelta(days=MATCH_DAYS)).isoformat()
    hi = (max(dates) + timedelta(days=MATCH_DAYS)).isoformat()
    receipts = (
        supabase.table("receipts").select("*").eq("business_id", business_id)
        .gte("receipt_date", lo).lte("receipt_date", hi).execute()
    ).data or []
    taken = _matched_receipt_ids(supabase, business_id)
    n = 0
    for t in sorted(candidates, key=lambda x: str(x["posted_date"])):
        best, best_score = None, None
        for r in receipts:
            if r["id"] in taken:
                continue
            sc = _score(t, r)
            if sc is not None and (best_score is None or sc > best_score):
                best, best_score = r, sc
        if best:
            _link(supabase, t["id"], best["id"])
            taken.add(best["id"])
            n += 1
    return n


def match_receipt(supabase, business_id: str, receipt: dict) -> Optional[str]:
    """Run when a new receipt is scanned: link it to the best charge that
    still needs a receipt. Returns the transaction id, if any."""
    if receipt.get("amount") is None:
        return None
    rd = _d(receipt.get("receipt_date") or receipt.get("uploaded_at"))
    if not rd:
        return None
    if receipt["id"] in _matched_receipt_ids(supabase, business_id):
        return None
    rows = (
        supabase.table("rv_bank_transactions").select("*").eq("business_id", business_id)
        .eq("status", "needs_receipt")
        .gte("posted_date", (rd - timedelta(days=MATCH_DAYS)).isoformat())
        .lte("posted_date", (rd + timedelta(days=MATCH_DAYS)).isoformat())
        .execute()
    ).data or []
    best, best_score = None, None
    for t in rows:
        if t.get("match_locked"):
            continue
        sc = _score(t, receipt)
        if sc is not None and (best_score is None or sc > best_score):
            best, best_score = t, sc
    if best:
        _link(supabase, best["id"], receipt["id"])
        return best["id"]
    return None


def receipt_candidates(supabase, business_id: str, txn: dict, limit: int = 10) -> list:
    """Receipts the owner could link to this charge by hand: unclaimed,
    closest amount and date first (wider window than auto-matching)."""
    td = _d(txn.get("posted_date")) or date.today()
    receipts = (
        supabase.table("receipts").select("*").eq("business_id", business_id)
        .gte("receipt_date", (td - timedelta(days=30)).isoformat())
        .lte("receipt_date", (td + timedelta(days=30)).isoformat()).execute()
    ).data or []
    taken = _matched_receipt_ids(supabase, business_id)
    amt = abs(float(txn.get("amount") or 0))

    def rank(r):
        ra = float(r.get("amount") or 0)
        rd = _d(r.get("receipt_date")) or td
        return (abs(ra - amt) > MATCH_AMOUNT_TOLERANCE, abs(ra - amt), abs((rd - td).days))

    return sorted([r for r in receipts if r["id"] not in taken], key=rank)[:limit]


def apply_known_vendors(supabase, rows: list) -> int:
    """Fix older automatic guesses for obvious vendors (E-ZPass, gas,
    utilities, phone, insurance, rent). Never touches anything the owner
    sorted or a vendor rule set. Updates `rows` in place."""
    n = 0
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        if float(r.get("amount") or 0) >= 0 or r.get("category_source") not in (None, "ai", "none"):
            continue
        if r.get("category") == TRANSFER or r.get("status") in ("personal", "ignored"):
            continue
        cat = known_vendor_category(r.get("description"))
        if not cat:
            continue
        data = {"category": cat, "schedule_c_line": line_for(cat), "category_source": "known", "updated_at": now}
        if r.get("category") == cat:
            data = {"category_source": "known", "updated_at": now}
        supabase.table("rv_bank_transactions").update(data).eq("id", r["id"]).execute()
        r.update(data)
        n += 1
    return n


# ── Reporting helpers (P&L, tax, accountant PDF, CSV) ───────────────────────

def business_transactions(supabase, business_id: str, start: str, end: str) -> list:
    """Charges and counted deposits that belong in the books for a period:
    personal and ignored rows are left out."""
    rows = (
        supabase.table("rv_bank_transactions").select("*").eq("business_id", business_id)
        .gte("posted_date", start).lte("posted_date", end).execute()
    ).data or []
    out = []
    for r in rows:
        amount = float(r.get("amount") or 0)
        if r.get("is_personal") or r.get("status") == "personal":
            continue
        if amount > 0:
            if r.get("counts_as_income"):
                out.append(r)
            continue
        if r.get("status") == "ignored":  # transfers, "already counted"
            continue
        out.append(r)
    return out


def matched_receipt_ids_in(supabase, business_id: str) -> set:
    return _matched_receipt_ids(supabase, business_id)

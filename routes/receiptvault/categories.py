"""
The ONE ReceiptKept expense category list, each mapped to its IRS
Schedule C line. Receipts, bank transactions, vendor rules, AI prompts,
exports and the front end (via GET /api/categories) all use this list.

Names are plain English on purpose (no accounting jargon); the Schedule C
line is shown next to them for the accountant.
"""

from typing import Optional

# (name, schedule C line code, line label, hint for people + the AI)
CATEGORIES = [
    ("Inventory", "Line 4", "Line 4 — Cost of goods sold", "Things you buy to resell: drinks, snacks, groceries, tobacco, distributor invoices"),
    ("Advertising", "Line 8", "Line 8 — Advertising", "Signs, flyers, ads, promotions"),
    ("Vehicle & Fuel", "Line 9", "Line 9 — Car and truck expenses", "Gas, tolls, parking, car repairs for the business"),
    ("Card & Bank Fees", "Line 10", "Line 10 — Commissions and fees", "Card machine fees, bank fees, Square/Clover/Stripe fees"),
    ("Contract Labor", "Line 11", "Line 11 — Contract labor", "Paying people who aren't on payroll"),
    ("Equipment", "Line 13", "Line 13 — Depreciation", "Fridges, registers, computers, big tools"),
    ("Insurance", "Line 15", "Line 15 — Insurance", "Business insurance (not health insurance)"),
    ("Loan Interest", "Line 16b", "Line 16b — Interest", "Interest on business loans or business credit cards"),
    ("Accountant & Legal", "Line 17", "Line 17 — Legal and professional services", "Accountant, lawyer, bookkeeper"),
    ("Office Expenses", "Line 18", "Line 18 — Office expense", "Postage, printing, small office items"),
    ("Rent", "Line 20b", "Line 20b — Rent (property)", "Rent for the store or storage"),
    ("Repairs & Maintenance", "Line 21", "Line 21 — Repairs and maintenance", "Fixing the store, equipment repairs, cleaning service"),
    ("Supplies", "Line 22", "Line 22 — Supplies", "Bags, cups, cleaning supplies, store supplies you use up"),
    ("Taxes & Licenses", "Line 23", "Line 23 — Taxes and licenses", "Business licenses, permits, sales-tax filing fees"),
    ("Travel", "Line 24a", "Line 24a — Travel", "Hotels, flights, train for business trips"),
    ("Meals", "Line 24b", "Line 24b — Meals", "Business meals"),
    ("Utilities", "Line 25", "Line 25 — Utilities", "Electric, gas, water, phone, internet"),
    ("Wages", "Line 26", "Line 26 — Wages", "Payroll for employees"),
    ("Software & Subscriptions", "Line 27a", "Line 27a — Other expenses", "Apps, POS software, subscriptions"),
    ("Other", "Line 27a", "Line 27a — Other expenses", "Anything else for the business"),
]

CATEGORY_NAMES = [c[0] for c in CATEGORIES]
CATEGORY_TO_LINE = {c[0]: c[1] for c in CATEGORIES}
LINE_LABELS = {c[1]: c[2] for c in CATEGORIES}
SCHEDULE_C_LINES = list(dict.fromkeys(c[1] for c in CATEGORIES))

# Bank-only pseudo category: money moving between your own accounts (card
# payments, transfers). Never an expense, so it never reaches the P&L.
TRANSFER = "Transfer"

# Old names (backend's previous list, the legacy server/extract.ts list and
# the old Schedule C words) -> the unified list.
LEGACY_CATEGORY_MAP = {
    "Office Supplies": "Supplies",
    "Meals & Entertainment": "Meals",
    "Inventory/Supplies": "Inventory",
    "Fuel/Vehicle": "Vehicle & Fuel",
    "Office": "Office Expenses",
    "Repairs/Maintenance": "Repairs & Maintenance",
    "Professional Services": "Accountant & Legal",
}


def normalize_category(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    name = str(name).strip()
    if name in CATEGORY_TO_LINE:
        return name
    if name in LEGACY_CATEGORY_MAP:
        return LEGACY_CATEGORY_MAP[name]
    lower = {n.lower(): n for n in CATEGORY_NAMES}
    return lower.get(name.lower(), "Other")


def line_for(category: Optional[str]) -> Optional[str]:
    cat = normalize_category(category)
    return CATEGORY_TO_LINE.get(cat) if cat else None


def public_categories() -> list:
    return [{"name": n, "scheduleCLine": line, "lineLabel": label, "hint": hint} for n, line, label, hint in CATEGORIES]


def prompt_category_list() -> str:
    return "\n".join(f"- {n}: {hint}" for n, _l, _lab, hint in CATEGORIES)

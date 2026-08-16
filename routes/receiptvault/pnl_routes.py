"""
ReceiptVault Plain-Language P&L ("Where Your Money Stands") + Quarterly Tax Estimate.
Combines the Money Box ledger (rv_money_entries) and receipts into one
income/expense picture for a date range, then estimates quarterly
self-employment tax off of it.
"""

import os
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from supabase import create_client

from routes.receiptvault.routes import get_current_user, resolve_module_access, SCHEDULE_C_LINES
from routes.receiptvault.money_routes import CATEGORY_LABELS

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://wzcuzyouymauokijaqjk.supabase.co")
SUPABASE_KEY = (os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY") or "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Ind6Y3V6eW91eW1hdW9raWphcWprIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM5NDUyMDAsImV4cCI6MjA4OTUyMTIwMH0.fDuyCZGrCbL9Obd7l6FDnNd5AB-AUytp-3S60KwwKvM")

pnl_routes = APIRouter(prefix="/api", tags=["pnl"])


def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _receipt_group_label(row: dict, group_by: str) -> str:
    if group_by == "scheduleCLine":
        return row.get("schedule_c_line") or "Uncategorized"
    return row.get("category") or "Uncategorized"


def _entry_group_label(row: dict, group_by: str) -> str:
    if group_by == "scheduleCLine":
        # Ledger entries aren't mapped to Schedule C lines — they bucket
        # separately from receipt-driven expenses so nothing silently
        # disappears from the total.
        return "Income" if row.get("entry_type") == "income" else "Uncategorized (Ledger)"
    return CATEGORY_LABELS.get(row.get("category"), (row.get("category") or "Uncategorized").replace("_", " ").title())


@pnl_routes.get("/pnl/totals")
async def get_pnl_totals(
    start: Optional[str] = None,
    end: Optional[str] = None,
    groupBy: str = "category",
    current_user=Depends(get_current_user),
):
    """Plain-language P&L: total in / out / net for a date range, pulled
    from the Money Box ledger (income + expense entries) and receipts
    (always expenses), grouped by category or Schedule C line."""
    if groupBy not in ("category", "scheduleCLine"):
        raise HTTPException(status_code=400, detail="groupBy must be 'category' or 'scheduleCLine'")

    supabase = get_supabase()
    business = resolve_module_access(supabase, current_user.user.id, "money_box")
    business_id = business["id"]

    entries_query = supabase.table("rv_money_entries").select("*").eq("business_id", business_id)
    receipts_query = supabase.table("receipts").select("*").eq("business_id", business_id)
    if start:
        entries_query = entries_query.gte("entry_date", start)
        receipts_query = receipts_query.gte("receipt_date", start)
    if end:
        entries_query = entries_query.lte("entry_date", end)
        receipts_query = receipts_query.lte("receipt_date", end)

    entries = entries_query.execute().data or []
    receipts = receipts_query.execute().data or []

    total_in = 0.0
    total_out = 0.0
    breakdown: dict = {}  # group label -> {"in": x, "out": y}

    def bump(label: str, key: str, amount: float):
        bucket = breakdown.setdefault(label, {"in": 0.0, "out": 0.0})
        bucket[key] += amount

    for e in entries:
        amount = float(e.get("amount") or 0)
        label = _entry_group_label(e, groupBy)
        if e.get("entry_type") == "income":
            total_in += amount
            bump(label, "in", amount)
        else:
            total_out += amount
            bump(label, "out", amount)

    for r in receipts:
        amount = float(r.get("amount") or 0)
        if not amount:
            continue
        label = _receipt_group_label(r, groupBy)
        total_out += amount
        bump(label, "out", amount)

    net = total_in - total_out

    return {
        "periodStart": start,
        "periodEnd": end,
        "groupBy": groupBy,
        "totalIn": round(total_in, 2),
        "totalOut": round(total_out, 2),
        "net": round(net, 2),
        "breakdown": [
            {"group": label, "in": round(v["in"], 2), "out": round(v["out"], 2)}
            for label, v in sorted(breakdown.items(), key=lambda kv: -(kv[1]["in"] + kv[1]["out"]))
        ],
    }


# ── Quarterly Tax Estimate ──────────────────────────────────────────────────

SELF_EMPLOYMENT_TAX_RATE = 0.153       # Social Security (12.4%) + Medicare (2.9%)
SE_TAXABLE_PORTION = 0.9235            # only 92.35% of net SE earnings is taxed
STANDARD_DEDUCTION_SINGLE = 15000.00   # approximate, single filer

# Approximate single-filer marginal brackets, applied to (net income - half
# the SE tax deduction - standard deduction). This is an ESTIMATE for
# budgeting purposes only, not tax advice, and ignores filing status,
# other income, and credits.
INCOME_TAX_BRACKETS_SINGLE = [
    (0, 11600, 0.10),
    (11600, 47150, 0.12),
    (47150, 100525, 0.22),
    (100525, 191950, 0.24),
    (191950, 243725, 0.32),
    (243725, 609350, 0.35),
    (609350, float("inf"), 0.37),
]


def _bracket_tax(taxable_income: float) -> float:
    if taxable_income <= 0:
        return 0.0
    tax = 0.0
    for lower, upper, rate in INCOME_TAX_BRACKETS_SINGLE:
        if taxable_income <= lower:
            break
        taxed_amount = min(taxable_income, upper) - lower
        tax += taxed_amount * rate
    return tax


@pnl_routes.get("/pnl/tax-estimate")
async def get_quarterly_tax_estimate(
    start: str,
    end: str,
    current_user=Depends(get_current_user),
):
    """Quarterly estimated tax: annualizes net income for the given period,
    applies the self-employment tax rate plus a bracket-based income tax
    estimate, then divides the annual figures into a quarterly payment."""
    supabase = get_supabase()
    business = resolve_module_access(supabase, current_user.user.id, "money_box")
    business_id = business["id"]

    try:
        start_d = datetime.strptime(start, "%Y-%m-%d").date()
        end_d = datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="start and end must be YYYY-MM-DD")
    days_in_period = max((end_d - start_d).days + 1, 1)

    entries = supabase.table("rv_money_entries").select("entry_type, amount")\
        .eq("business_id", business_id).gte("entry_date", start).lte("entry_date", end).execute().data or []
    receipts = supabase.table("receipts").select("amount")\
        .eq("business_id", business_id).gte("receipt_date", start).lte("receipt_date", end).execute().data or []

    total_in = sum(float(e.get("amount") or 0) for e in entries if e.get("entry_type") == "income")
    total_out = sum(float(e.get("amount") or 0) for e in entries if e.get("entry_type") == "expense")
    total_out += sum(float(r.get("amount") or 0) for r in receipts)
    period_net_income = total_in - total_out

    annualized_net_income = period_net_income * (365.0 / days_in_period)

    if annualized_net_income > 0:
        se_taxable_earnings = annualized_net_income * SE_TAXABLE_PORTION
        annual_se_tax = se_taxable_earnings * SELF_EMPLOYMENT_TAX_RATE
        taxable_income = max(0.0, annualized_net_income - (annual_se_tax / 2) - STANDARD_DEDUCTION_SINGLE)
        annual_income_tax = _bracket_tax(taxable_income)
    else:
        annual_se_tax = 0.0
        annual_income_tax = 0.0

    annual_total = annual_se_tax + annual_income_tax

    return {
        "periodStart": start,
        "periodEnd": end,
        "periodNetIncome": round(period_net_income, 2),
        "annualizedNetIncome": round(annualized_net_income, 2),
        "selfEmploymentTaxRate": SELF_EMPLOYMENT_TAX_RATE,
        "annual": {
            "selfEmploymentTax": round(annual_se_tax, 2),
            "incomeTax": round(annual_income_tax, 2),
            "total": round(annual_total, 2),
        },
        "quarterly": {
            "selfEmploymentTax": round(annual_se_tax / 4, 2),
            "incomeTax": round(annual_income_tax / 4, 2),
            "total": round(annual_total / 4, 2),
        },
        "disclaimer": (
            "This is an estimate for budgeting purposes only, based on the income and expenses "
            "recorded in ReceiptVault. It assumes a single filer, standard deduction, and no other "
            "income, and is not tax advice. Confirm with your accountant before making a payment."
        ),
    }

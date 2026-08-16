"""
ReceiptVault Plain-Language P&L ("Where Your Money Stands") + Quarterly Tax
Estimate. Combines the Money Box ledger (rv_money_entries) and receipts
into one income/expense picture, then estimates quarterly self-employment
tax off of it.

Endpoint shapes are locked to the already-pushed frontend contract
(claude/new-session-1yi0xs, Appendix A of the build spec):
  GET /api/pnl                     -> { totalIn, totalOut, net, periodLabel? }
  GET /api/tax/quarterly-estimate  -> { estimate, quarter?, dueDate? }
"""

import os
from datetime import date, datetime, timedelta
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


def _default_period() -> tuple:
    """No date range given -> current calendar month to date."""
    today = date.today()
    return today.replace(day=1), today


def _format_period_label(start_d: date, end_d: date) -> str:
    if start_d.replace(day=1) == start_d and end_d == date.today() and start_d.month == end_d.month and start_d.year == end_d.year:
        return start_d.strftime("%B %Y")
    return f"{start_d.strftime('%b %-d, %Y')} – {end_d.strftime('%b %-d, %Y')}"


def _totals_for_period(supabase, business_id: str, start: str, end: str, group_by: str):
    entries_query = supabase.table("rv_money_entries").select("*").eq("business_id", business_id)
    receipts_query = supabase.table("receipts").select("*").eq("business_id", business_id)
    entries_query = entries_query.gte("entry_date", start).lte("entry_date", end)
    receipts_query = receipts_query.gte("receipt_date", start).lte("receipt_date", end)

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
        label = _entry_group_label(e, group_by)
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
        label = _receipt_group_label(r, group_by)
        total_out += amount
        bump(label, "out", amount)

    return total_in, total_out, breakdown


@pnl_routes.get("/pnl")
async def get_pnl(
    start: Optional[str] = None,
    end: Optional[str] = None,
    groupBy: str = "category",
    current_user=Depends(get_current_user),
):
    """Plain-language P&L: total in / out / net, pulled from the Money Box
    ledger (income + expense entries) and receipts (always expenses).
    Defaults to the current calendar month when no range is given.
    groupBy ("category" | "scheduleCLine") adds an optional breakdown on
    top of the locked { totalIn, totalOut, net, periodLabel } contract."""
    if groupBy not in ("category", "scheduleCLine"):
        raise HTTPException(status_code=400, detail="groupBy must be 'category' or 'scheduleCLine'")

    supabase = get_supabase()
    business = resolve_module_access(supabase, current_user.user.id, "money_box")

    if start and end:
        start_d = datetime.strptime(start, "%Y-%m-%d").date()
        end_d = datetime.strptime(end, "%Y-%m-%d").date()
    else:
        start_d, end_d = _default_period()
        start, end = start_d.isoformat(), end_d.isoformat()

    total_in, total_out, breakdown = _totals_for_period(supabase, business["id"], start, end, groupBy)
    net = total_in - total_out

    return {
        "totalIn": round(total_in, 2),
        "totalOut": round(total_out, 2),
        "net": round(net, 2),
        "periodLabel": _format_period_label(start_d, end_d),
        "periodStart": start,
        "periodEnd": end,
        "groupBy": groupBy,
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


def _current_estimated_tax_period(today: date):
    """IRS Form 1040-ES estimated-tax payment periods for the calendar
    year containing `today`. Periods aren't even calendar quarters (period
    2 is 2 months, period 4 is 4 months) — this mirrors the real due dates."""
    year = today.year
    if today.month <= 3:
        return date(year, 1, 1), date(year, 3, 31), f"Q1 {year}", date(year, 4, 15)
    if today.month <= 5:
        return date(year, 4, 1), date(year, 5, 31), f"Q2 {year}", date(year, 6, 15)
    if today.month <= 8:
        return date(year, 6, 1), date(year, 8, 31), f"Q3 {year}", date(year, 9, 15)
    return date(year, 9, 1), date(year, 12, 31), f"Q4 {year}", date(year + 1, 1, 15)


@pnl_routes.get("/tax/quarterly-estimate")
async def get_quarterly_tax_estimate(
    start: Optional[str] = None,
    end: Optional[str] = None,
    current_user=Depends(get_current_user),
):
    """Quarterly estimated tax: annualizes net income earned so far in the
    current IRS estimated-tax period (or an explicit start/end override),
    applies the self-employment tax rate plus a bracket-based income tax
    estimate, then divides the annual figures into a quarterly payment."""
    supabase = get_supabase()
    business = resolve_module_access(supabase, current_user.user.id, "money_box")

    today = date.today()
    period_start, period_end, quarter_label, due_date = _current_estimated_tax_period(today)

    if start and end:
        try:
            start_d = datetime.strptime(start, "%Y-%m-%d").date()
            end_d = datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="start and end must be YYYY-MM-DD")
    else:
        start_d, end_d = period_start, min(today, period_end)

    days_elapsed = max((end_d - start_d).days + 1, 1)

    total_in, total_out, _ = _totals_for_period(supabase, business["id"], start_d.isoformat(), end_d.isoformat(), "category")
    period_net_income = total_in - total_out
    annualized_net_income = period_net_income * (365.0 / days_elapsed)

    if annualized_net_income > 0:
        se_taxable_earnings = annualized_net_income * SE_TAXABLE_PORTION
        annual_se_tax = se_taxable_earnings * SELF_EMPLOYMENT_TAX_RATE
        taxable_income = max(0.0, annualized_net_income - (annual_se_tax / 2) - STANDARD_DEDUCTION_SINGLE)
        annual_income_tax = _bracket_tax(taxable_income)
    else:
        annual_se_tax = 0.0
        annual_income_tax = 0.0

    annual_total = annual_se_tax + annual_income_tax
    quarterly_total = annual_total / 4

    return {
        "estimate": round(quarterly_total, 2),
        "quarter": quarter_label,
        "dueDate": due_date.isoformat(),
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
            "total": round(quarterly_total, 2),
        },
        "disclaimer": (
            "This is an estimate for budgeting purposes only, based on the income and expenses "
            "recorded in ReceiptVault. It assumes a single filer, standard deduction, and no other "
            "income, and is not tax advice. Confirm with your accountant before making a payment."
        ),
    }

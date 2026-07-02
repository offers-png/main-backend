import io
import os
from datetime import date, datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import create_client
from routes.receiptvault.routes import get_current_user

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, HRFlowable
)

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://wzcuzyouymauokijaqjk.supabase.co")
SUPABASE_KEY = (os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY") or "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Ind6Y3V6eW91eW1hdW9raWphcWprIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM5NDUyMDAsImV4cCI6MjA4OTUyMTIwMH0.fDuyCZGrCbL9Obd7l6FDnNd5AB-AUytp-3S60KwwKvM")

money_routes = APIRouter(prefix="/api", tags=["money-box"])

CATEGORY_LABELS = {
    "sales_cash": "Sales - Cash",
    "sales_credit": "Sales - Credit/Debit",
    "sales_other": "Sales - Other",
    "vendor_payment": "Vendor Payment",
    "other_expense": "Other Business Expense",
    "other_income": "Other Income",
}


def _label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category.replace("_", " ").title())


def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def get_owner_business(supabase, user_id: str):
    result = supabase.table("businesses").select("*").eq("user_id", user_id).execute()
    return result.data[0] if result.data else None


def get_member_business(supabase, user_id: str):
    member = supabase.table("business_users").select("business_id, role").eq("user_id", user_id).eq("status", "active").execute()
    if member.data:
        biz = supabase.table("businesses").select("*").eq("id", member.data[0]["business_id"]).execute()
        if biz.data:
            return biz.data[0]
    return None


def resolve_access(supabase, user_id: str):
    """Returns (business, is_owner). Raises 404 if no business."""
    biz = get_owner_business(supabase, user_id)
    if biz:
        return biz, True
    biz = get_member_business(supabase, user_id)
    if biz:
        return biz, False
    raise HTTPException(status_code=404, detail="Business not found")


def to_entry(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "businessId": row.get("business_id"),
        "userId": row.get("user_id"),
        "entryDate": str(row.get("entry_date", "")),
        "entryType": row.get("entry_type"),
        "category": row.get("category"),
        "amount": float(row.get("amount") or 0),
        "paymentMethod": row.get("payment_method"),
        "note": row.get("note"),
        "createdAt": str(row.get("created_at", "")),
    }


class CreateEntryBody(BaseModel):
    entryDate: str
    entryType: str  # "income" | "expense"
    category: str
    amount: float
    paymentMethod: Optional[str] = None
    note: Optional[str] = None


class PacketRequestBody(BaseModel):
    periodStart: str
    periodEnd: str


@money_routes.get("/money-entries")
async def list_money_entries(start: Optional[str] = None, end: Optional[str] = None, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, _ = resolve_access(supabase, current_user.user.id)
    query = supabase.table("rv_money_entries").select("*").eq("business_id", business["id"])
    if start:
        query = query.gte("entry_date", start)
    if end:
        query = query.lte("entry_date", end)
    rows = query.order("entry_date", desc=True).order("created_at", desc=True).execute()
    return [to_entry(r) for r in (rows.data or [])]


@money_routes.post("/money-entries")
async def create_money_entry(body: CreateEntryBody, current_user=Depends(get_current_user)):
    if body.entryType not in ("income", "expense"):
        raise HTTPException(status_code=400, detail="entryType must be 'income' or 'expense'")
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero")

    supabase = get_supabase()
    business, _ = resolve_access(supabase, current_user.user.id)
    data = {
        "business_id": business["id"],
        "user_id": current_user.user.id,
        "entry_date": body.entryDate,
        "entry_type": body.entryType,
        "category": body.category,
        "amount": body.amount,
        "payment_method": body.paymentMethod,
        "note": body.note,
    }
    result = supabase.table("rv_money_entries").insert(data).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to save entry")
    return to_entry(result.data[0])


@money_routes.delete("/money-entries/{entry_id}")
async def delete_money_entry(entry_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, _ = resolve_access(supabase, current_user.user.id)
    result = supabase.table("rv_money_entries").delete()\
        .eq("id", entry_id).eq("business_id", business["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"ok": True}


def _build_pdf(entries: List[dict], period_start: str, period_end: str,
               business_name: str, owner_name: str, business_address: str, tax_id: Optional[str]):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.75 * inch, bottomMargin=0.75 * inch,
                             leftMargin=0.75 * inch, rightMargin=0.75 * inch)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleCustom", parent=styles["Title"], fontSize=18, spaceAfter=6)
    heading_style = ParagraphStyle("HeadingCustom", parent=styles["Heading2"], spaceBefore=14, spaceAfter=6)
    normal = styles["Normal"]
    small = ParagraphStyle("Small", parent=styles["Normal"], fontSize=8, textColor=colors.grey)

    period_start_d = datetime.strptime(period_start, "%Y-%m-%d").date()
    period_end_d = datetime.strptime(period_end, "%Y-%m-%d").date()

    story = []
    story.append(Paragraph("Self-Employment Income &amp; Expense Statement", title_style))
    story.append(Paragraph(f"Prepared for: {owner_name}", normal))
    story.append(Paragraph(f"Business: {business_name}", normal))
    if business_address:
        story.append(Paragraph(f"Address: {business_address}", normal))
    if tax_id:
        story.append(Paragraph(f"Tax ID: {tax_id}", normal))
    story.append(Paragraph(
        f"Reporting period: {period_start_d.strftime('%B %d, %Y')} to {period_end_d.strftime('%B %d, %Y')}", normal))
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "This packet contains a signed profit and loss statement and itemized transaction log "
        "prepared to support a self-employment income verification request "
        "(e.g. SNAP, Medicaid, or other public assistance application).", normal))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Contents:", normal))
    for item in ["1. Profit & Loss Statement (signed)", "2. Itemized Income Log", "3. Itemized Expense Log"]:
        story.append(Paragraph(item, normal))
    story.append(Spacer(1, 14))
    days_covered = (period_end_d - period_start_d).days + 1
    story.append(Paragraph(f"Days covered: {days_covered} &middot; Transactions recorded: {len(entries)}", small))
    story.append(Paragraph(f"Prepared on {date.today().strftime('%B %d, %Y')}", small))

    story.append(PageBreak())

    income_entries = [e for e in entries if e["entryType"] == "income"]
    expense_entries = [e for e in entries if e["entryType"] == "expense"]

    income_by_cat: dict = {}
    for e in income_entries:
        income_by_cat[e["category"]] = income_by_cat.get(e["category"], 0) + e["amount"]
    expense_by_cat: dict = {}
    for e in expense_entries:
        expense_by_cat[e["category"]] = expense_by_cat.get(e["category"], 0) + e["amount"]

    total_income = sum(income_by_cat.values())
    total_expenses = sum(expense_by_cat.values())
    net_income = total_income - total_expenses

    story.append(Paragraph("Profit &amp; Loss Statement", heading_style))
    story.append(Paragraph(
        f"For the period {period_start_d.strftime('%B %d, %Y')} to {period_end_d.strftime('%B %d, %Y')}", normal))
    story.append(Spacer(1, 8))

    pl_data = [["Income Source", "Amount"]]
    for cat, amt in sorted(income_by_cat.items(), key=lambda x: -x[1]):
        pl_data.append([_label(cat), f"${amt:,.2f}"])
    pl_data.append(["Total Income", f"${total_income:,.2f}"])
    pl_table = Table(pl_data, colWidths=[3.5 * inch, 2 * inch])
    pl_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(pl_table)
    story.append(Spacer(1, 14))

    ex_data = [["Expense Category", "Amount"]]
    for cat, amt in sorted(expense_by_cat.items(), key=lambda x: -x[1]):
        ex_data.append([_label(cat), f"${amt:,.2f}"])
    ex_data.append(["Total Expenses", f"${total_expenses:,.2f}"])
    ex_table = Table(ex_data, colWidths=[3.5 * inch, 2 * inch])
    ex_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#7f2d2d")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(ex_table)
    story.append(Spacer(1, 14))

    net_table = Table([["Net Self-Employment Income (Income - Expenses)", f"${net_income:,.2f}"]],
                       colWidths=[3.5 * inch, 2 * inch])
    net_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eaf3ea")),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(net_table)
    story.append(Spacer(1, 24))
    story.append(Paragraph(
        "I certify that the income and expense information above accurately reflects the operations of my "
        "self-employment business for the stated period, to the best of my knowledge.", normal))
    story.append(Spacer(1, 30))
    story.append(Paragraph("Signature: _______________________________", normal))
    story.append(Paragraph(f"Print name: {owner_name}", normal))
    story.append(Paragraph("Date: _______________", normal))

    for title, rows, header_label, header_color in [
        ("Itemized Income Log", income_entries, "Detail", colors.HexColor("#2c3e50")),
        ("Itemized Expense Log", expense_entries, "Paid To", colors.HexColor("#7f2d2d")),
    ]:
        story.append(PageBreak())
        story.append(Paragraph(title, heading_style))
        if rows:
            log_data = [["Date", "Category", "Method", "Amount", header_label]]
            for e in sorted(rows, key=lambda x: x["entryDate"]):
                d = datetime.strptime(e["entryDate"], "%Y-%m-%d").strftime("%m/%d/%Y")
                log_data.append([d, _label(e["category"]), (e["paymentMethod"] or "-").title(),
                                  f"${e['amount']:,.2f}", (e["note"] or "")[:40]])
            log_table = Table(log_data, colWidths=[0.9 * inch, 1.4 * inch, 0.9 * inch, 0.9 * inch, 1.9 * inch])
            log_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), header_color),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
                ("ALIGN", (3, 0), (3, -1), "RIGHT"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
            ]))
            story.append(log_table)
        else:
            story.append(Paragraph(f"No {title.lower()} recorded for this period.", normal))

    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Generated by ReceiptVault Money Box. Underlying records (receipts, bank deposits) available upon request.", small))

    doc.build(story)
    return buf.getvalue(), total_income, total_expenses, net_income


@money_routes.post("/money-packets/generate")
async def generate_money_packet(body: PacketRequestBody, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, _ = resolve_access(supabase, current_user.user.id)

    rows = supabase.table("rv_money_entries").select("*")\
        .eq("business_id", business["id"])\
        .gte("entry_date", body.periodStart).lte("entry_date", body.periodEnd)\
        .execute()
    entries = [to_entry(r) for r in (rows.data or [])]
    if not entries:
        raise HTTPException(status_code=400, detail="No entries found in that date range.")

    pdf_bytes, total_income, total_expenses, net_income = _build_pdf(
        entries, body.periodStart, body.periodEnd,
        business.get("business_name") or "My Business",
        business.get("owner_name") or "",
        business.get("business_address") or "",
        business.get("tax_id"),
    )

    filename = f"money-packets/{business['id']}/{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
    supabase.storage.from_("receipts").upload(
        filename, pdf_bytes, {"content-type": "application/pdf"}
    )
    file_url = f"{SUPABASE_URL}/storage/v1/object/public/receipts/{filename}"

    packet_row = supabase.table("rv_money_packets").insert({
        "business_id": business["id"],
        "period_start": body.periodStart,
        "period_end": body.periodEnd,
        "total_income": total_income,
        "total_expenses": total_expenses,
        "net_income": net_income,
        "file_path": file_url,
    }).execute()

    return {
        "id": packet_row.data[0]["id"] if packet_row.data else None,
        "periodStart": body.periodStart,
        "periodEnd": body.periodEnd,
        "totalIncome": total_income,
        "totalExpenses": total_expenses,
        "netIncome": net_income,
        "fileUrl": file_url,
    }


@money_routes.get("/money-packets")
async def list_money_packets(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, _ = resolve_access(supabase, current_user.user.id)
    rows = supabase.table("rv_money_packets").select("*")\
        .eq("business_id", business["id"]).order("created_at", desc=True).execute()
    return [{
        "id": r.get("id"),
        "periodStart": str(r.get("period_start", "")),
        "periodEnd": str(r.get("period_end", "")),
        "totalIncome": float(r.get("total_income") or 0),
        "totalExpenses": float(r.get("total_expenses") or 0),
        "netIncome": float(r.get("net_income") or 0),
        "fileUrl": r.get("file_path"),
        "createdAt": str(r.get("created_at", "")),
    } for r in (rows.data or [])]

import os
import asyncio
import base64 as b64lib
from datetime import datetime, date
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
import httpx
from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://wzcuzyouymauokijaqjk.supabase.co")
SUPABASE_KEY = (os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY") or "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Ind6Y3V6eW91eW1hdW9raWphcWprIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM5NDUyMDAsImV4cCI6MjA4OTUyMTIwMH0.fDuyCZGrCbL9Obd7l6FDnNd5AB-AUytp-3S60KwwKvM")
RESEND_API_KEY = (os.getenv("RESEND_API_KEY") or os.getenv("RESEND_KEY") or "re_123")

invoice_routes = APIRouter(prefix="/api", tags=["invoices"])


def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def get_business_for_user(supabase, user_id: str):
    """OWNER ONLY — employees cannot access this module."""
    result = supabase.table("businesses").select("*").eq("user_id", user_id).execute()
    if result.data:
        return result.data[0]
    return None



async def get_current_user(authorization: str = None):
    from fastapi import Header
    return authorization


# Re-use auth from receiptvault
def get_auth_dep():
    from routes.receiptvault.routes import get_current_user as rv_auth
    return rv_auth


# ── Models ────────────────────────────────────────────────────────────────────

class InvoiceItem(BaseModel):
    description: str
    quantity: float = 1.0
    unitPrice: float = 0.0
    sortOrder: int = 0


class CreateInvoiceBody(BaseModel):
    customerName: str
    customerEmail: Optional[str] = None
    customerAddress: Optional[str] = None
    customerPhone: Optional[str] = None
    issueDate: Optional[str] = None
    dueDate: Optional[str] = None
    paymentTerms: Optional[str] = "Net 30"
    notes: Optional[str] = None
    taxRate: Optional[float] = 0.0
    items: List[InvoiceItem] = []


class UpdateInvoiceBody(BaseModel):
    customerName: Optional[str] = None
    customerEmail: Optional[str] = None
    customerAddress: Optional[str] = None
    customerPhone: Optional[str] = None
    issueDate: Optional[str] = None
    dueDate: Optional[str] = None
    paymentTerms: Optional[str] = None
    notes: Optional[str] = None
    taxRate: Optional[float] = None
    status: Optional[str] = None
    items: Optional[List[InvoiceItem]] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def to_invoice(row: dict, items: list = []) -> dict:
    return {
        "id": row.get("id"),
        "businessId": row.get("business_id"),
        "invoiceNumber": row.get("invoice_number"),
        "status": row.get("status", "draft"),
        "customerName": row.get("customer_name"),
        "customerEmail": row.get("customer_email"),
        "customerAddress": row.get("customer_address"),
        "customerPhone": row.get("customer_phone"),
        "issueDate": str(row.get("issue_date", "")),
        "dueDate": str(row.get("due_date", "")) if row.get("due_date") else None,
        "paymentTerms": row.get("payment_terms"),
        "notes": row.get("notes"),
        "subtotal": float(row.get("subtotal") or 0),
        "taxRate": float(row.get("tax_rate") or 0),
        "taxAmount": float(row.get("tax_amount") or 0),
        "total": float(row.get("total") or 0),
        "sentAt": str(row.get("sent_at", "")) if row.get("sent_at") else None,
        "paidAt": str(row.get("paid_at", "")) if row.get("paid_at") else None,
        "createdAt": str(row.get("created_at", "")),
        "items": [
            {
                "id": i.get("id"),
                "description": i.get("description"),
                "quantity": float(i.get("quantity") or 1),
                "unitPrice": float(i.get("unit_price") or 0),
                "amount": float(i.get("amount") or 0),
                "sortOrder": i.get("sort_order", 0),
            }
            for i in items
        ],
    }


def next_invoice_number(supabase, business_id: str) -> str:
    result = supabase.table("invoices") \
        .select("invoice_number") \
        .eq("business_id", business_id) \
        .order("created_at", desc=True) \
        .limit(1) \
        .execute()
    if not result.data:
        return "INV-001"
    last = result.data[0].get("invoice_number", "INV-000")
    try:
        num = int(last.split("-")[-1]) + 1
    except:
        num = 1
    return f"INV-{num:03d}"


def compute_totals(items: List[InvoiceItem], tax_rate: float):
    subtotal = sum(i.quantity * i.unitPrice for i in items)
    tax_amount = round(subtotal * (tax_rate / 100), 2)
    total = round(subtotal + tax_amount, 2)
    return round(subtotal, 2), tax_amount, total


# ── Invoice PDF Generator ────────────────────────────────────────────────────

def generate_invoice_pdf(invoice: dict, business: dict) -> bytes:
    """Generate a professional green-branded invoice PDF using reportlab."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from io import BytesIO

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter,
                            rightMargin=0.75*inch, leftMargin=0.75*inch,
                            topMargin=0.75*inch, bottomMargin=0.75*inch)

    GREEN = colors.HexColor("#1a6b3a")
    GREEN_LIGHT = colors.HexColor("#e8f4ec")
    INK = colors.HexColor("#151513")
    INK3 = colors.HexColor("#5c5c58")
    WHITE = colors.white

    styles = getSampleStyleSheet()
    story = []

    # ── Header ────────────────────────────────────────────────────────────────
    header_data = [[
        Paragraph(f'<font color="#1a6b3a" size="22"><b>ReceiptVault</b></font><br/>'
                  f'<font color="#5c5c58" size="9">receipts.dealdily.com</font>', styles["Normal"]),
        Paragraph(f'<font color="#1a6b3a" size="28"><b>INVOICE</b></font><br/>'
                  f'<font color="#5c5c58" size="10">#{invoice["invoiceNumber"]}</font>', styles["Normal"]),
    ]]
    header_table = Table(header_data, colWidths=[3.5*inch, 3.5*inch])
    header_table.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 0.2*inch))
    story.append(HRFlowable(width="100%", thickness=2, color=GREEN))
    story.append(Spacer(1, 0.2*inch))

    # ── From / To / Dates ────────────────────────────────────────────────────
    biz_name = business.get("business_name", "")
    biz_addr = business.get("business_address", "")
    owner = business.get("owner_name", "")

    issue = invoice.get("issueDate", "")
    due = invoice.get("dueDate") or "—"
    terms = invoice.get("paymentTerms") or "Net 30"
    status = invoice.get("status", "draft").upper()
    status_color = "#1a6b3a" if status == "PAID" else "#d97706" if status == "SENT" else "#5c5c58"

    info_data = [
        [
            Paragraph(f'<b>FROM</b><br/><font size="11"><b>{biz_name}</b></font><br/>'
                      f'<font color="#5c5c58">{owner}<br/>{biz_addr}</font>', styles["Normal"]),
            Paragraph(f'<b>BILL TO</b><br/><font size="11"><b>{invoice["customerName"]}</b></font><br/>'
                      f'<font color="#5c5c58">{invoice.get("customerAddress") or ""}<br/>'
                      f'{invoice.get("customerEmail") or ""}<br/>{invoice.get("customerPhone") or ""}</font>',
                      styles["Normal"]),
            Paragraph(f'<b>ISSUE DATE</b><br/>{issue}<br/><br/>'
                      f'<b>DUE DATE</b><br/>{due}<br/><br/>'
                      f'<b>TERMS</b><br/>{terms}<br/><br/>'
                      f'<b>STATUS</b><br/><font color="{status_color}"><b>{status}</b></font>',
                      styles["Normal"]),
        ]
    ]
    info_table = Table(info_data, colWidths=[2.33*inch, 2.33*inch, 2.33*inch])
    info_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), GREEN_LIGHT),
        ("ROUNDEDCORNERS", [6, 6, 6, 6]),
        ("PADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 0.25*inch))

    # ── Line Items ────────────────────────────────────────────────────────────
    item_data = [["DESCRIPTION", "QTY", "UNIT PRICE", "AMOUNT"]]
    for item in invoice.get("items", []):
        item_data.append([
            item["description"],
            str(item["quantity"]),
            f'${item["unitPrice"]:.2f}',
            f'${item["amount"]:.2f}',
        ])

    # Subtotal / tax / total rows
    item_data.append(["", "", "Subtotal", f'${invoice["subtotal"]:.2f}'])
    if invoice.get("taxRate", 0) > 0:
        item_data.append(["", "", f'Tax ({invoice["taxRate"]}%)', f'${invoice["taxAmount"]:.2f}'])
    item_data.append(["", "", "TOTAL DUE", f'${invoice["total"]:.2f}'])

    col_widths = [3.5*inch, 0.7*inch, 1.2*inch, 1.1*inch]
    items_table = Table(item_data, colWidths=col_widths)

    n = len(item_data)
    items_table.setStyle(TableStyle([
        # Header row
        ("BACKGROUND", (0, 0), (-1, 0), GREEN),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("PADDING", (0, 0), (-1, 0), 8),
        # Data rows
        ("FONTSIZE", (0, 1), (-1, n-4), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, n-4), [WHITE, GREEN_LIGHT]),
        ("PADDING", (0, 1), (-1, -1), 7),
        # Totals rows
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("FONTNAME", (2, n-1), (-1, n-1), "Helvetica-Bold"),
        ("FONTSIZE", (2, n-1), (-1, n-1), 11),
        ("TEXTCOLOR", (2, n-1), (-1, n-1), GREEN),
        ("LINEABOVE", (2, n-1), (-1, n-1), 1.5, GREEN),
        ("GRID", (0, 0), (-1, n-4), 0.5, colors.HexColor("#e8e8e4")),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 0.2*inch))

    # ── Notes ─────────────────────────────────────────────────────────────────
    if invoice.get("notes"):
        story.append(Paragraph(f'<b>Notes:</b> {invoice["notes"]}',
                                ParagraphStyle("notes", parent=styles["Normal"],
                                               fontSize=9, textColor=INK3)))
        story.append(Spacer(1, 0.1*inch))

    # ── Footer ─────────────────────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=1, color=GREEN_LIGHT))
    story.append(Spacer(1, 0.1*inch))
    story.append(Paragraph(
        f'<font color="#9c9c96" size="8">Generated by ReceiptVault · receipts.dealdily.com</font>',
        ParagraphStyle("footer", parent=styles["Normal"], alignment=1)
    ))

    doc.build(story)
    return buffer.getvalue()


# ── Routes ────────────────────────────────────────────────────────────────────

from routes.receiptvault.routes import get_current_user


@invoice_routes.get("/invoices")
async def list_invoices(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    biz = type("BizR", (), {"data": ([{"id": _b["id"]}] if (_b := get_business_for_user(supabase, current_user.user.id)) else [])})()
    if not biz.data:
        return []
    business_id = biz.data[0]["id"]
    rows = supabase.table("invoices").select("*").eq("business_id", business_id)\
        .order("created_at", desc=True).execute()
    return [to_invoice(r) for r in (rows.data or [])]


@invoice_routes.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    biz = type("BizR", (), {"data": ([{"id": _b["id"]}] if (_b := get_business_for_user(supabase, current_user.user.id)) else [])})()
    if not biz.data:
        raise HTTPException(status_code=404, detail="Business not found")
    business_id = biz.data[0]["id"]
    row = supabase.table("invoices").select("*").eq("id", invoice_id)\
        .eq("business_id", business_id).execute()
    if not row.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    items = supabase.table("invoice_items").select("*").eq("invoice_id", invoice_id)\
        .order("sort_order").execute()
    return to_invoice(row.data[0], items.data or [])


@invoice_routes.post("/invoices")
async def create_invoice(body: CreateInvoiceBody, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    biz = type("BizR", (), {"data": ([_b] if (_b := get_business_for_user(supabase, current_user.user.id)) else [])})()
    if not biz.data:
        raise HTTPException(status_code=400, detail="Business profile required")
    business_id = biz.data[0]["id"]

    inv_num = next_invoice_number(supabase, business_id)
    subtotal, tax_amount, total = compute_totals(body.items, body.taxRate or 0)

    inv_data = {
        "business_id": business_id,
        "invoice_number": inv_num,
        "status": "draft",
        "customer_name": body.customerName,
        "customer_email": body.customerEmail,
        "customer_address": body.customerAddress,
        "customer_phone": body.customerPhone,
        "issue_date": body.issueDate or str(date.today()),
        "due_date": body.dueDate,
        "payment_terms": body.paymentTerms or "Net 30",
        "notes": body.notes,
        "subtotal": subtotal,
        "tax_rate": body.taxRate or 0,
        "tax_amount": tax_amount,
        "total": total,
    }

    inv = supabase.table("invoices").insert(inv_data).execute()
    if not inv.data:
        raise HTTPException(status_code=500, detail="Failed to create invoice")

    invoice_id = inv.data[0]["id"]

    # Insert line items
    if body.items:
        items_data = [
            {
                "invoice_id": invoice_id,
                "description": item.description,
                "quantity": item.quantity,
                "unit_price": item.unitPrice,
                "sort_order": item.sortOrder,
            }
            for item in body.items
        ]
        supabase.table("invoice_items").insert(items_data).execute()

    items_rows = supabase.table("invoice_items").select("*").eq("invoice_id", invoice_id).execute()
    return to_invoice(inv.data[0], items_rows.data or [])


@invoice_routes.patch("/invoices/{invoice_id}")
async def update_invoice(invoice_id: str, body: UpdateInvoiceBody, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    biz = type("BizR", (), {"data": ([{"id": _b["id"]}] if (_b := get_business_for_user(supabase, current_user.user.id)) else [])})()
    if not biz.data:
        raise HTTPException(status_code=404, detail="Not found")
    business_id = biz.data[0]["id"]

    update_data = {}
    if body.customerName is not None: update_data["customer_name"] = body.customerName
    if body.customerEmail is not None: update_data["customer_email"] = body.customerEmail
    if body.customerAddress is not None: update_data["customer_address"] = body.customerAddress
    if body.customerPhone is not None: update_data["customer_phone"] = body.customerPhone
    if body.issueDate is not None: update_data["issue_date"] = body.issueDate
    if body.dueDate is not None: update_data["due_date"] = body.dueDate
    if body.paymentTerms is not None: update_data["payment_terms"] = body.paymentTerms
    if body.notes is not None: update_data["notes"] = body.notes
    if body.status is not None: update_data["status"] = body.status

    # Recalculate totals if items changed
    if body.items is not None:
        tax_rate = body.taxRate if body.taxRate is not None else 0
        subtotal, tax_amount, total = compute_totals(body.items, tax_rate)
        update_data.update({
            "subtotal": subtotal, "tax_rate": tax_rate,
            "tax_amount": tax_amount, "total": total,
        })
        # Replace items
        supabase.table("invoice_items").delete().eq("invoice_id", invoice_id).execute()
        if body.items:
            supabase.table("invoice_items").insert([
                {"invoice_id": invoice_id, "description": i.description,
                 "quantity": i.quantity, "unit_price": i.unitPrice, "sort_order": i.sortOrder}
                for i in body.items
            ]).execute()

    if update_data:
        update_data["updated_at"] = datetime.utcnow().isoformat()
        supabase.table("invoices").update(update_data).eq("id", invoice_id)\
            .eq("business_id", business_id).execute()

    row = supabase.table("invoices").select("*").eq("id", invoice_id).execute()
    items = supabase.table("invoice_items").select("*").eq("invoice_id", invoice_id)\
        .order("sort_order").execute()
    return to_invoice(row.data[0], items.data or [])


@invoice_routes.delete("/invoices/{invoice_id}")
async def delete_invoice(invoice_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    biz = type("BizR", (), {"data": ([{"id": _b["id"]}] if (_b := get_business_for_user(supabase, current_user.user.id)) else [])})()
    if not biz.data:
        raise HTTPException(status_code=404, detail="Not found")
    business_id = biz.data[0]["id"]
    supabase.table("invoice_items").delete().eq("invoice_id", invoice_id).execute()
    supabase.table("invoices").delete().eq("id", invoice_id).eq("business_id", business_id).execute()
    return {"ok": True}


@invoice_routes.post("/invoices/{invoice_id}/send")
async def send_invoice(invoice_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    biz = type("BizR", (), {"data": ([_b] if (_b := get_business_for_user(supabase, current_user.user.id)) else [])})()
    if not biz.data:
        raise HTTPException(status_code=404, detail="Not found")
    business = biz.data[0]
    business_id = business["id"]

    row = supabase.table("invoices").select("*").eq("id", invoice_id)\
        .eq("business_id", business_id).execute()
    if not row.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice_row = row.data[0]

    if not invoice_row.get("customer_email"):
        raise HTTPException(status_code=400, detail="Customer email required to send invoice")

    items = supabase.table("invoice_items").select("*").eq("invoice_id", invoice_id)\
        .order("sort_order").execute()
    invoice = to_invoice(invoice_row, items.data or [])

    # Generate PDF
    pdf_bytes = generate_invoice_pdf(invoice, business)
    pdf_b64 = b64lib.b64encode(pdf_bytes).decode()
    safe_name = business.get("business_name", "Business").replace(" ", "_")
    pdf_filename = f"Invoice_{safe_name}_{invoice['invoiceNumber']}.pdf"

    # Send via Supabase Edge Function (which has Resend key configured)
    EDGE_URL = f"{SUPABASE_URL}/functions/v1/send-invoice"
    
    # Build HTML for the invoice email
    items_html = "".join(
        f'<tr style="background:{"white" if i%2==0 else "#e8f4ec"}"><td style="padding:8px 12px">{item["description"]}</td><td style="padding:8px 12px;text-align:right">${item["amount"]:.2f}</td></tr>'
        for i, item in enumerate(invoice["items"])
    )
    html = f"""<div style="font-family:sans-serif;max-width:600px;margin:0 auto">
      <div style="background:#1a6b3a;padding:24px 32px;border-radius:12px 12px 0 0">
        <h1 style="color:white;margin:0;font-size:22px">Invoice {invoice['invoiceNumber']}</h1>
        <p style="color:#a7d4b5;margin:4px 0 0">from {business.get('business_name','')}</p>
      </div>
      <div style="background:#f2faf5;padding:24px 32px">
        <p style="color:#2c2c2a">Dear <b>{invoice['customerName']}</b>,</p>
        <p style="color:#5c5c58">Please find your invoice attached. Total due: <b style="color:#1a6b3a;font-size:18px">${invoice['total']:.2f}</b>.</p>
        <table style="width:100%;border-collapse:collapse;margin:16px 0">
          <tr style="background:#1a6b3a;color:white"><th style="padding:8px 12px;text-align:left">Description</th><th style="padding:8px 12px;text-align:right">Amount</th></tr>
          {items_html}
          <tr><td style="padding:8px 12px;text-align:right;color:#5c5c58">Total</td><td style="padding:8px 12px;text-align:right;font-weight:bold;color:#1a6b3a;font-size:16px">${invoice['total']:.2f}</td></tr>
        </table>
        {"<p style='color:#5c5c58'><b>Due Date:</b> " + str(invoice.get('dueDate','')) + "</p>" if invoice.get('dueDate') else ""}
        {"<p style='color:#5c5c58'><b>Notes:</b> " + str(invoice.get('notes','')) + "</p>" if invoice.get('notes') else ""}
        <p style="color:#9c9c96;font-size:12px;margin-top:24px">Generated by ReceiptVault · receipts.dealdily.com</p>
      </div>
    </div>"""

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{SUPABASE_URL}/functions/v1/send-invoice",
            headers={"apikey": SUPABASE_KEY, "Content-Type": "application/json"},
            json={
                "to": invoice_row["customer_email"],
                "from": business.get("business_name", "ReceiptVault"),
                "subject": f"Invoice {invoice['invoiceNumber']} from {business.get('business_name', '')}",
                "html": html,
                "pdfBase64": pdf_b64,
                "pdfFilename": pdf_filename,
            }
        )

    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=f"Email failed: {resp.text}")

    # Mark as sent
    supabase.table("invoices").update({
        "status": "sent",
        "sent_at": datetime.utcnow().isoformat()
    }).eq("id", invoice_id).execute()

    return {"ok": True, "invoiceNumber": invoice["invoiceNumber"], "sentTo": invoice_row["customer_email"]}

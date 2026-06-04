"""
ReceiptVault PDF Generator — lightweight version
Cover sheet + receipt index only. No image downloads = no memory spikes.
"""

import io
from datetime import datetime
from typing import List, Dict, Any, Optional

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT

GREEN       = colors.HexColor("#1a6b3a")
GREEN_LIGHT = colors.HexColor("#e8f4ec")
GREEN_MID   = colors.HexColor("#2d8c52")
BLACK       = colors.HexColor("#151513")
GRAY        = colors.HexColor("#5c5c58")
GRAY_LIGHT  = colors.HexColor("#f4f4f3")
BORDER      = colors.HexColor("#eaeae8")

CATEGORY_ICONS = {
    "Meals & Entertainment": "Meals",
    "Travel": "Travel",
    "Office Supplies": "Office",
    "Utilities": "Utilities",
    "Software & Subscriptions": "Software",
    "Advertising": "Advertising",
    "Vehicle & Fuel": "Vehicle",
    "Equipment": "Equipment",
    "Other": "Other",
}


async def generate_cover_pdf(
    business_name: str,
    owner_name: str,
    accountant_email: str,
    receipts: List[Dict[str, Any]],
    period_label: Optional[str] = None,
) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.75*inch, rightMargin=0.75*inch,
        topMargin=0.75*inch, bottomMargin=0.75*inch,
    )
    W = letter[0] - 1.5*inch
    story = []
    now = datetime.now()
    period = period_label or now.strftime("%B %Y")

    def S(name, **kw):
        return ParagraphStyle(name, **kw)

    # ── HEADER ────────────────────────────────────────────────────────────────
    header = Table([[
        Paragraph(business_name or "Your Business", S("co", fontSize=26, fontName="Helvetica-Bold", textColor=colors.white, leading=30)),
        Paragraph("ReceiptVault", S("rv", fontSize=10, fontName="Helvetica-Bold", textColor=colors.white, alignment=TA_RIGHT)),
    ]], colWidths=[W*0.75, W*0.25])
    header.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), GREEN),
        ("TOPPADDING", (0,0), (-1,-1), 18), ("BOTTOMPADDING", (0,0), (-1,-1), 18),
        ("LEFTPADDING", (0,0), (0,-1), 20), ("RIGHTPADDING", (-1,0), (-1,-1), 20),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(header)
    story.append(Spacer(1, 20))

    # ── PERIOD ────────────────────────────────────────────────────────────────
    story.append(Paragraph("EXPENSE REPORT", S("ey", fontSize=9, fontName="Helvetica-Bold", textColor=GREEN, letterSpacing=1.5)))
    story.append(Spacer(1, 6))
    story.append(Paragraph(period, S("ph", fontSize=22, fontName="Helvetica-Bold", textColor=BLACK, leading=26)))
    story.append(Paragraph(
        f"Prepared {now.strftime('%B %d, %Y')}  ·  For {accountant_email}",
        S("ps", fontSize=11, textColor=GRAY)
    ))
    story.append(Spacer(1, 16))
    story.append(HRFlowable(width=W, thickness=1, color=BORDER, spaceAfter=16))

    # ── CATEGORY TOTALS ───────────────────────────────────────────────────────
    category_totals: Dict[str, float] = {}
    total_amount = 0.0
    for r in receipts:
        cat = r.get("category") or "Other"
        amt = r.get("amount")
        if amt is not None:
            try:
                val = float(amt)
                category_totals[cat] = category_totals.get(cat, 0.0) + val
                total_amount += val
            except (ValueError, TypeError):
                pass

    story.append(Paragraph("SPENDING BY CATEGORY", S("sl", fontSize=9, fontName="Helvetica-Bold", textColor=GREEN, letterSpacing=1.5)))
    story.append(Spacer(1, 8))

    if category_totals:
        rows = []
        for cat, amt in sorted(category_totals.items(), key=lambda x: -x[1]):
            pct = (amt / total_amount * 100) if total_amount > 0 else 0
            rows.append([
                Paragraph(cat, S("cn", fontSize=10, textColor=BLACK, leftIndent=4)),
                Paragraph(f"{pct:.0f}%", S("cp", fontSize=9, textColor=GRAY, alignment=TA_RIGHT)),
                Paragraph(f"${amt:,.2f}", S("ca", fontSize=10, fontName="Helvetica-Bold", textColor=BLACK, alignment=TA_RIGHT)),
            ])
        cat_table = Table(rows, colWidths=[W*0.60, W*0.15, W*0.25], rowHeights=34)
        style_cmds = [
            ("TOPPADDING",(0,0),(-1,-1),8), ("BOTTOMPADDING",(0,0),(-1,-1),8),
            ("LEFTPADDING",(0,0),(0,-1),12), ("RIGHTPADDING",(-1,0),(-1,-1),12),
            ("LINEBELOW",(0,0),(-1,-2),0.5,BORDER), ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ]
        for i in range(len(rows)):
            if i % 2 == 0:
                style_cmds.append(("BACKGROUND",(0,i),(-1,i),GRAY_LIGHT))
        cat_table.setStyle(TableStyle(style_cmds))
        story.append(cat_table)
    else:
        story.append(Paragraph("No categorized receipts.", S("nc", fontSize=10, textColor=GRAY)))

    story.append(Spacer(1, 20))
    story.append(HRFlowable(width=W, thickness=1, color=BORDER, spaceAfter=0))

    # ── TOTALS BOX ────────────────────────────────────────────────────────────
    totals = Table([[
        Table([[[Paragraph("TOTAL EXPENSES", S("tl", fontSize=10, textColor=GRAY))],
                [Paragraph(f"${total_amount:,.2f}", S("ta", fontSize=32, fontName="Helvetica-Bold", textColor=GREEN, leading=36))]]],
               colWidths=[W*0.5]),
        Table([[[Paragraph("RECEIPTS", S("tl2", fontSize=10, textColor=GRAY))],
                [Paragraph(str(len(receipts)), S("ta2", fontSize=32, fontName="Helvetica-Bold", textColor=GREEN, leading=36))]]],
               colWidths=[W*0.25]),
        Table([[[Paragraph("PERIOD", S("tl3", fontSize=10, textColor=GRAY))],
                [Paragraph(period, S("ta3", fontSize=14, fontName="Helvetica-Bold", textColor=BLACK, leading=18))]]],
               colWidths=[W*0.25]),
    ]], colWidths=[W*0.5, W*0.25, W*0.25])
    totals.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,-1),GREEN_LIGHT),
        ("TOPPADDING",(0,0),(-1,-1),20), ("BOTTOMPADDING",(0,0),(-1,-1),20),
        ("LEFTPADDING",(0,0),(0,-1),20), ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("LINEABOVE",(0,0),(-1,0),3,GREEN),
    ]))
    story.append(totals)
    story.append(Spacer(1, 28))

    # ── RECEIPT INDEX ─────────────────────────────────────────────────────────
    story.append(Paragraph("RECEIPT INDEX", S("si", fontSize=9, fontName="Helvetica-Bold", textColor=GREEN, letterSpacing=1.5)))
    story.append(Spacer(1, 8))

    hdr = [
        Paragraph("#", S("ih", fontSize=8, fontName="Helvetica-Bold", textColor=colors.white, alignment=TA_CENTER)),
        Paragraph("MERCHANT", S("ih2", fontSize=8, fontName="Helvetica-Bold", textColor=colors.white)),
        Paragraph("DATE", S("ih3", fontSize=8, fontName="Helvetica-Bold", textColor=colors.white)),
        Paragraph("CATEGORY", S("ih4", fontSize=8, fontName="Helvetica-Bold", textColor=colors.white)),
        Paragraph("AMOUNT", S("ih5", fontSize=8, fontName="Helvetica-Bold", textColor=colors.white, alignment=TA_RIGHT)),
    ]
    idx_rows = [hdr]
    for i, r in enumerate(receipts, 1):
        merchant = r.get("merchant") or r.get("originalName") or r.get("original_name") or "Unknown"
        date_raw = r.get("receiptDate") or r.get("receipt_date") or r.get("uploadedAt") or r.get("uploaded_at") or ""
        try:
            date_str = datetime.fromisoformat(str(date_raw)[:10]).strftime("%b %d, %Y")
        except Exception:
            date_str = str(date_raw)[:10] if date_raw else "—"
        cat = r.get("category") or "Uncategorized"
        amt = r.get("amount")
        amt_str = f"${float(amt):,.2f}" if amt is not None else "—"
        idx_rows.append([
            Paragraph(str(i), S("ir", fontSize=9, textColor=GRAY, alignment=TA_CENTER)),
            Paragraph(str(merchant)[:32], S("ir2", fontSize=9, textColor=BLACK)),
            Paragraph(date_str, S("ir3", fontSize=9, textColor=GRAY)),
            Paragraph(cat, S("ir4", fontSize=9, textColor=GRAY)),
            Paragraph(amt_str, S("ir5", fontSize=9, fontName="Helvetica-Bold", textColor=BLACK, alignment=TA_RIGHT)),
        ])

    idx_table = Table(idx_rows, colWidths=[W*0.06, W*0.32, W*0.18, W*0.26, W*0.18])
    idx_style = [
        ("BACKGROUND",(0,0),(-1,0),GREEN),
        ("TOPPADDING",(0,0),(-1,-1),7), ("BOTTOMPADDING",(0,0),(-1,-1),7),
        ("LEFTPADDING",(0,0),(-1,-1),6),
        ("LINEBELOW",(0,0),(-1,-2),0.5,BORDER),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
    ]
    for i in range(1, len(idx_rows)):
        if i % 2 == 0:
            idx_style.append(("BACKGROUND",(0,i),(-1,i),GRAY_LIGHT))
    idx_table.setStyle(TableStyle(idx_style))
    story.append(idx_table)

    # ── FOOTER ────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 24))
    story.append(Paragraph(
        f"Generated by ReceiptVault  ·  {now.strftime('%B %d, %Y at %I:%M %p')}  ·  receipts.dealdily.com",
        S("ft", fontSize=8, textColor=GRAY, alignment=TA_CENTER)
    ))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "Original receipt images are stored securely in ReceiptVault and available for review at any time.",
        S("ft2", fontSize=8, textColor=GRAY, alignment=TA_CENTER)
    ))

    doc.build(story)
    buf.seek(0)
    return buf.read()

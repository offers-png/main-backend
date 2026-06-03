"""
ReceiptVault PDF Generator
Builds a cover-sheet + receipt-images PDF for accountant delivery.
"""

import io
import os
import httpx
from datetime import datetime
from typing import List, Dict, Any, Optional

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, Image as RLImage
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT


# ── Brand colors ────────────────────────────────────────────────────────────
GREEN       = colors.HexColor("#1a6b3a")
GREEN_LIGHT = colors.HexColor("#e8f4ec")
GREEN_MID   = colors.HexColor("#2d8c52")
BLACK       = colors.HexColor("#151513")
GRAY        = colors.HexColor("#5c5c58")
GRAY_LIGHT  = colors.HexColor("#f4f4f3")
BORDER      = colors.HexColor("#eaeae8")
WHITE       = colors.white

CATEGORY_COLORS: Dict[str, Any] = {
    "Meals & Entertainment": colors.HexColor("#fff4e6"),
    "Travel":                colors.HexColor("#e8f0fe"),
    "Office Supplies":       colors.HexColor("#e8f4ec"),
    "Utilities":             colors.HexColor("#fce8e8"),
    "Software & Subscriptions": colors.HexColor("#f3e8ff"),
    "Advertising":           colors.HexColor("#fce8f3"),
    "Vehicle & Fuel":        colors.HexColor("#fef9e8"),
    "Equipment":             colors.HexColor("#e8f8fc"),
    "Other":                 colors.HexColor("#f4f4f3"),
}

CATEGORY_ICONS = {
    "Meals & Entertainment": "🍽",
    "Travel":                "✈",
    "Office Supplies":       "📎",
    "Utilities":             "⚡",
    "Software & Subscriptions": "💻",
    "Advertising":           "📢",
    "Vehicle & Fuel":        "⛽",
    "Equipment":             "🔧",
    "Other":                 "📄",
}


def _styles():
    base = getSampleStyleSheet()

    styles = {
        "company": ParagraphStyle(
            "company",
            fontSize=28, fontName="Helvetica-Bold",
            textColor=BLACK, spaceAfter=4,
            leading=32,
        ),
        "tagline": ParagraphStyle(
            "tagline",
            fontSize=11, fontName="Helvetica",
            textColor=GRAY, spaceAfter=2,
        ),
        "section_label": ParagraphStyle(
            "section_label",
            fontSize=9, fontName="Helvetica-Bold",
            textColor=GREEN, spaceBefore=24, spaceAfter=8,
            letterSpacing=1.5,
        ),
        "period_heading": ParagraphStyle(
            "period_heading",
            fontSize=18, fontName="Helvetica-Bold",
            textColor=BLACK, spaceAfter=4, leading=22,
        ),
        "period_sub": ParagraphStyle(
            "period_sub",
            fontSize=11, fontName="Helvetica",
            textColor=GRAY, spaceAfter=16,
        ),
        "total_label": ParagraphStyle(
            "total_label",
            fontSize=10, fontName="Helvetica",
            textColor=GRAY,
        ),
        "total_amount": ParagraphStyle(
            "total_amount",
            fontSize=32, fontName="Helvetica-Bold",
            textColor=GREEN, leading=36,
        ),
        "footer": ParagraphStyle(
            "footer",
            fontSize=8, fontName="Helvetica",
            textColor=GRAY, alignment=TA_CENTER,
        ),
        "receipt_title": ParagraphStyle(
            "receipt_title",
            fontSize=10, fontName="Helvetica-Bold",
            textColor=BLACK, spaceAfter=2,
        ),
        "receipt_meta": ParagraphStyle(
            "receipt_meta",
            fontSize=9, fontName="Helvetica",
            textColor=GRAY,
        ),
    }
    return styles


async def _fetch_image(url: str) -> Optional[bytes]:
    """Download an image from Supabase Storage."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.get(url)
            if res.status_code == 200:
                return res.content
    except Exception:
        pass
    return None


def _is_image_mime(url: str) -> bool:
    lower = url.lower()
    return any(lower.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"])


async def generate_cover_pdf(
    business_name: str,
    owner_name: str,
    accountant_email: str,
    receipts: List[Dict[str, Any]],
    period_label: Optional[str] = None,
) -> bytes:
    """
    Generate a complete PDF:
      Page 1  — Cover sheet with summary table
      Page 2+ — One receipt image per page (PDFs skipped with a note)
    Returns raw PDF bytes.
    """

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )

    S = _styles()
    W = letter[0] - 1.5 * inch   # usable width
    story = []

    # ── COVER PAGE ───────────────────────────────────────────────────────────

    # Header bar — green stripe
    header_data = [[
        Paragraph(business_name or "Your Business", S["company"]),
        Paragraph("ReceiptVault", ParagraphStyle(
            "rv", fontSize=10, fontName="Helvetica-Bold",
            textColor=WHITE, alignment=TA_RIGHT
        )),
    ]]
    header_table = Table(header_data, colWidths=[W * 0.75, W * 0.25])
    header_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), GREEN),
        ("TOPPADDING",    (0, 0), (-1, -1), 18),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 18),
        ("LEFTPADDING",   (0, 0), (0, -1), 20),
        ("RIGHTPADDING",  (-1, 0), (-1, -1), 20),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 20))

    # Period and meta
    now = datetime.now()
    period = period_label or now.strftime("%B %Y")
    story.append(Paragraph("EXPENSE REPORT", S["section_label"]))
    story.append(Paragraph(period, S["period_heading"]))
    story.append(Paragraph(
        f"Prepared {now.strftime('%B %d, %Y')}  ·  Delivered to {accountant_email}",
        S["period_sub"]
    ))

    story.append(HRFlowable(width=W, thickness=1, color=BORDER, spaceAfter=16))

    # ── Category summary table ───────────────────────────────────────────────
    category_totals: Dict[str, float] = {}
    total_amount = 0.0
    receipt_count = len(receipts)

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

    story.append(Paragraph("SPENDING BY CATEGORY", S["section_label"]))

    if category_totals:
        cat_rows = []
        for cat, amt in sorted(category_totals.items(), key=lambda x: -x[1]):
            icon = CATEGORY_ICONS.get(cat, "•")
            pct = (amt / total_amount * 100) if total_amount > 0 else 0
            cat_rows.append([
                Paragraph(f"{icon}  {cat}", ParagraphStyle(
                    "cat_name", fontSize=10, fontName="Helvetica",
                    textColor=BLACK, leftIndent=4,
                )),
                Paragraph(f"{pct:.0f}%", ParagraphStyle(
                    "pct", fontSize=9, fontName="Helvetica",
                    textColor=GRAY, alignment=TA_RIGHT,
                )),
                Paragraph(f"${amt:,.2f}", ParagraphStyle(
                    "cat_amt", fontSize=10, fontName="Helvetica-Bold",
                    textColor=BLACK, alignment=TA_RIGHT,
                )),
            ])

        cat_table = Table(
            cat_rows,
            colWidths=[W * 0.60, W * 0.15, W * 0.25],
            rowHeights=36,
        )
        style_cmds = [
            ("TOPPADDING",    (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING",   (0, 0), (0, -1), 12),
            ("RIGHTPADDING",  (-1, 0), (-1, -1), 12),
            ("LINEBELOW",     (0, 0), (-1, -2), 0.5, BORDER),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]
        # Alternate row shading
        for i in range(len(cat_rows)):
            if i % 2 == 0:
                style_cmds.append(("BACKGROUND", (0, i), (-1, i), GRAY_LIGHT))
        cat_table.setStyle(TableStyle(style_cmds))
        story.append(cat_table)
    else:
        story.append(Paragraph(
            "No categorized receipts in this period.",
            ParagraphStyle("nc", fontSize=10, textColor=GRAY)
        ))

    story.append(Spacer(1, 24))
    story.append(HRFlowable(width=W, thickness=1, color=BORDER, spaceAfter=0))

    # ── Grand total box ──────────────────────────────────────────────────────
    total_data = [[
        Table([[
            [Paragraph("TOTAL EXPENSES", S["total_label"])],
            [Paragraph(f"${total_amount:,.2f}", S["total_amount"])],
        ]], colWidths=[W * 0.5]),
        Table([[
            [Paragraph("RECEIPTS", S["total_label"])],
            [Paragraph(str(receipt_count), S["total_amount"])],
        ]], colWidths=[W * 0.25]),
        Table([[
            [Paragraph("PERIOD", S["total_label"])],
            [Paragraph(period, ParagraphStyle(
                "per_val", fontSize=14, fontName="Helvetica-Bold",
                textColor=BLACK, leading=18,
            ))],
        ]], colWidths=[W * 0.25]),
    ]]
    total_table = Table(total_data, colWidths=[W * 0.5, W * 0.25, W * 0.25])
    total_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), GREEN_LIGHT),
        ("TOPPADDING",    (0, 0), (-1, -1), 20),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 20),
        ("LEFTPADDING",   (0, 0), (0, -1), 20),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("LINEABOVE",     (0, 0), (-1, 0), 3, GREEN),
    ]))
    story.append(total_table)
    story.append(Spacer(1, 32))

    # ── Receipt list table (index) ───────────────────────────────────────────
    story.append(Paragraph("RECEIPT INDEX", S["section_label"]))

    index_header = [
        Paragraph("#", ParagraphStyle("h", fontSize=8, fontName="Helvetica-Bold", textColor=WHITE, alignment=TA_CENTER)),
        Paragraph("MERCHANT", ParagraphStyle("h", fontSize=8, fontName="Helvetica-Bold", textColor=WHITE)),
        Paragraph("DATE", ParagraphStyle("h", fontSize=8, fontName="Helvetica-Bold", textColor=WHITE)),
        Paragraph("CATEGORY", ParagraphStyle("h", fontSize=8, fontName="Helvetica-Bold", textColor=WHITE)),
        Paragraph("AMOUNT", ParagraphStyle("h", fontSize=8, fontName="Helvetica-Bold", textColor=WHITE, alignment=TA_RIGHT)),
    ]
    index_rows = [index_header]

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

        index_rows.append([
            Paragraph(str(i), ParagraphStyle("n", fontSize=9, fontName="Helvetica", textColor=GRAY, alignment=TA_CENTER)),
            Paragraph(merchant[:32], ParagraphStyle("m", fontSize=9, fontName="Helvetica", textColor=BLACK)),
            Paragraph(date_str, ParagraphStyle("d", fontSize=9, fontName="Helvetica", textColor=GRAY)),
            Paragraph(cat, ParagraphStyle("c", fontSize=9, fontName="Helvetica", textColor=GRAY)),
            Paragraph(amt_str, ParagraphStyle("a", fontSize=9, fontName="Helvetica-Bold", textColor=BLACK, alignment=TA_RIGHT)),
        ])

    index_table = Table(
        index_rows,
        colWidths=[W*0.06, W*0.32, W*0.18, W*0.26, W*0.18],
    )
    idx_style = [
        ("BACKGROUND",    (0, 0), (-1, 0), GREEN),
        ("TOPPADDING",    (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("LINEBELOW",     (0, 0), (-1, -2), 0.5, BORDER),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]
    for i in range(1, len(index_rows)):
        if i % 2 == 0:
            idx_style.append(("BACKGROUND", (0, i), (-1, i), GRAY_LIGHT))
    index_table.setStyle(TableStyle(idx_style))
    story.append(index_table)

    # Footer
    story.append(Spacer(1, 24))
    story.append(Paragraph(
        f"Generated by ReceiptVault  ·  {now.strftime('%B %d, %Y at %I:%M %p')}  ·  receipts.dealdily.com",
        S["footer"]
    ))

    # ── RECEIPT IMAGE PAGES ──────────────────────────────────────────────────
    for i, r in enumerate(receipts, 1):
        story.append(PageBreak())

        merchant = r.get("merchant") or r.get("originalName") or r.get("original_name") or "Receipt"
        date_raw = r.get("receiptDate") or r.get("receipt_date") or r.get("uploadedAt") or r.get("uploaded_at") or ""
        try:
            date_str = datetime.fromisoformat(str(date_raw)[:10]).strftime("%B %d, %Y")
        except Exception:
            date_str = str(date_raw)[:10] if date_raw else "—"
        cat = r.get("category") or "Uncategorized"
        amt = r.get("amount")
        amt_str = f"${float(amt):,.2f}" if amt is not None else "—"
        file_path = r.get("filePath") or r.get("file_path") or ""

        # Receipt page header
        page_header = [[
            Paragraph(f"Receipt #{i} of {receipt_count}", ParagraphStyle(
                "rh", fontSize=9, fontName="Helvetica", textColor=WHITE,
            )),
            Paragraph(amt_str, ParagraphStyle(
                "ra", fontSize=14, fontName="Helvetica-Bold", textColor=WHITE, alignment=TA_RIGHT,
            )),
        ]]
        ph_table = Table(page_header, colWidths=[W * 0.7, W * 0.3])
        ph_table.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), GREEN),
            ("TOPPADDING",    (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LEFTPADDING",   (0, 0), (0, 0), 14),
            ("RIGHTPADDING",  (-1, 0), (-1, 0), 14),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(ph_table)
        story.append(Spacer(1, 12))

        # Receipt meta row
        meta_data = [[
            Paragraph(f"<b>{merchant}</b>", ParagraphStyle("mn", fontSize=12, fontName="Helvetica-Bold", textColor=BLACK)),
            Paragraph(date_str, ParagraphStyle("md", fontSize=10, fontName="Helvetica", textColor=GRAY, alignment=TA_CENTER)),
            Paragraph(cat, ParagraphStyle("mc", fontSize=10, fontName="Helvetica", textColor=GREEN, alignment=TA_RIGHT)),
        ]]
        meta_table = Table(meta_data, colWidths=[W*0.45, W*0.25, W*0.30])
        meta_table.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), GRAY_LIGHT),
            ("TOPPADDING",    (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LEFTPADDING",   (0, 0), (0, 0), 14),
            ("RIGHTPADDING",  (-1, 0), (-1, 0), 14),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(meta_table)
        story.append(Spacer(1, 16))

        # Receipt image or note
        if file_path and _is_image_mime(file_path):
            img_bytes = await _fetch_image(file_path)
            if img_bytes:
                try:
                    img_buf = io.BytesIO(img_bytes)
                    img = RLImage(img_buf, width=W, height=6.5 * inch, kind="proportional")
                    story.append(img)
                except Exception:
                    story.append(Paragraph(
                        "⚠ Image could not be rendered. Original file stored in ReceiptVault.",
                        ParagraphStyle("err", fontSize=10, textColor=GRAY, alignment=TA_CENTER),
                    ))
            else:
                story.append(Paragraph(
                    "⚠ Image unavailable. Original file stored in ReceiptVault.",
                    ParagraphStyle("err", fontSize=10, textColor=GRAY, alignment=TA_CENTER),
                ))
        elif file_path and file_path.lower().endswith(".pdf"):
            story.append(Spacer(1, 40))
            story.append(Paragraph(
                "📄 PDF Receipt",
                ParagraphStyle("pt", fontSize=16, fontName="Helvetica-Bold", textColor=BLACK, alignment=TA_CENTER),
            ))
            story.append(Spacer(1, 8))
            story.append(Paragraph(
                "This receipt was submitted as a PDF document.\nThe original file is stored securely in ReceiptVault.",
                ParagraphStyle("pd", fontSize=11, textColor=GRAY, alignment=TA_CENTER, leading=18),
            ))
        else:
            story.append(Paragraph(
                "No image available for this receipt.",
                ParagraphStyle("na", fontSize=10, textColor=GRAY, alignment=TA_CENTER),
            ))

    doc.build(story)
    buf.seek(0)
    return buf.read()

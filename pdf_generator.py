"""
ReceiptVault PDF Generator — clean professional layout
"""
import io
from datetime import datetime
from typing import List, Dict, Any, Optional
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

GREEN      = colors.HexColor("#1a6b3a")
GREEN_LIGHT= colors.HexColor("#e8f4ec")
GREEN_MID  = colors.HexColor("#2d8c52")
BLACK      = colors.HexColor("#151513")
GRAY       = colors.HexColor("#5c5c58")
GRAY_LIGHT = colors.HexColor("#f4f4f3")
BORDER     = colors.HexColor("#eaeae8")
WHITE      = colors.white

def S(name, **kw):
    return ParagraphStyle(name, fontName=kw.pop("font","Helvetica"), **kw)

async def generate_cover_pdf(
    business_name: str,
    owner_name: str,
    accountant_email: str,
    receipts: List[Dict[str, Any]],
    period_label: Optional[str] = None,
    bank_transactions: Optional[List[Dict[str, Any]]] = None,
) -> bytes:
    """bank_transactions: business bank/card charges for the period (personal
    ones already left out by the caller). Charges matched to a receipt point
    at that receipt's number in the index, so nothing is counted twice."""
    buf = io.BytesIO()
    W = letter[0] - 1.5*inch
    doc = SimpleDocTemplate(buf, pagesize=letter,
        leftMargin=0.75*inch, rightMargin=0.75*inch,
        topMargin=0.75*inch, bottomMargin=0.75*inch)
    story = []
    now = datetime.now()
    period = period_label or now.strftime("%B %Y")

    # ── HEADER ────────────────────────────────────────────────────────────────
    hdr = Table([[
        Paragraph(business_name or "Your Business",
            S("bn", font="Helvetica-Bold", fontSize=22, textColor=WHITE, leading=26)),
        Paragraph("ReceiptKept",
            S("rv", font="Helvetica-Bold", fontSize=10, textColor=WHITE, alignment=TA_RIGHT)),
    ]], colWidths=[W*0.75, W*0.25])
    hdr.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,-1),GREEN),
        ("TOPPADDING",(0,0),(-1,-1),16),("BOTTOMPADDING",(0,0),(-1,-1),16),
        ("LEFTPADDING",(0,0),(0,0),20),("RIGHTPADDING",(1,0),(1,0),20),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
    ]))
    story.append(hdr)
    story.append(Spacer(1,16))

    # ── PERIOD ────────────────────────────────────────────────────────────────
    story.append(Paragraph("EXPENSE REPORT",
        S("ey",font="Helvetica-Bold",fontSize=9,textColor=GREEN,spaceAfter=4)))
    story.append(Paragraph(period,
        S("ph",font="Helvetica-Bold",fontSize=20,textColor=BLACK,leading=24,spaceAfter=4)))
    story.append(Paragraph(
        f"Prepared {now.strftime('%B %d, %Y')}  ·  For {accountant_email}",
        S("ps",fontSize=10,textColor=GRAY,spaceAfter=12)))
    story.append(HRFlowable(width=W,thickness=1,color=BORDER,spaceAfter=14))

    # ── CATEGORY TOTALS ───────────────────────────────────────────────────────
    category_totals: Dict[str,float] = {}
    total_amount = 0.0
    for r in receipts:
        cat = r.get("category") or "Other"
        amt = r.get("amount")
        if amt is not None:
            try:
                v = float(amt); category_totals[cat]=category_totals.get(cat,0)+v; total_amount+=v
            except: pass

    story.append(Paragraph("SPENDING BY CATEGORY",
        S("sl",font="Helvetica-Bold",fontSize=9,textColor=GREEN,spaceAfter=6)))

    if category_totals:
        cat_rows = []
        for cat, amt in sorted(category_totals.items(), key=lambda x:-x[1]):
            pct = f"{amt/total_amount*100:.0f}%" if total_amount else "0%"
            cat_rows.append([
                Paragraph(cat, S("cn",fontSize=10,textColor=BLACK,leftIndent=6)),
                Paragraph(pct, S("cp",fontSize=9,textColor=GRAY,alignment=TA_RIGHT)),
                Paragraph(f"${amt:,.2f}", S("ca",font="Helvetica-Bold",fontSize=10,textColor=BLACK,alignment=TA_RIGHT)),
            ])
        ct = Table(cat_rows, colWidths=[W*0.62, W*0.14, W*0.24], rowHeights=30)
        cmds = [
            ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
            ("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),
            ("LINEBELOW",(0,0),(-1,-2),0.5,BORDER),("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ]
        for i in range(len(cat_rows)):
            if i%2==0: cmds.append(("BACKGROUND",(0,i),(-1,i),GRAY_LIGHT))
        ct.setStyle(TableStyle(cmds))
        story.append(ct)
    else:
        story.append(Paragraph("No categorized receipts.", S("nc",fontSize=10,textColor=GRAY)))

    story.append(Spacer(1,16))
    story.append(HRFlowable(width=W,thickness=1,color=BORDER,spaceAfter=0))

    # ── TOTAL BOX ─────────────────────────────────────────────────────────────
    total_box = Table([[
        Paragraph("TOTAL EXPENSES", S("tl",fontSize=9,textColor=GRAY,spaceAfter=4)),
        Paragraph("RECEIPTS", S("tl2",fontSize=9,textColor=GRAY,spaceAfter=4,alignment=TA_CENTER)),
        Paragraph("PERIOD", S("tl3",fontSize=9,textColor=GRAY,spaceAfter=4,alignment=TA_CENTER)),
    ],[
        Paragraph(f"${total_amount:,.2f}", S("ta",font="Helvetica-Bold",fontSize=28,textColor=GREEN,leading=32)),
        Paragraph(str(len(receipts)), S("ta2",font="Helvetica-Bold",fontSize=28,textColor=GREEN,leading=32,alignment=TA_CENTER)),
        Paragraph(period, S("ta3",font="Helvetica-Bold",fontSize=12,textColor=BLACK,leading=16,alignment=TA_CENTER)),
    ]], colWidths=[W*0.50, W*0.20, W*0.30])
    total_box.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,-1),GREEN_LIGHT),
        ("TOPPADDING",(0,0),(-1,-1),14),("BOTTOMPADDING",(0,0),(-1,-1),14),
        ("LEFTPADDING",(0,0),(0,-1),20),("RIGHTPADDING",(-1,0),(-1,-1),12),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("LINEABOVE",(0,0),(-1,0),3,GREEN),
    ]))
    story.append(total_box)
    story.append(Spacer(1,20))

    # ── RECEIPT INDEX ─────────────────────────────────────────────────────────
    story.append(Paragraph("RECEIPT INDEX",
        S("si",font="Helvetica-Bold",fontSize=9,textColor=GREEN,spaceAfter=6)))

    hrow = [
        Paragraph("#",   S("ih",font="Helvetica-Bold",fontSize=8,textColor=WHITE,alignment=TA_CENTER)),
        Paragraph("MERCHANT", S("ih2",font="Helvetica-Bold",fontSize=8,textColor=WHITE)),
        Paragraph("DATE",     S("ih3",font="Helvetica-Bold",fontSize=8,textColor=WHITE,alignment=TA_CENTER)),
        Paragraph("CATEGORY", S("ih4",font="Helvetica-Bold",fontSize=8,textColor=WHITE)),
        Paragraph("AMOUNT",   S("ih5",font="Helvetica-Bold",fontSize=8,textColor=WHITE,alignment=TA_RIGHT)),
    ]
    idx = [hrow]
    for i,r in enumerate(receipts,1):
        merchant = r.get("merchant") or r.get("originalName") or r.get("original_name") or "Unknown"
        date_raw = r.get("receiptDate") or r.get("receipt_date") or r.get("uploadedAt") or r.get("uploaded_at") or ""
        try: date_str = datetime.fromisoformat(str(date_raw)[:10]).strftime("%b %d, %Y")
        except: date_str = str(date_raw)[:10] if date_raw else "—"
        cat = r.get("category") or "Uncategorized"
        amt = r.get("amount")
        amt_str = f"${float(amt):,.2f}" if amt is not None else "—"
        bg = WHITE if i%2==1 else GRAY_LIGHT
        idx.append([
            Paragraph(str(i),   S(f"r{i}a",fontSize=9,textColor=GRAY,alignment=TA_CENTER)),
            Paragraph(str(merchant)[:30], S(f"r{i}b",fontSize=9,textColor=BLACK)),
            Paragraph(date_str, S(f"r{i}c",fontSize=9,textColor=GRAY,alignment=TA_CENTER)),
            Paragraph(cat,      S(f"r{i}d",fontSize=9,textColor=GRAY)),
            Paragraph(amt_str,  S(f"r{i}e",font="Helvetica-Bold",fontSize=9,textColor=BLACK,alignment=TA_RIGHT)),
        ])

    idx_table = Table(idx, colWidths=[W*0.06, W*0.30, W*0.18, W*0.26, W*0.20])
    idx_cmds = [
        ("BACKGROUND",(0,0),(-1,0),GREEN),
        ("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7),
        ("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),
        ("LINEBELOW",(0,0),(-1,-2),0.5,BORDER),("VALIGN",(0,0),(-1,-1),"MIDDLE"),
    ]
    for i in range(1,len(idx)):
        if i%2==0: idx_cmds.append(("BACKGROUND",(0,i),(-1,i),GRAY_LIGHT))
    idx_table.setStyle(TableStyle(idx_cmds))
    story.append(idx_table)

    # ── BANK & CARD CHARGES ───────────────────────────────────────────────────
    if bank_transactions:
        receipt_no = {str(r.get("id")): i for i, r in enumerate(receipts, 1)}
        story.append(Spacer(1, 20))
        story.append(Paragraph("BANK &amp; CARD CHARGES (PERSONAL CHARGES LEFT OUT)",
            S("sb",font="Helvetica-Bold",fontSize=9,textColor=GREEN,spaceAfter=6)))
        brow = [[
            Paragraph("DATE", S("bh1",font="Helvetica-Bold",fontSize=8,textColor=WHITE)),
            Paragraph("VENDOR", S("bh2",font="Helvetica-Bold",fontSize=8,textColor=WHITE)),
            Paragraph("CATEGORY / SCHEDULE C", S("bh3",font="Helvetica-Bold",fontSize=8,textColor=WHITE)),
            Paragraph("RECEIPT", S("bh4",font="Helvetica-Bold",fontSize=8,textColor=WHITE,alignment=TA_CENTER)),
            Paragraph("AMOUNT", S("bh5",font="Helvetica-Bold",fontSize=8,textColor=WHITE,alignment=TA_RIGHT)),
        ]]
        unmatched_total = 0.0
        for j, t in enumerate(sorted(bank_transactions, key=lambda x: str(x.get("posted_date"))), 1):
            amt = abs(float(t.get("amount") or 0))
            rid = t.get("matched_receipt_id")
            if rid and str(rid) in receipt_no:
                rec = f"Yes (#{receipt_no[str(rid)]})"
            elif rid:
                rec = "Yes"
            else:
                rec = "No"
            if not (rid and str(rid) in receipt_no):
                unmatched_total += amt
            try: d = datetime.fromisoformat(str(t.get("posted_date"))[:10]).strftime("%b %d, %Y")
            except: d = str(t.get("posted_date") or "")[:10]
            cat = t.get("category") or "Other"
            line = t.get("schedule_c_line")
            brow.append([
                Paragraph(d, S(f"b{j}a",fontSize=8,textColor=GRAY)),
                Paragraph(str(t.get("vendor") or t.get("description") or "")[:34], S(f"b{j}b",fontSize=8,textColor=BLACK)),
                Paragraph(f"{cat}{' · ' + line if line else ''}", S(f"b{j}c",fontSize=8,textColor=GRAY)),
                Paragraph(rec, S(f"b{j}d",fontSize=8,textColor=GRAY,alignment=TA_CENTER)),
                Paragraph(f"${amt:,.2f}", S(f"b{j}e",font="Helvetica-Bold",fontSize=8,textColor=BLACK,alignment=TA_RIGHT)),
            ])
        bt = Table(brow, colWidths=[W*0.16, W*0.30, W*0.26, W*0.12, W*0.16])
        bcmds = [
            ("BACKGROUND",(0,0),(-1,0),GREEN),
            ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),
            ("LEFTPADDING",(0,0),(-1,-1),5),("RIGHTPADDING",(0,0),(-1,-1),5),
            ("LINEBELOW",(0,0),(-1,-2),0.5,BORDER),("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ]
        for i in range(1, len(brow)):
            if i % 2 == 0: bcmds.append(("BACKGROUND",(0,i),(-1,i),GRAY_LIGHT))
        bt.setStyle(TableStyle(bcmds))
        story.append(bt)
        story.append(Spacer(1, 8))
        story.append(Paragraph(
            f"Charges not already listed as receipts above: <b>${unmatched_total:,.2f}</b>. "
            f"Total business spending this period: <b>${total_amount + unmatched_total:,.2f}</b>.",
            S("bs",fontSize=9,textColor=BLACK)))

    # ── FOOTER ────────────────────────────────────────────────────────────────
    story.append(Spacer(1,20))
    story.append(Paragraph(
        f"Generated by ReceiptKept  ·  {now.strftime('%B %d, %Y')}  ·  receiptkept.com",
        S("ft",fontSize=8,textColor=GRAY,alignment=TA_CENTER)))
    story.append(Spacer(1,4))
    story.append(Paragraph(
        "Original receipt images stored securely in ReceiptKept.",
        S("ft2",fontSize=8,textColor=GRAY,alignment=TA_CENTER)))

    doc.build(story)
    buf.seek(0)
    return buf.read()

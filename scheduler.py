"""
ReceiptVault Scheduled Sender
Runs every hour, checks all businesses for pending sends,
fires email+PDF+Excel if today matches their send_day / send_frequency.
"""

import asyncio
import base64 as b64lib
import os
from datetime import datetime, timezone

import httpx
from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://wzcuzyouymauokijaqjk.supabase.co")
SUPABASE_KEY = (
    os.getenv("SUPABASE_ANON_KEY")
    or os.getenv("SUPABASE_KEY")
    or os.getenv("SUPABASE_SERVICE_KEY")
)
EDGE_URL = f"{SUPABASE_URL}/functions/v1/send-receipts"
TELNYX_API_KEY = os.getenv("TELNYX_API_KEY", "")
TELNYX_FROM    = os.getenv("TELNYX_FROM_NUMBER", "+13156252025")


def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def should_send_today(business: dict) -> bool:
    """Return True if this business is due for a scheduled send today."""
    freq    = business.get("send_frequency", "monthly")
    day     = int(business.get("send_day") or 1)
    enabled = business.get("send_enabled", 1)

    if not enabled:
        return False

    today = datetime.now()

    if freq == "weekly":
        # send_day 0=Mon … 6=Sun (matches Python weekday())
        return today.weekday() == (day % 7)

    if freq == "monthly":
        return today.day == day

    if freq == "quarterly":
        # Send on send_day of Jan, Apr, Jul, Oct
        return today.month in (1, 4, 7, 10) and today.day == day

    if freq == "semi-annually":
        # Send on send_day of Jan and Jul
        return today.month in (1, 7) and today.day == day

    if freq == "annually":
        # Send on send_day of January each year
        return today.month == 1 and today.day == day

    return False


async def send_for_business(business: dict):
    """Generate PDF+Excel and fire the edge function for one business."""
    supabase = get_supabase()
    biz_id   = business["id"]
    biz_name = business.get("business_name", "")
    acct_email = business.get("accountant_email", "")
    owner_phone = business.get("owner_phone", "")   # optional — SMS notify

    # Fetch all unsent receipts
    rows = (
        supabase.table("receipts")
        .select("*")
        .eq("business_id", biz_id)
        .filter("sent_at", "is", "null")
        .execute()
    )
    if not rows.data:
        print(f"[scheduler] {biz_name}: no unsent receipts — skip")
        return

    # Convert rows to camelCase dicts the generators expect
    def to_receipt(row):
        return {
            "id":           row.get("id"),
            "businessId":   row.get("business_id"),
            "filePath":     row.get("file_path"),
            "originalName": row.get("original_name"),
            "merchant":     row.get("merchant"),
            "amount":       row.get("amount"),
            "receiptDate":  row.get("receipt_date"),
            "category":     row.get("category"),
            "notes":        row.get("notes"),
            "uploadedAt":   str(row.get("uploaded_at", "")),
            "sentAt":       None,
        }

    receipts = [to_receipt(r) for r in rows.data]

    # Mark as sent immediately
    sent_at = datetime.now(timezone.utc).isoformat()
    for r in rows.data:
        supabase.table("receipts").update({"sent_at": sent_at}).eq("id", r["id"]).execute()

    # Generate PDF + Excel
    try:
        from pdf_generator import generate_cover_pdf
        from excel_generator import build_excel

        month_str    = datetime.now().strftime("%Y-%m")
        period_label = datetime.now().strftime("%B %Y") + " Expenses"
        safe_name    = biz_name.replace(" ", "_")

        pdf_bytes = await generate_cover_pdf(
            business_name=biz_name,
            owner_name=business.get("owner_name", ""),
            accountant_email=acct_email,
            receipts=receipts,
            period_label=period_label,
        )
        excel_bytes = build_excel(
            business_name=biz_name,
            period_label=period_label,
            receipts=receipts,
            gross_income=0.0,
        )

        payload = {
            "businessId":     biz_id,
            "accountantEmail": acct_email,
            "businessName":   biz_name,
            "receipts":       receipts,
            "pdfBase64":      b64lib.b64encode(pdf_bytes).decode(),
            "pdfFilename":    f"ReceiptVault_{safe_name}_{month_str}.pdf",
            "excelBase64":    b64lib.b64encode(excel_bytes).decode(),
            "excelFilename":  f"ReceiptVault_{safe_name}_{month_str}.xlsx",
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(EDGE_URL, json=payload)
            resp.raise_for_status()

        print(f"[scheduler] {biz_name}: sent {len(receipts)} receipts ✓")

        # ── SMS notify owner ─────────────────────────────────────────────
        if owner_phone and TELNYX_API_KEY:
            await send_sms(
                to=owner_phone,
                body=(
                    f"ReceiptVault: {len(receipts)} receipt(s) sent to your accountant "
                    f"({acct_email}) for {period_label}. Total: "
                    f"${sum(float(r['amount'] or 0) for r in receipts):.2f}"
                ),
            )

    except Exception as e:
        print(f"[scheduler] {biz_name}: ERROR — {e}")
        # Roll back sent_at so it retries next run
        for r in rows.data:
            supabase.table("receipts").update({"sent_at": None}).eq("id", r["id"]).execute()


async def send_sms(to: str, body: str):
    """Send SMS via Telnyx."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                "https://api.telnyx.com/v2/messages",
                headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
                json={"from": TELNYX_FROM, "to": to, "text": body},
            )
    except Exception as e:
        print(f"[scheduler] SMS failed: {e}")


async def run_scheduled_sends():
    """Called every hour by the APScheduler job."""
    print(f"[scheduler] tick — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    try:
        supabase = get_supabase()
        bizzes = supabase.table("businesses").select("*").eq("send_enabled", 1).execute()
        for biz in (bizzes.data or []):
            if not biz.get("accountant_email"):
                continue
            if should_send_today(biz):
                await send_for_business(biz)
    except Exception as e:
        print(f"[scheduler] top-level error: {e}")

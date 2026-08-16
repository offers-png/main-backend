"""
ReceiptVault Trial Reminder Emails
Runs alongside the hourly accountant-send scheduler. Checks every business
still in trial (no paid plan) and fires the two churn-prevention emails:
  - "2 days left" once trial_ends_at is 2 calendar days away
  - "last day" once today is the trial's final day
Each email is guarded by its own *_sent_at flag so re-running the check
(hourly) never double-sends.
"""

import os
from datetime import datetime

import httpx
from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://wzcuzyouymauokijaqjk.supabase.co")
SUPABASE_KEY = (
    os.getenv("SUPABASE_ANON_KEY")
    or os.getenv("SUPABASE_KEY")
    or os.getenv("SUPABASE_SERVICE_KEY")
)
SEND_EMAIL_URL = f"{SUPABASE_URL}/functions/v1/send-invoice"
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://receipts.dealdily.com")


def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _parse_trial_end(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


async def _send_email(to: str, subject: str, html: str):
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            SEND_EMAIL_URL,
            headers={"apikey": SUPABASE_KEY, "Content-Type": "application/json"},
            json={"to": to, "from": "ReceiptVault", "subject": subject, "html": html},
        )
        resp.raise_for_status()


def _wrap(body_html: str) -> str:
    return f"""<div style="font-family:sans-serif;max-width:500px;margin:0 auto">
  <div style="background:#1a6b3a;padding:24px 32px;border-radius:12px 12px 0 0">
    <h1 style="color:white;margin:0;font-size:20px">ReceiptVault</h1>
  </div>
  <div style="background:#f2faf5;padding:24px 32px">
    {body_html}
    <p style="color:#9c9c96;font-size:12px;margin-top:24px">ReceiptVault &middot; receipts.dealdily.com</p>
  </div>
</div>"""


def _day5_email_html(owner_name: str) -> str:
    return _wrap(f"""
    <p>Hi {owner_name or 'there'},</p>
    <p><b>2 days left</b> on your ReceiptVault trial &mdash; your books are ready.</p>
    <p>Every receipt you've uploaded is organized, categorized, and ready to send to your accountant.
    Don't lose that when your trial ends.</p>
    <a href="{FRONTEND_URL}/pricing" style="display:inline-block;background:#1a6b3a;color:white;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;margin-top:8px">Keep My Books</a>
    """)


def _day7_email_html(owner_name: str) -> str:
    return _wrap(f"""
    <p>Hi {owner_name or 'there'},</p>
    <p><b>Today is the last day</b> of your ReceiptVault trial.</p>
    <p>Here's what happens to your data: your receipts, mileage logs, and invoices stay safely stored,
    but sending, OCR, and reports pause until you subscribe. Nothing is deleted.</p>
    <a href="{FRONTEND_URL}/pricing" style="display:inline-block;background:#1a6b3a;color:white;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;margin-top:8px">Subscribe Now</a>
    """)


async def check_trial_reminder_emails():
    """Called on the same hourly tick as the accountant-send scheduler.
    Looks at every business still in trial (plan is null, trial_ends_at
    set) and sends whichever reminder is due."""
    supabase = get_supabase()
    try:
        rows = (
            supabase.table("businesses")
            .select("*")
            .is_("plan", "null")
            .not_.is_("trial_ends_at", "null")
            .execute()
        )
    except Exception as e:
        print(f"[trial_emails] fetch error: {e}")
        return

    today = datetime.utcnow().date()

    for biz in (rows.data or []):
        email = biz.get("owner_email")
        if not email:
            continue
        trial_ends_at = biz.get("trial_ends_at")
        if not trial_ends_at:
            continue
        try:
            trial_end_date = _parse_trial_end(trial_ends_at).date()
        except ValueError:
            continue
        days_remaining = (trial_end_date - today).days

        # Fetch the owner's email from Supabase auth (businesses doesn't store it)
        owner_name = biz.get("owner_name", "")
        biz_id = biz["id"]

        if days_remaining == 2 and not biz.get("trial_day5_email_sent_at"):
            try:
                await _send_email(email, "2 days left — your books are ready", _day5_email_html(owner_name))
                supabase.table("businesses").update(
                    {"trial_day5_email_sent_at": datetime.utcnow().isoformat()}
                ).eq("id", biz_id).execute()
                print(f"[trial_emails] sent day5 to {email}")
            except Exception as e:
                print(f"[trial_emails] day5 send failed for {biz_id}: {e}")

        if days_remaining <= 0 and not biz.get("trial_day7_email_sent_at"):
            try:
                await _send_email(email, "Last day — here's what happens to your data", _day7_email_html(owner_name))
                supabase.table("businesses").update(
                    {"trial_day7_email_sent_at": datetime.utcnow().isoformat()}
                ).eq("id", biz_id).execute()
                print(f"[trial_emails] sent day7 to {email}")
            except Exception as e:
                print(f"[trial_emails] day7 send failed for {biz_id}: {e}")

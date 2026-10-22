# ReceiptKept v3.0 — Release notes

Branch `receiptkept-v3` in both repos (`offers-png/receiptvault` front end,
`offers-png/main-backend` API). Nothing has been merged to `main` or deployed.
Database changes are already applied to Supabase project `wzcuzyouymauokijaqjk`
(additive only, ReceiptKept tables only, RLS on).

## What shipped

### 1. Multiple stores per account
- One owner login can run several stores. A store picker sits in the top bar;
  the chosen store is remembered per user (browser + Supabase user metadata)
  and sent to the API as `X-Business-Id` on every request.
- The backend checks on every request that the user owns the store or is an
  active team member of it, and refuses (403) otherwise — it never silently
  falls back to another store. Old app builds that send no header get the
  user's first store, so existing accounts keep working with no action.
- "Add a store" (reuses the setup form, then the accountant step), an
  "All stores" money summary on the dashboard (2+ stores), a "Your stores"
  card in Settings with Archive/Restore (archived stores keep their data,
  are view-only and aren't billed).
- Closed cross-store gaps found on the way: inventory history/adjust and
  invoice line-item edits now check the store.

### 2. New plans (Free / Basic $5.99 / Growth $14.99 / Business $29.99)
- All limits live in one place: `routes/receiptvault/entitlements.py`
  (`PLAN_LIMITS`), served at `GET /api/billing/plans`. The app never
  hard-codes a limit.
- No trial. Every signup starts on Free; the trial banner, trial emails,
  post-trial paywall and lockout are gone. Free accounts only see friendly
  upgrade prompts at limits, e.g. "You've used your 10 free scans this month.
  Upgrade to Basic for $5.99 to keep scanning."
- Monthly counters (scans, statement uploads) are counted from existing rows
  and reset on the 1st in the owner's time zone.
- Billing is per owner account (new `rv_accounts` table). Extra stores are a
  second subscription item at the plan's add-on price ($4.99 / $9.99 / $14.99).
- Plans page (`/plans`) with usage meters; Checkout for new subscriptions,
  in-place plan changes (with proration, add-on moved to the new plan's
  price), Stripe portal for card/bills/cancel.
- Webhook handles `checkout.session.completed`, `customer.subscription.created/
  updated/deleted`, `invoice.paid`, `invoice.payment_failed` (signature and
  timestamp verified).
- Downgrades never delete data: stores the plan doesn't pay for and paid-only
  features (invoices, inventory, team, bank sorting) become view-only.
- Referral reward is now a free month on a paid plan (applied at once if
  paying, or automatically at their first Checkout).
- Landing pricing, `paywall.tsx` (now the Plans page), Settings billing,
  Terms, `llms.txt` and JSON-LD updated.

### 3. Bank transactions core
- New tables `rv_bank_accounts`, `rv_bank_transactions`, `rv_vendor_rules`.
  Duplicates blocked by a unique index on business + dedupe key (date, amount,
  normalized description, source account; Stripe transaction id for bank
  feeds). Two genuinely identical charges on the same day are both kept.
- ONE category list with Schedule C lines (`categories.py`, `GET /api/categories`)
  replaces the two different lists; existing receipts were migrated to it.
- Auto-sort: the store's vendor rules first, then the AI. Changing a category
  or tapping Personal saves a rule and re-sorts that vendor's other charges.
- Receipt matching: same amount (±$0.01), within 5 days, vendor name preferred;
  runs when charges arrive and when a receipt is scanned; manual pick/unlink.
- Bank & Cards page with "12 charges need a receipt", one-tap "Scan receipt
  for this charge", Personal, "Already counted", plus a dashboard nudge.
- Personal charges are left out of the P&L, quarterly tax estimate, accountant
  PDF and CSV export. A receipt matched to a charge is counted once.
- Money coming IN from the bank is not counted as income unless the owner taps
  "Count as income" — sales are already in Money Box and counting both would
  double income and the tax estimate. (Decision made during the build.)
- The accountant PDF now has a "Bank & card charges" section that points each
  charge at its receipt number.

### 4. Bank statement upload (CSV + PDF)
- Preview first, nothing saved until "Looks right — save"; re-uploading the
  same file adds nothing. Basic: 2 uploads/month (counted when saved).
- CSV auto-detects columns and layouts; tested on Chase checking + card, Bank
  of America (summary block on top), Wells Fargo (no header), Citi, Capital
  One, Amex/Discover (purchases shown as positive — auto-flipped), KeyBank,
  M&T and a credit union. Unknown layouts get a "which column is the date /
  amount / description?" picker. A "flip signs" switch covers odd exports.
- PDF statements are read by the existing AI provider.

### 5. Bank connection (Stripe Financial Connections) — Growth/Business
- Sessions request the `transactions` permission only (read-only; no
  payments, balances or account numbers), checking + savings + credit card.
- Linked accounts are subscribed to daily refreshes; the
  `financial_connections.account.refreshed_transactions` webhook imports only
  new/changed transactions (`transaction_refresh[after]`); pending ones wait
  until posted, voided ones are ignored, replays add nothing.
- Settings → Connected banks: accounts, last sync time, Sync, Disconnect
  (unsubscribe + disconnect; data kept), Reconnect (reuses the account, skips
  transactions already saved). Growth 2 accounts, Business unlimited.
- "Your bank can't connect yet — upload your statement instead" fallback.

### 6. Email receipt forwarding — Growth/Business
- Chosen: **Resend Inbound** (available on Resend; no extra service).
- Each store gets `r-<token>@in.receiptkept.com` (Settings, with Copy and a
  "get a new address" button). Only known tokens are accepted; signed
  webhooks only; 30/hour and 200/day per store; anything over 20 MB ignored;
  each email processed once.
- PDF/image attachments become receipts (AI fills merchant, amount, date,
  category) and are matched to bank charges. With no attachment, the email
  is saved as a PDF (if PDFShift is configured) or as plain text, and read by
  the AI. The AI can now read PDF receipts too (before, PDFs were skipped).

### 7. CSV export (all plans)
- Settings → Download expenses: receipts + Money Box money out + bank charges
  for a date range with Date, Vendor, Amount, Category, Schedule C line,
  Payment method, Has receipt (Y/N), Receipt link, Notes. Per store or all
  stores; personal left out by default. Spreadsheet formula injection guarded.

### Export my data / Delete my account (Settings → Your Data)
- `GET /api/account/export`: a zip with every store the owner has (one
  folder each: everything as JSON, a CSV per kind of record, and the expenses
  spreadsheet), the billing account and team memberships. Team members get
  only the entries they added themselves. Secret tokens are left out.
- `DELETE /api/account` (needs `{"confirm": "DELETE"}`; the dialog asks you to
  type DELETE): cancels the Stripe subscription first (if that fails, nothing
  is deleted), disconnects connected banks, deletes every owned store with all
  its records and uploaded files, removes team members and the billing
  account, and takes the person off any teams they're on. Other people's
  stores are never touched.
- The shared Supabase **login is deleted too only if the person has no data
  in any other app** in this Supabase project. The database function
  `rv_user_other_app_data()` (backend-only) looks for their user id in every
  non-ReceiptKept table, their email in email columns, and files they own in
  other storage buckets. If it finds anything, or the check fails, only the
  ReceiptKept data is deleted and the login stays.
- **Saleh's account can never be deleted this way**: user
  `83055487-…` and the emails saleh852@gmail.com / offers@dealdily.com are
  built into the code (by id and by email, any letter case). The env vars
  `RK_PROTECTED_EMAILS` / `RK_PROTECTED_USER_IDS` can add more, never remove.
- Privacy policy updated to say exactly this (version 2026-10-22).

### 8. Verification
- Backend: 60 automated tests (in-memory database fake; no network) covering
  store isolation, entitlements/limits, billing flow and webhooks, dedupe,
  vendor rules, matching, personal exclusion, statement parsing for 10 bank
  layouts, bank connection, email forwarding, CSV export, and one end-to-end
  regression: signup → Free → scan → Money Box → upgrade → add store →
  upload statement → connect bank → match receipts → mark personal →
  accountant PDF → CSV export → downgrade. Run: `python -m pytest tests`.
- Front end: `npm run check` (type check) and `npm run build` pass. Every
  main screen was rendered at 375px wide: no horizontal scroll anywhere; on
  touch screens every tap target is at least 44px (global rule in
  `index.css`). The front end has no lint setup, so no lint ran.
- Supabase advisors: no new warnings on ReceiptKept tables. Also fixed the
  existing per-row `auth.uid()` RLS warnings on the older ReceiptKept tables
  and added two missing `referrals` indexes. "Index not used yet" notes on
  the new tables are expected until they have data.
- No secrets in the front-end bundle or current files. See "Must do" below
  about an old key in git history.

## Must do before going live (owner)

1. **Rotate the Supabase service-role key if it hasn't been done since July.**
   An old service-role key is still in the git history of `main-backend`
   (a **public** repo) and of `receiptvault`, though it's not in any current
   file. Anyone with it has full access to the shared database. Rotate it in
   Supabase (Settings → API / JWT keys) and update `SUPABASE_SERVICE_KEY` on
   Render.
2. **Stripe (test mode first):**
   - `STRIPE_SECRET_KEY=sk_test_... python scripts/stripe_setup_receiptkept.py`
     (backend repo). It creates the 3 plans + 3 extra-store prices and the
     `referral-1-month-free` coupon, and prints the price env vars.
   - Add a webhook endpoint `https://main-backend-k32m.onrender.com/api/billing/webhook`
     with events: `checkout.session.completed`, `customer.subscription.created`,
     `customer.subscription.updated`, `customer.subscription.deleted`,
     `invoice.paid`, `invoice.payment_failed`,
     `financial_connections.account.refreshed_transactions`.
   - Turn on the customer portal (Settings → Billing → Customer portal) with
     cancel and card update allowed.
   - Test with card `4242 4242 4242 4242`: Free → Basic → Growth → Business →
     add a store → cancel in the portal.
   - For live: run the script again with the live key, put the live price IDs
     in Render, and **complete Financial Connections registration** in the
     Stripe dashboard (required before live bank linking works).
3. **Email forwarding DNS:** in Resend, add receiving domain
   `in.receiptkept.com` and create the MX record Resend shows for it (only the
   `in.` subdomain — your normal email is untouched). Add a Resend webhook for
   `email.received` → `https://main-backend-k32m.onrender.com/api/inbound/resend`.
4. **Render env vars (backend):**
   - `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`
   - `STRIPE_WEBHOOK_SECRET_RECEIPTKEPT` (the webhook now rejects all events
     until this is set; the old `STRIPE_WEBHOOK_SECRET_ReceiptVault` name
     still works)
   - `STRIPE_PRICE_BASIC`, `STRIPE_PRICE_GROWTH`, `STRIPE_PRICE_BUSINESS`,
     `STRIPE_PRICE_STORE_BASIC`, `STRIPE_PRICE_STORE_GROWTH`, `STRIPE_PRICE_STORE_BUSINESS`
   - `RESEND_API_KEY`, `RESEND_INBOUND_WEBHOOK_SECRET`, optional
     `INBOUND_EMAIL_DOMAIN` (default `in.receiptkept.com`)
   - optional `PDFSHIFT_API_KEY` (forwarded emails with no attachment become PDFs)
   - optional `RK_PROTECTED_EMAILS` / `RK_PROTECTED_USER_IDS` (extra accounts that can never be deleted)
   - `FRONTEND_URL=https://receiptkept.com` (several emails still default to
     the old receipts.dealdily.com when this isn't set)
   - existing: `SUPABASE_SERVICE_KEY`, `ANTHROPIC_API_KEY`, `EMAIL_RELAY_SECRET`, `TELNYX_API_KEY`
5. **Netlify:** no new env vars. `_headers` now allows Stripe.js (bank
   connection) and uses `Cross-Origin-Opener-Policy: same-origin-allow-popups`
   for banks that sign in through a popup.
6. Merge `receiptkept-v3` into `main` in both repos (backend first) once the
   Stripe test-mode run passes.

## Didn't work as expected / not verified

- **Nothing ran against real Stripe, Resend or the AI** — no keys were
  available during the build. Billing, Financial Connections, inbound email
  and PDF-statement reading were built to the documented APIs and tested
  against fakes. Do the Stripe test-mode run above before launch.
- **Credit cards in Financial Connections:** credit card accounts are
  requested; whether a given bank returns card transactions couldn't be
  tested without a Stripe test key. Check with Stripe's test institution.
  If a card doesn't sync, statement upload covers it.
- **Bank connection inside the Android app** wasn't tested on a device.
  Stripe's bank login can open a popup/redirect that a WebView may not hand
  back; if it fails, the app falls back to "upload your statement instead".
- **Money Box vs bank charges:** a vendor paid by card and ALSO typed into
  Money Box will show twice. Owners can tap "Already counted" on the bank
  charge. Automatic matching of Money Box entries wasn't in scope.
- **Other-app check limits:** it finds a person by user id (uuid columns and
  text columns named like user/owner/member/author/created_by/uid), by email
  columns, and by files they own. An app that stores people only by phone
  number or inside JSON fields wouldn't be detected, so a login used only
  there could be deleted. All apps checked today store users by id or email.
- Deleting the login needs the backend to run with the service-role key (it
  does on Render). Without it the login is simply kept.
- The legacy Express `server/` folder in the front-end repo is unused (the app
  talks to the FastAPI backend); only its category list was updated.

-- ReceiptKept Android app: push notification phones + per-type switches.
-- Additive only. ReceiptKept tables only. RLS on: a signed-in user can see
-- and change only their own rows; the backend uses the service key.

create table if not exists public.rv_device_tokens (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  store_id character varying references public.businesses(id) on delete set null,
  fcm_token text not null unique,
  platform text not null default 'android',
  created_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);
create index if not exists rv_device_tokens_user_idx on public.rv_device_tokens(user_id);
create index if not exists rv_device_tokens_store_idx on public.rv_device_tokens(store_id);
alter table public.rv_device_tokens enable row level security;
drop policy if exists rv_device_tokens_own on public.rv_device_tokens;
create policy rv_device_tokens_own on public.rv_device_tokens
  for all using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));

create table if not exists public.rv_notification_prefs (
  user_id uuid primary key references auth.users(id) on delete cascade,
  weekly_missing_receipts boolean not null default true,
  receipt_saved boolean not null default true,
  bank_relogin boolean not null default true,
  accountant_sent boolean not null default true,
  last_weekly_push_at timestamptz,
  updated_at timestamptz not null default now()
);
alter table public.rv_notification_prefs enable row level security;
drop policy if exists rv_notification_prefs_own on public.rv_notification_prefs;
create policy rv_notification_prefs_own on public.rv_notification_prefs
  for all using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));

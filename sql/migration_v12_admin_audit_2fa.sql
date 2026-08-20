-- TrendPlus: admin security hardening (Task 3)
--
-- Adds, on top of migration_v5_admin_approval.sql:
--   1. An immutable-ish audit trail of every mutating admin action.
--   2. A role on admin accounts ('support' | 'superadmin') so the most
--      destructive actions can be gated to superadmins only.
--   3. Optional TOTP-based 2FA columns on admin accounts.
--
-- Safe to run once (idempotent — uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).

-- ── Admin roles ───────────────────────────────────────────────────────
-- 'support'    — can view/approve/reject/enable/disable users
-- 'superadmin' — everything above + delete users, reset passwords, run engine
alter table admins
    add column if not exists role text not null default 'support'
        check (role in ('support', 'superadmin'));

-- ── Optional TOTP 2FA on admin accounts ───────────────────────────────
-- totp_secret is the base32 shared secret (nullable until enrolled).
-- totp_enabled flips true only after the admin verifies a first code, so a
-- half-finished enrollment never locks anyone out.
alter table admins add column if not exists totp_secret  text;
alter table admins add column if not exists totp_enabled boolean not null default false;

-- Existing single admin, if any, should be a superadmin so the panel keeps working.
-- (No-op when the table is empty / freshly seeded.)
update admins set role = 'superadmin' where role = 'support'
  and id = (select min(id) from admins);

-- ── Admin audit log ───────────────────────────────────────────────────
-- One row per mutating admin action. admin_id is nullable because engine
-- runs can be triggered by the shared X-Admin-Secret (no admin identity),
-- in which case actor_label records how it was authenticated.
create table if not exists admin_audit_log (
    id            bigserial primary key,
    admin_id      bigint      references admins(id) on delete set null,
    actor_label   text        not null,               -- username, or 'x-admin-secret'
    action        text        not null,               -- e.g. 'user.delete', 'engine.backfill'
    target_type   text,                               -- 'user' | 'pending_user' | 'engine' | ...
    target_id     text,                               -- id of the affected object (as text; users are uuid)
    metadata      jsonb       not null default '{}'::jsonb,
    ip_address    text,
    created_at    timestamptz not null default now()
);

create index if not exists idx_admin_audit_created  on admin_audit_log (created_at desc);
create index if not exists idx_admin_audit_admin    on admin_audit_log (admin_id, created_at desc);
create index if not exists idx_admin_audit_action   on admin_audit_log (action, created_at desc);

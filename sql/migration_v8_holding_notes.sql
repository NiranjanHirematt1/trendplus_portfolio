-- ═══════════════════════════════════════════════════════════════════════
-- TrendPlus v8 — Per-holding notes (shown in the unified Detail Panel)
-- ═══════════════════════════════════════════════════════════════════════
-- Additive only; safe to run once against a v7 database.

alter table holdings add column if not exists notes text;

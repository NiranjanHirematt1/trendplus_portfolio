-- ═══════════════════════════════════════════════════════════════════
--  TrendPulse — Migration v11
--  Adds multi-horizon period returns to trend_results:
--    chg_3d  (3 sessions)   chg_1m (21)   chg_2m (42)
--    chg_3m  (63)           chg_6m (126)  chg_12m (252)
--  Run in Supabase SQL Editor.
--  SAFE / idempotent: ADD COLUMN IF NOT EXISTS — won't touch existing data.
--
--  NOTE: existing trend_results rows keep NULL for these columns. Only a
--  recompute fills them:
--    • today only            → python scripts/compute_today.py --recompute
--    • all historical dates  → python scripts/compute_all_dates.py
-- ═══════════════════════════════════════════════════════════════════

begin;

-- ── Period returns (trading sessions), numeric(8,2) like chg_1d/5d/12d ──
ALTER TABLE trend_results
  ADD COLUMN IF NOT EXISTS chg_3d   numeric(8,2),   -- 3  trading-session return
  ADD COLUMN IF NOT EXISTS chg_1m   numeric(8,2),   -- 21 sessions (~1 month)
  ADD COLUMN IF NOT EXISTS chg_2m   numeric(8,2),   -- 42 sessions (~2 months)
  ADD COLUMN IF NOT EXISTS chg_3m   numeric(8,2),   -- 63 sessions (~3 months)
  ADD COLUMN IF NOT EXISTS chg_6m   numeric(8,2),   -- 126 sessions (~6 months)
  ADD COLUMN IF NOT EXISTS chg_12m  numeric(8,2);   -- 252 sessions (~12 months)

commit;

-- ── Verify ───────────────────────────────────────────────────────────
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'trend_results'
  AND column_name IN ('chg_3d','chg_1m','chg_2m','chg_3m','chg_6m','chg_12m')
ORDER BY column_name;

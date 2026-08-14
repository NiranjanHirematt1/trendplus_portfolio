-- ═══════════════════════════════════════════════════════════════════════
-- TrendPlus v10 — Cup & Handle engine (screener-wide, precomputed)
-- ═══════════════════════════════════════════════════════════════════════
-- Replaces the earlier per-user "Cup & Handle Watch" (v9). Additive apart
-- from dropping the now-unused watch tables; safe to run once on a v8/v9 DB.
--
-- The new engine scans EVERY active symbol after each bhav load and stores a
-- single latest pattern per (symbol, timeframe) here. The screener and the
-- stock detail panel both read straight from this table — no per-request
-- computation, so the whole ~2,000-name universe stays cheap to serve.
--
--   cup_handle_pattern : one row per (symbol, timeframe). `stage` is the
--                        current lifecycle position (cup_forming / handle_-
--                        forming / breakout / confirmed). The quantified
--                        columns back the screener table and the detail
--                        panel; `meta` carries the full detector payload
--                        (rim prices, roundness, symmetry, …) for the UI.
-- ───────────────────────────────────────────────────────────────────────

begin;

-- Retire the v9 per-user watch feature (replaced by the universe-wide scan).
drop table if exists cup_handle_signal;
drop table if exists cup_handle_watch;

create table if not exists cup_handle_pattern (
    symbol            text        not null references symbols(symbol),
    timeframe         text        not null check (timeframe in ('daily', 'weekly')),
    stage             text        not null check (stage in
                                    ('cup_forming', 'handle_forming', 'breakout', 'confirmed')),
    resistance        numeric,
    cup_depth_pct     numeric,
    cup_duration      integer,
    handle_depth_pct  numeric,
    handle_duration   integer,
    breakout          boolean     not null default false,
    volume_ratio      numeric,
    pattern_score     numeric,
    last_close        numeric,
    meta              jsonb       not null default '{}',
    computed_at       timestamptz not null default now(),
    primary key (symbol, timeframe)
);

-- Screener reads: filter by timeframe + stage, order by score.
create index if not exists idx_cup_handle_pattern_tf_stage_score
    on cup_handle_pattern (timeframe, stage, pattern_score desc);

create index if not exists idx_cup_handle_pattern_tf_score
    on cup_handle_pattern (timeframe, pattern_score desc);

commit;

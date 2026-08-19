-- ═══════════════════════════════════════════════════════════════════
--  TrendPulse  —  Supabase PostgreSQL Schema  v2.0
--  Run this ONCE in Supabase SQL Editor → New query → Run
--  Safe to re-run (all statements are idempotent)
-- ═══════════════════════════════════════════════════════════════════

-- ── EXTENSIONS ──────────────────────────────────────────────────────
create extension if not exists "pg_trgm";    -- trigram fuzzy search
create extension if not exists "btree_gin";  -- GIN on numeric types

-- ════════════════════════════════════════════════════════════════════
--  TABLE 1 : symbols
--  Master reference — one row per NSE EQ symbol
--  Source: EQUITY_L.csv + nse_sector_master.csv
-- ════════════════════════════════════════════════════════════════════
create table if not exists symbols (
    symbol          text        primary key,
    company_name    text        not null default '',
    isin            text        not null default '',
    sector          text        not null default '',
    cap_category    text        not null default ''  -- 'Large Cap'|'Mid Cap'|'Small Cap'
                    check (cap_category in ('Large Cap','Mid Cap','Small Cap','')),
    is_active       boolean     not null default true,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

-- Indexes on symbols
create index if not exists idx_sym_sector   on symbols (sector)          where is_active;
create index if not exists idx_sym_cap      on symbols (cap_category)    where is_active;
create index if not exists idx_sym_isin     on symbols (isin)            where isin != '';
create index if not exists idx_sym_name_gin on symbols using gin (company_name gin_trgm_ops);
create index if not exists idx_sym_sym_gin  on symbols using gin (symbol  gin_trgm_ops);


-- ════════════════════════════════════════════════════════════════════
--  TABLE 2 : trend_results
--  Core analytics table — one row per (symbol, trade_date)
--  Written daily by the engine after market close
-- ════════════════════════════════════════════════════════════════════
create table if not exists trend_results (
    id              bigserial   primary key,
    trade_date      date        not null,
    symbol          text        not null
                    references symbols (symbol) on delete cascade,

    -- ── 12-day momentum matrix ──────────────────────────────────────
    trending_days   smallint    check (trending_days between 0 and 12),
    -- JSONB maps: {"2026-02-25": true/false, ...} newest→oldest
    bool_matrix     jsonb       not null default '{}',
    -- JSONB maps: {"2026-02-25": 1.23, ...} daily % change per day
    pct_matrix      jsonb       not null default '{}',

    -- ── Period returns (all in TRADING SESSIONS) ────────────────────
    chg_1d          numeric(8,2),   -- today vs yesterday
    chg_3d          numeric(8,2),   -- 3  trading day return
    chg_5d          numeric(8,2),   -- 5d label / 6-session legacy offset
    chg_12d         numeric(8,2),   -- 12 trading day return
    chg_1m          numeric(8,2),   -- 21 sessions  (~1 month)
    chg_2m          numeric(8,2),   -- 42 sessions  (~2 months)
    chg_3m          numeric(8,2),   -- 63 sessions  (~3 months)
    chg_6m          numeric(8,2),   -- 126 sessions (~6 months)
    chg_12m         numeric(8,2),   -- 252 sessions (~12 months)

    -- ── 52-Week High ────────────────────────────────────────────────
    high_52w        numeric(12,2),  -- highest close in rolling 252 days
    pct_from_high   numeric(8,2),   -- (close - 52wh) / 52wh * 100  (≤ 0)
    near_52w_high   boolean,        -- true if pct_from_high >= -5%
    rank_52w        numeric(5,1),   -- percentile rank 0-100 (100 = AT high)

    -- ── Technical indicators ────────────────────────────────────────
    rsi_14          numeric(6,2)    check (rsi_14 between 0 and 100),
    adx_14          numeric(6,2)    check (adx_14 >= 0),

    -- ── Relative Strength & Composite ──────────────────────────────
    rs_score        numeric(5,1)    check (rs_score between 1 and 99),
    momentum_score  numeric(5,1)    check (momentum_score between 0 and 100),

    -- ── OHLC snapshot for the day ───────────────────────────────────
    open_price      numeric(12,2),
    high_price      numeric(12,2),
    low_price       numeric(12,2),
    close_price     numeric(12,2),
    volume          bigint,
    total_trades    integer,

    created_at      timestamptz not null default now(),

    constraint uq_trend_date_symbol unique (trade_date, symbol)
);

-- Partial index: only latest 500 days for screener queries (keeps index small)
create index if not exists idx_tr_date_momentum
    on trend_results (trade_date desc, momentum_score desc nulls last)
    where momentum_score is not null;

create index if not exists idx_tr_date_rs
    on trend_results (trade_date desc, rs_score desc nulls last)
    where rs_score is not null;

create index if not exists idx_tr_date_trending
    on trend_results (trade_date desc, trending_days desc nulls last);

create index if not exists idx_tr_date_near_high
    on trend_results (trade_date desc, rank_52w desc nulls last)
    where near_52w_high = true;

create index if not exists idx_tr_symbol_date
    on trend_results (symbol, trade_date desc);

create index if not exists idx_tr_date_only
    on trend_results (trade_date desc);


-- ════════════════════════════════════════════════════════════════════
--  TABLE 3 : sector_daily
--  Pre-aggregated sector summary per trading day
--  Written by engine alongside trend_results
-- ════════════════════════════════════════════════════════════════════
create table if not exists sector_daily (
    id              bigserial   primary key,
    trade_date      date        not null,
    sector          text        not null,

    -- ── Breadth ─────────────────────────────────────────────────────
    stock_count     smallint    not null default 0,
    stocks_up       smallint    not null default 0,
    stocks_down     smallint    not null default 0,
    pct_stocks_up   numeric(5,1),

    -- ── Averages ────────────────────────────────────────────────────
    avg_trending_days   numeric(5,1),
    avg_chg_12d         numeric(8,2),
    avg_chg_5d          numeric(8,2),
    avg_chg_today       numeric(8,2),
    avg_rsi_14          numeric(6,2),
    avg_adx_14          numeric(6,2),
    avg_rs_score        numeric(5,1),
    avg_momentum        numeric(5,1),

    -- ── 52W High breadth ────────────────────────────────────────────
    stocks_near_high    smallint not null default 0,
    pct_near_high       numeric(5,1),

    created_at      timestamptz not null default now(),

    constraint uq_sector_date unique (trade_date, sector)
);

create index if not exists idx_sd_date_momentum
    on sector_daily (trade_date desc, avg_momentum desc nulls last);

create index if not exists idx_sd_date_trending
    on sector_daily (trade_date desc, avg_trending_days desc nulls last);

create index if not exists idx_sd_sector
    on sector_daily (sector, trade_date desc);


-- ════════════════════════════════════════════════════════════════════
--  TABLE 4 : price_history
--  Full OHLCV per (symbol, date) — used for charting
--  Populated from bhav files; kept separate from trend_results
--  so charts work even for dates outside the 12-day matrix window
-- ════════════════════════════════════════════════════════════════════
create table if not exists price_history (
    id              bigserial   primary key,
    symbol          text        not null
                    references symbols (symbol) on delete cascade,
    trade_date      date        not null,
    open_price      numeric(12,2),
    high_price      numeric(12,2),
    low_price       numeric(12,2),
    close_price     numeric(12,2),
    volume          bigint,
    total_trades    integer,
    prev_close      numeric(12,2),
    constraint uq_price_symbol_date unique (symbol, trade_date)
);

create index if not exists idx_ph_symbol_date
    on price_history (symbol, trade_date desc);

create index if not exists idx_ph_date
    on price_history (trade_date desc);


-- ════════════════════════════════════════════════════════════════════
--  TABLE 5 : market_calendar
--  One row per calendar date — tracks trading days and engine runs
-- ════════════════════════════════════════════════════════════════════
create table if not exists market_calendar (
    trade_date      date        primary key,
    is_trading_day  boolean     not null default true,
    holiday_name    text,                               -- 'Holi', 'Diwali', etc
    bhav_downloaded boolean     not null default false,
    engine_status   text        not null default 'pending'
                    check (engine_status in ('pending','running','done','error','skipped')),
    symbol_count    integer,
    engine_duration_secs numeric(8,2),
    error_message   text,
    processed_at    timestamptz,
    created_at      timestamptz not null default now()
);

create index if not exists idx_cal_status
    on market_calendar (engine_status, trade_date desc);


-- ════════════════════════════════════════════════════════════════════
--  TABLE 6 : engine_runs
--  Audit log of every engine execution
-- ════════════════════════════════════════════════════════════════════
create table if not exists engine_runs (
    id                  bigserial   primary key,
    run_date            date        not null,
    trigger             text        not null default 'scheduled'  -- 'scheduled'|'manual'|'backfill'
                        check (trigger in ('scheduled','manual','backfill')),
    started_at          timestamptz not null default now(),
    finished_at         timestamptz,
    status              text        not null default 'running'
                        check (status in ('running','success','error')),
    symbols_processed   integer,
    bhav_files_loaded   integer,
    duration_secs       numeric(8,2),
    error_message       text,
    notes               text
);

create index if not exists idx_er_date
    on engine_runs (run_date desc, started_at desc);


-- ════════════════════════════════════════════════════════════════════
--  VIEWS
-- ════════════════════════════════════════════════════════════════════

-- Latest completed trading date
create or replace view v_latest_date as
select max(trade_date) as trade_date
from market_calendar
where engine_status = 'done';


-- Full leaderboard for today — used by /api/trend with no date param
create or replace view v_leaderboard_today as
select
    s.symbol,
    s.company_name,
    s.sector,
    s.cap_category,
    s.isin,
    tr.trade_date,
    tr.trending_days,
    tr.chg_1d,
    tr.chg_5d,
    tr.chg_12d,
    tr.high_52w,
    tr.pct_from_high,
    tr.near_52w_high,
    tr.rank_52w,
    tr.rsi_14,
    tr.adx_14,
    tr.rs_score,
    tr.momentum_score,
    tr.open_price,
    tr.high_price,
    tr.low_price,
    tr.close_price,
    tr.volume,
    tr.bool_matrix,
    tr.pct_matrix
from symbols s
join trend_results tr on tr.symbol = s.symbol
where tr.trade_date = (select trade_date from v_latest_date)
  and s.is_active = true;


-- Sector summary for today
create or replace view v_sector_today as
select *
from sector_daily
where trade_date = (select trade_date from v_latest_date)
order by avg_momentum desc nulls last;


-- Symbol trend history (last 90 days) — used by /api/symbol/{sym}
create or replace view v_symbol_history as
select
    tr.symbol,
    tr.trade_date,
    tr.trending_days,
    tr.chg_1d,
    tr.chg_5d,
    tr.chg_12d,
    tr.rsi_14,
    tr.adx_14,
    tr.rs_score,
    tr.momentum_score,
    tr.pct_from_high,
    tr.near_52w_high,
    tr.high_52w,
    tr.close_price
from trend_results tr
where tr.trade_date >= (current_date - interval '90 days')
order by tr.symbol, tr.trade_date desc;


-- ════════════════════════════════════════════════════════════════════
--  FUNCTIONS
-- ════════════════════════════════════════════════════════════════════

-- auto-update symbols.updated_at on change
create or replace function fn_symbols_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_symbols_updated_at on symbols;
create trigger trg_symbols_updated_at
    before update on symbols
    for each row execute function fn_symbols_updated_at();


-- Bulk-upsert helper called from Python engine
-- Accepts a JSONB array of trend records, does a single INSERT ... ON CONFLICT
create or replace function fn_upsert_trend_batch(p_rows jsonb)
returns int language plpgsql as $$
declare
    v_count int := 0;
    v_row   jsonb;
begin
    for v_row in select * from jsonb_array_elements(p_rows) loop
        insert into trend_results (
            trade_date, symbol,
            trending_days, bool_matrix, pct_matrix,
            chg_1d, chg_5d, chg_12d,
            high_52w, pct_from_high, near_52w_high, rank_52w,
            rsi_14, adx_14, rs_score, momentum_score,
            open_price, high_price, low_price, close_price,
            volume, total_trades
        ) values (
            (v_row->>'trade_date')::date,
            v_row->>'symbol',
            (v_row->>'trending_days')::smallint,
            coalesce(v_row->'bool_matrix', '{}'),
            coalesce(v_row->'pct_matrix',  '{}'),
            (v_row->>'chg_1d')::numeric,
            (v_row->>'chg_5d')::numeric,
            (v_row->>'chg_12d')::numeric,
            (v_row->>'high_52w')::numeric,
            (v_row->>'pct_from_high')::numeric,
            (v_row->>'near_52w_high')::boolean,
            (v_row->>'rank_52w')::numeric,
            (v_row->>'rsi_14')::numeric,
            (v_row->>'adx_14')::numeric,
            (v_row->>'rs_score')::numeric,
            (v_row->>'momentum_score')::numeric,
            (v_row->>'open_price')::numeric,
            (v_row->>'high_price')::numeric,
            (v_row->>'low_price')::numeric,
            (v_row->>'close_price')::numeric,
            (v_row->>'volume')::bigint,
            (v_row->>'total_trades')::integer
        )
        on conflict (trade_date, symbol) do update set
            trending_days  = excluded.trending_days,
            bool_matrix    = excluded.bool_matrix,
            pct_matrix     = excluded.pct_matrix,
            chg_1d         = excluded.chg_1d,
            chg_5d         = excluded.chg_5d,
            chg_12d        = excluded.chg_12d,
            high_52w       = excluded.high_52w,
            pct_from_high  = excluded.pct_from_high,
            near_52w_high  = excluded.near_52w_high,
            rank_52w       = excluded.rank_52w,
            rsi_14         = excluded.rsi_14,
            adx_14         = excluded.adx_14,
            rs_score       = excluded.rs_score,
            momentum_score = excluded.momentum_score,
            open_price     = excluded.open_price,
            high_price     = excluded.high_price,
            low_price      = excluded.low_price,
            close_price    = excluded.close_price,
            volume         = excluded.volume,
            total_trades   = excluded.total_trades;
        v_count := v_count + 1;
    end loop;
    return v_count;
end;
$$;


-- Get the top N stocks for a given date — called by screener API
create or replace function fn_screener(
    p_date          date        default null,
    p_min_trending  int         default 0,
    p_min_rsi       numeric     default 0,
    p_min_adx       numeric     default 0,
    p_min_chg_12d   numeric     default -999,
    p_min_momentum  numeric     default 0,
    p_sector        text        default null,
    p_cap           text        default null,
    p_near_high     boolean     default null,
    p_sort_col      text        default 'momentum_score',
    p_sort_dir      text        default 'desc',
    p_limit         int         default 50,
    p_offset        int         default 0
)
returns table (
    symbol          text,
    company_name    text,
    sector          text,
    cap_category    text,
    trending_days   smallint,
    chg_1d          numeric,
    chg_5d          numeric,
    chg_12d         numeric,
    high_52w        numeric,
    pct_from_high   numeric,
    near_52w_high   boolean,
    rank_52w        numeric,
    rsi_14          numeric,
    adx_14          numeric,
    rs_score        numeric,
    momentum_score  numeric,
    close_price     numeric,
    bool_matrix     jsonb,
    pct_matrix      jsonb,
    total_count     bigint
) language plpgsql as $$
declare
    v_date date;
begin
    -- resolve date
    v_date := coalesce(p_date, (select trade_date from v_latest_date));
    if v_date is null then return; end if;

    return query execute format(
        'select
            s.symbol, s.company_name, s.sector, s.cap_category,
            tr.trending_days, tr.chg_1d, tr.chg_5d, tr.chg_12d,
            tr.high_52w, tr.pct_from_high, tr.near_52w_high, tr.rank_52w,
            tr.rsi_14, tr.adx_14, tr.rs_score, tr.momentum_score,
            tr.close_price, tr.bool_matrix, tr.pct_matrix,
            count(*) over() as total_count
         from trend_results tr
         join symbols s on s.symbol = tr.symbol
         where tr.trade_date = $1
           and s.is_active = true
           and ($2 = 0      or tr.trending_days  >= $2)
           and ($3 = 0      or tr.rsi_14         >= $3)
           and ($4 = 0      or tr.adx_14         >= $4)
           and ($5 = -999   or tr.chg_12d        >= $5)
           and ($6 = 0      or tr.momentum_score >= $6)
           and ($7 is null  or s.sector          =  $7)
           and ($8 is null  or s.cap_category    =  $8)
           and ($9 is null  or tr.near_52w_high  =  $9)
         order by tr.%I %s nulls last
         limit $10 offset $11',
        p_sort_col, p_sort_dir
    )
    using v_date,
          p_min_trending, p_min_rsi, p_min_adx, p_min_chg_12d, p_min_momentum,
          p_sector, p_cap, p_near_high,
          p_limit, p_offset;
end;
$$;


-- ════════════════════════════════════════════════════════════════════
--  ROW LEVEL SECURITY  (enable when you add auth)
--  Uncomment these when you add user authentication
-- ════════════════════════════════════════════════════════════════════
-- alter table symbols       enable row level security;
-- alter table trend_results enable row level security;
-- alter table sector_daily  enable row level security;
-- alter table price_history enable row level security;

-- Public read-only policies (no auth required — suitable for a public app)
-- create policy "public_read_symbols"       on symbols       for select using (true);
-- create policy "public_read_trend_results" on trend_results for select using (true);
-- create policy "public_read_sector_daily"  on sector_daily  for select using (true);
-- create policy "public_read_price_history" on price_history for select using (true);

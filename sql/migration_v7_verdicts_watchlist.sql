-- ═══════════════════════════════════════════════════════════════════════
-- TrendPlus v7 — Verdict History + Watchlist
-- ═══════════════════════════════════════════════════════════════════════
-- Additive only; safe to run against a v6 database. Run once.
--
--   holding_verdicts : one row per (holding, trading day) recording the
--                      deterministic verdict engine's call, its confidence
--                      and the exact rules that fired. This is what powers
--                      "this holding has been a TRIM for 9 sessions" and
--                      lets verdicts be audited after the fact.
--
--   watchlist_items  : server-side watchlist per user (previously nothing —
--                      the screener had no persistent watchlist at all).
-- ───────────────────────────────────────────────────────────────────────

begin;

create table if not exists holding_verdicts (
    id              bigserial primary key,
    holding_id      bigint not null references holdings(id) on delete cascade,
    portfolio_id    bigint not null references portfolios(id) on delete cascade,
    symbol          text   not null,
    trade_date      date   not null,
    verdict         text   not null check (verdict in ('ADD_MORE','HOLD','TRIM','EXIT')),
    confidence      smallint check (confidence between 0 and 100),
    position_score  numeric(5,1),
    gain_pct        numeric(10,2),
    reasons         jsonb  not null default '[]',
    created_at      timestamptz not null default now(),
    unique (holding_id, trade_date)
);

create index if not exists idx_holding_verdicts_holding
    on holding_verdicts (holding_id, trade_date desc);

create index if not exists idx_holding_verdicts_portfolio
    on holding_verdicts (portfolio_id, trade_date desc);

create table if not exists watchlist_items (
    id          bigserial primary key,
    user_id     uuid not null references users(id) on delete cascade,
    symbol      text not null references symbols(symbol),
    note        text,
    created_at  timestamptz not null default now(),
    unique (user_id, symbol)
);

create index if not exists idx_watchlist_user
    on watchlist_items (user_id, created_at desc);

commit;

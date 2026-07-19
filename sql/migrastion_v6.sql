-- ═══════════════════════════════════════════════════════════════════════
-- TrendPlus Portfolio v6 — Partial Sell, Transaction Ledger, Soft Delete,
-- Portfolio Performance History
-- ═══════════════════════════════════════════════════════════════════════
-- Safe to run against an existing v4/v5 database. Every statement is
-- additive (add column / create table if not exists) except the two
-- `alter ... drop constraint / add constraint` blocks, which replace the
-- old ACTIVE/SOLD-only status check with one that also allows PARTIAL and
-- ARCHIVED, and relax the "sold rows must carry sell_date/sell_price"
-- rule so it only applies to fully-exited holdings.
--
-- Run once. Back up first if in doubt.
-- ───────────────────────────────────────────────────────────────────────

begin;

-- ── 1. holdings: new columns needed for partial sell / soft delete ────
alter table holdings add column if not exists total_bought_quantity numeric(18,4);
alter table holdings add column if not exists realized_pnl          numeric(16,2) not null default 0;
alter table holdings add column if not exists is_archived           boolean not null default false;
alter table holdings add column if not exists archived_at           timestamptz;
alter table holdings add column if not exists user_id               uuid references users(id) on delete cascade;

-- Backfill total_bought_quantity for existing rows: the current quantity
-- is the best information we have pre-migration (no prior partial sells
-- were possible under the old single-shot "Mark Sold" flow).
update holdings set total_bought_quantity = quantity where total_bought_quantity is null;
alter table holdings alter column total_bought_quantity set not null;

-- ── 2. status: allow PARTIAL alongside ACTIVE / SOLD / ARCHIVED ───────
-- ACTIVE   = fully held, nothing sold yet
-- PARTIAL  = some quantity sold, remainder still held at the original
--            average buy price ("Partial Exit")
-- SOLD     = remaining quantity is zero ("Fully Exited")
-- ARCHIVED = soft-deleted, excluded from all portfolio views/analytics
alter table holdings drop constraint if exists holdings_status_check;
alter table holdings
    add constraint holdings_status_check
    check (status in ('ACTIVE', 'PARTIAL', 'SOLD', 'ARCHIVED'));

-- A fully-exited holding must carry its final sell details; partial exits
-- and still-open positions do not need them at the holdings-row level —
-- that detail now lives per-transaction in holding_transactions.
alter table holdings drop constraint if exists chk_sold_details;
alter table holdings
    add constraint chk_sold_details
    check (status <> 'SOLD' or (sell_date is not null and sell_price is not null));

-- Only one *open* position (ACTIVE or PARTIAL) per symbol per portfolio.
-- A fully-exited or archived row must not block re-entering the same
-- stock later (handled by reusing the row via the /buy endpoint when one
-- already exists, but this index protects against races/direct inserts).
drop index if exists uq_holdings_active_symbol;
create unique index if not exists uq_holdings_open_symbol
    on holdings (portfolio_id, symbol)
    where status in ('ACTIVE', 'PARTIAL') and not is_archived;

-- Soft-deleted rows should not show up in normal listings.
create index if not exists idx_holdings_portfolio_open
    on holdings (portfolio_id, status)
    where not is_archived;

-- ── 3. holding_transactions: append-only buy/sell ledger ───────────────
-- One row per purchase or sale. This is the source of truth for:
--   * weighted-average cost recomputation on each additional buy
--   * realized P/L per sale
--   * portfolio performance history reconstruction (Part 9)
create table if not exists holding_transactions (
    id              bigserial primary key,
    holding_id      bigint not null references holdings(id) on delete cascade,
    portfolio_id    bigint not null references portfolios(id) on delete cascade,
    symbol          text not null,
    txn_type        text not null check (txn_type in ('BUY', 'SELL')),
    quantity        numeric(18,4) not null check (quantity > 0),
    price           numeric(14,4) not null check (price > 0),
    txn_date        date not null,
    charges         numeric(14,2) not null default 0 check (charges >= 0),
    realized_pnl    numeric(16,2),           -- only populated for SELL rows
    notes           text,
    created_at      timestamptz not null default now()
);

create index if not exists idx_holding_txn_holding on holding_transactions (holding_id, txn_date);
create index if not exists idx_holding_txn_portfolio on holding_transactions (portfolio_id, txn_date);
create index if not exists idx_holding_txn_symbol on holding_transactions (symbol, txn_date);

-- Backfill: give every pre-existing holding its originating BUY row (and,
-- for already-SOLD holdings, the SELL row that closed it) so the ledger
-- is complete going forward and performance-history reconstruction has
-- data to work with immediately.
insert into holding_transactions (holding_id, portfolio_id, symbol, txn_type, quantity, price, txn_date, charges, realized_pnl, notes)
select h.id, h.portfolio_id, h.symbol, 'BUY', h.total_bought_quantity, h.avg_buy_price,
       coalesce(h.buy_date, h.created_at::date), 0, null, 'Backfilled from pre-v6 holding row'
from holdings h
where not exists (select 1 from holding_transactions t where t.holding_id = h.id and t.txn_type = 'BUY');

insert into holding_transactions (holding_id, portfolio_id, symbol, txn_type, quantity, price, txn_date, charges, realized_pnl, notes)
select h.id, h.portfolio_id, h.symbol, 'SELL', h.total_bought_quantity, h.sell_price,
       coalesce(h.sell_date, h.updated_at::date), 0,
       h.total_bought_quantity * (h.sell_price - h.avg_buy_price),
       'Backfilled from pre-v6 holding row'
from holdings h
where h.status = 'SOLD'
  and not exists (select 1 from holding_transactions t where t.holding_id = h.id and t.txn_type = 'SELL');

-- Backfill cumulative realized_pnl on the holdings row from the ledger.
update holdings h
set realized_pnl = coalesce((
    select sum(t.realized_pnl) from holding_transactions t
    where t.holding_id = h.id and t.txn_type = 'SELL'
), 0)
where h.realized_pnl = 0;

-- ── 4. portfolio_snapshots: daily portfolio value history ─────────────
-- Populated once per trading day by the scheduler after the market-data
-- engine run (see scheduler.run_daily_pipeline). Performance-history reads
-- prefer this table; if it's sparse for the requested range the API
-- transparently reconstructs from holding_transactions + price_history
-- and backfills this table so subsequent reads are fast.
create table if not exists portfolio_snapshots (
    id                  bigserial primary key,
    portfolio_id        bigint not null references portfolios(id) on delete cascade,
    snapshot_date       date not null,
    total_investment    numeric(18,2) not null default 0,
    current_value       numeric(18,2) not null default 0,
    unrealized_pnl      numeric(18,2) not null default 0,
    realized_pnl        numeric(18,2) not null default 0,
    holdings_count      integer not null default 0,
    created_at          timestamptz not null default now(),
    unique (portfolio_id, snapshot_date)
);

create index if not exists idx_portfolio_snapshots_lookup
    on portfolio_snapshots (portfolio_id, snapshot_date desc);

commit;
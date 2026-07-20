# TrendPlus Portfolio — Phase 1 Redesign

Treat the Portfolio as an independent product: a professional trader's command
center. This document is the design contract for the rebuild of
`frontend/portfolio.html` and its backend support.

---

## 1. UX Audit of the current Portfolio

**What works** (kept): auth flow, broker import, buy/sell/delete modals with
previews, the transaction ledger, verdict engine data, performance history
reconstruction.

**What fails against Kite/TradingView/Bloomberg standards:**

1. **No information hierarchy.** The page is a vertical stack of ~12
   equal-weight "panels" (brief → strip → cards → chart → stop watch →
   verdicts → AI → rotation → opportunities → table → form → history →
   analytics). The single most important object — the holdings table — is
   buried below six panels, roughly 4 screen-heights down.
2. **Generic card grid.** Eight identical rounded cards with one number each
   is the canonical AI-dashboard look. A trader reads 12 KPIs in one
   eye-track across a ticker strip; cards force a Z-scan over 2 rows.
3. **Wasted vertical space.** `padding:18–24px` everywhere, 12px radius,
   panel headers with subtitles for self-evident content. Kite fits 30
   holdings on one screen; this fits ~8.
4. **Critical risk info is polite, not loud.** Trailing-stop breaches render
   as a mid-page panel you must scroll to. Alerts must be the first thing
   after the KPIs, severity-coded, impossible to miss.
5. **Duplicate detail surfaces.** The Screener has a rich right-side Detail
   Panel; the Portfolio has a separate "Position Intelligence" modal with
   different layout, different chart, no technicals matrix. Two codepaths,
   two UXs for the same object (a stock).
6. **Table is rigid.** Fixed columns, server-round-trip single-sort, no
   search, no column control, no sparklines, cramped Symbol column with an
   emoji prefix (`📈`), actions consume a wide column.
7. **Navigation-heavy flows.** Reviewing one weak stock = scroll to table →
   open modal → close → scroll to verdicts → scroll to rotation. Everything
   about one symbol must be in one surface (the Detail Panel).
8. **Dead/demo UI.** Morning-brief "Good Morning." greeting block, tax
   estimate (backend returns `null`), decorative gradients.

---

## 2. Data feasibility (no fake data — hard scoping)

| Requested widget | Status | Source |
|---|---|---|
| Value, Today/Overall/Unrealized/Realized P&L, Return %, XIRR, Holdings count, Win rate, Best/Worst | ✅ build | existing `/summary` + `/holdings` |
| Sector / Industry / Holdings allocation | ✅ build | `symbols.sector` (raw = industry; SECTOR_MAP group = sector) |
| Portfolio heatmap | ✅ build | weight × today's change |
| Performance curve | ✅ build | `/performance-history` |
| Daily P&L history | ✅ build | derived client-side from history series (Δvalue − Δinvested) |
| Transaction history (account level) | ✅ build | new `GET /api/portfolio/transactions` |
| Sparklines (12D/1M/6M/1Y) | ✅ build | new `GET /api/portfolio/sparklines` from `price_history` |
| Alerts: consecutive ±5% sessions, drawdown 10/15/20%, volume spike, break below 20/50/200 EMA, RSI breakdown, concentration, user risk limit | ✅ build | new alert engine over `trend_results` + `price_history` |
| AI Portfolio Review | ✅ keep | existing Gemini advisor (optional) + deterministic verdicts |
| Risk score / concentration analysis | ✅ build | existing health + HHI |
| **Cash balance** | ⛔ defer | no funds ledger exists; needs deposit/withdrawal entity |
| **Dividends, corporate actions** | ⛔ defer | no corporate-actions ingestion (NSE CA feed needed) |
| **Upcoming earnings, news** | ⛔ defer | no announcements/news source |
| **Delivery spike, institutional selling, promoter pledge, rating downgrade, block/bulk deals, earnings miss** | ⛔ defer | not derivable from bhavcopy; needs new ingestion jobs |

Deferred items get **no placeholder UI** — an empty widget faking data is
worse than absence. They are listed in §8 as the Phase 1.5 data roadmap.

---

## 3. New Information Architecture

Priority order = what a trader asks, in order:
1. *Am I up or down right now?* → *Command Bar* (one strip, always visible)
2. *Is anything on fire?* → *Alerts rail* (directly under, severity-coded)
3. *My positions?* → *Holdings table* (the centerpiece, above the fold)
4. *Where is my money / how is it shaped?* → Allocation + Heatmap (side rail)
5. *How did I get here?* → Equity curve + daily P&L + transactions
6. *What should I do?* → Verdicts / AI review / rotation (decision support)

## 4. Wireframe

```
┌────────────────────────────────────────────────────────────────────────────┐
│ topbar  TrendPlus▸Portfolio      [search /]              acct ▾  logout    │
├────────────────────────────────────────────────────────────────────────────┤
│ COMMAND BAR (one row, mono, hairline-separated cells, sticky)              │
│ VALUE 12,45,300 │ TODAY +8,420 +0.68%▲ │ OVERALL +1.2L │ UNRLZD │ RLZD │  │
│ RET% │ XIRR │ POS 14 │ WIN 64% │ BEST SWSOLAR +42% │ WORST IDEA −18%      │
├────────────────────────────────────────────────────────────────────────────┤
│ ⚠ ALERTS (3 critical · 2 warning)  ─ severity left-rail rows, collapsible │
├──────────────────────────────────────────────┬─────────────────────────────┤
│ HOLDINGS  [search…] [12D 1M 6M 1Y] [⚙ cols] │  ALLOCATION                 │
│ ┌─ sticky head + sticky sym col ───────────┐ │  [Sector|Industry|Holding]  │
│ │ SYMBOL COMPANY QTY AVG LTP 1D P&L RET%   │ │  bar list w/ % + value      │
│ │ ALLOC% SECTOR TREND(spark) RISK VERDICT  │ │ ─────────────────────────── │
│ │ … rows, resize/reorder/pin/multi-sort …  │ │  HEATMAP                    │
│ └──────────────────────────────────────────┘ │  tiles ∝ weight, color=1D   │
├──────────────────────────────────────────────┴─────────────────────────────┤
│ EQUITY CURVE [1M 3M 6M 1Y ALL]        │ DAILY P&L (bars, green/red)        │
├───────────────────────────────────────┴────────────────────────────────────┤
│ DECISION SUPPORT: Verdicts & reasons │ AI second opinion │ Rotation        │
├────────────────────────────────────────────────────────────────────────────┤
│ TRANSACTIONS (account ledger)  │  EXITED POSITIONS  │  ADD / IMPORT        │
└────────────────────────────────────────────────────────────────────────────┘
   Detail Panel (right slide-over, SAME component as Screener):
   quote header ★ · [12D 1M 6M 1Y] price chart · technicals · matrix · peers
   + IF OWNED: position strip (qty/avg/value/1D/overall/ret%/alloc%),
     verdict + reasons, transactions, notes
```

## 5. Component hierarchy

```
portfolio.html
├─ Topbar (auth state, logout)
├─ AuthGate (unchanged flows, reskinned copy)
└─ Dashboard
   ├─ CommandBar          ← renderCommandBar(summary)
   ├─ AlertsRail          ← renderAlerts(GET /alerts)   [collapsible, count chips]
   ├─ HoldingsTable       ← TPTable (generic, column model in localStorage)
   │   ├─ Toolbar (instant search · spark timeframe · column menu · density)
   │   ├─ Header (multi-sort, drag-reorder, drag-resize, pin, sticky)
   │   ├─ Body (sticky symbol col, sparkline cells, windowed >200 rows)
   │   └─ RowActions (Buy / Sell / … menu)
   ├─ SideRail
   │   ├─ AllocationCard (tabs: Sector | Industry | Holdings)
   │   └─ Heatmap (tiles ∝ allocation, color = today %)
   ├─ ChartsRow (EquityCurve + DailyPnlBars — both from /performance-history)
   ├─ DecisionRow (Verdicts | AI review | Rotation)
   ├─ LedgerRow (Transactions | Exited positions)
   ├─ AddImport (manual add + broker file import)
   └─ Modals (Buy, Sell, Delete)  [kept]
shared.js  (used by BOTH pages)
├─ TP_SECTOR_MAP + normSector
└─ TPDP — unified Detail Panel (self-injecting markup+CSS)
    ├─ init({api, getToken, headerExtra, ownedProvider})
    ├─ open(symbol) → /api/symbol/{sym} + ownedProvider(symbol)
    └─ timeframe selector 12D/1M/6M/1Y for the price chart
```

## 6. Design system changes

Tokens (both pages share the dark identity; portfolio tightens it):
- **Density**: base font 13px→12.5px in data surfaces; row height 34px
  (compact 28px); panel padding 24px→12/16px; radius 12px→6px on data
  panels. Numbers: `JetBrains Mono` with `font-variant-numeric: tabular-nums`.
- **Color semantics**: gain `#10b981`, loss `#ef4444`, warn `#f59e0b`,
  critical alerts carry a 3px left rail + tinted bg — color is *data*, so
  chrome stays neutral (`#0b0e13` bg / `#12161d` panel / hairline `#232936`).
- **Signature element**: the Command Bar — a single unbroken exchange-style
  data strip: small uppercase labels over large tabular-mono values,
  hairline-separated, today's P&L cell tinted by sign. No cards anywhere.
- **Type roles**: Inter (500/600) for labels & prose only; all figures mono;
  section titles = 11px uppercase tracked eyebrows, not 15px headings.
- **Motion**: 120ms ease on hover states, one 240ms slide for the Detail
  Panel; no entrance animations, `prefers-reduced-motion` respected.

## 7. User workflows (click-counted)

- **Morning check**: load → Command Bar (0 clicks) → Alerts visible (0) →
  scan table sorted by Today's P&L (1 click, remembered).
- **Investigate a holding**: click row → Detail Panel with position overlay,
  verdict + reasons, chart, technicals, ledger, notes (1 click). Buy/Sell
  from panel or row (2 clicks total).
- **Act on an alert**: alert row names symbol + rule → click opens Detail
  Panel (1 click).
- **Rebalance**: Allocation card → click sector → table filters to it (1).

## 8. Implementation plan

1. **Backend** — `portfolio_alerts.py` (pure rules + fetchers), endpoints:
   `GET /alerts?risk_limit=`, `GET /sparklines?range=`, `GET /transactions`.
   Migration v8: `holdings.notes`. Unit tests for every alert rule.
2. **shared.js** — extract Screener Detail Panel into `TPDP` (self-injecting),
   add timeframe selector + owned-position overlay; index.html consumes it.
3. **portfolio.html rebuild** — new layout per wireframe; TPTable with
   column model (order/width/pin/hidden in localStorage), multi-sort
   (shift-click), instant client-side search, sparklines, heatmap,
   allocation tabs, daily P&L bars. Keep auth/import/modals.
4. **Validation** — pytest, import check, JS parse check, manual flows.

**Phase 1.5 (data roadmap, required before the deferred widgets):**
cash/funds ledger entity; NSE corporate-actions + dividends ingestion;
earnings calendar; delivery % (sec_bhavdata_full); bulk/block deals feed;
news source. Each lands as its own table + scheduler job, then its widget.

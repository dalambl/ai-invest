# Project overview

A personal investment platform: pulls positions and trades from Interactive
Brokers TWS, stores daily snapshots in SQLite + CSV, and serves a browser
dashboard (FastAPI + plain HTML/JS) with holdings, P&L across horizons,
performance vs benchmark, exposure, and risk metrics.

## Current status

**2026-04-22 (late pm)** — AIR follow-up: discovered the prior
"override `market_value` to USD" approach corrupted the snapshot
(today's `market_value` column got USD instead of EUR, and the next
sync re-derived stock/fx from that). Real fix: `_enrich_position`
in `ibkr.py` now always uses `qty × price` locally (was reading
IB's `marketValue` which is in account base USD for portfolio
items); `enrich_positions_from_db` no longer mutates local fields
and instead derives `market_value_usd` fresh from `mv_local ×
fx_rate` plus exposes `unrealized_pnl_usd` / `total_return_usd`;
frontend reads `unrealized_pnl_usd` in the Unrealized column. AIR:
+$252 USD (was "-$1,683 USD"). All foreign holdings still
reconcile (Stock + FX = Unreal USD).

**2026-04-22 (pm)** — Three follow-up fixes to multi-currency P&L
(see `notes/006`): (a) `build_daily_positions` now keeps local and
USD cost bases separate (was double-counting FX by adding `tx.net`
USD into a local-currency accumulator); (b) pre-existing-position
seed switched from day-1 market price to IB-avg-derived seed using
average-cost-method invariance (fixed RHM cost-basis underseed of
~$4000); (c) `enrich_positions_from_db` now overrides IB's
local-currency `unrealized_pnl`/`market_value` with USD equivalents
for foreign positions so the dashboard's Stock + FX = Unreal across
all rows. All foreign holdings (RHM/MC/AIR/CSU/ORSTED) now
reconcile to the cent. 54 tests still pass.

**2026-04-22** — Multi-currency P&L decomposition shipped end-to-end
plus Sharpe-by-frequency on the dashboard. New `fx.py` pulls daily FX
from Yahoo (`<CCY>USD=X`); snapshot rows now carry `currency`,
`fx_rate`, `market_value_usd`, `cost_basis_usd`, `stock_pnl_usd`,
`fx_pnl_usd` (idempotent `ALTER TABLE` migration in `db.py`).
`rebuild_history.build_daily_positions` tracks cost basis in both
local and historical-USD; sells realize in USD at sale-day FX; per-row
the snapshot decomposes `stock_pnl_usd = (mv − cost_local) × fx_t` and
`fx_pnl_usd = total_pnl_usd − stock_pnl_usd`. Aggregation in
`finance.py` now prefers the `*_usd` fields so cross-position sums are
genuinely in USD. Added `finance.sharpe_by_frequency` returning daily
/ weekly / monthly Sharpes (resampled by ISO year-week and YYYY-MM);
`/api/risk` now accepts `risk_free_rate` (default 0.045) and returns
`sharpe_by_frequency`. UI: holdings table grew Mkt Value (USD) +
Stock P&L + FX P&L columns; risk page grew a Sharpe-by-frequency
table. New `notes/005-industry-mapping-plan.md` lays out a layered
resolver design (override CSV → GICS-from-Wikipedia → Yahoo → SEC SIC
→ ETF expansion) for many-to-many industry tagging.

After running `AIINVEST_API=http://localhost:8765 uv run python
rebuild_history.py` (port-8765 because VS Code squats :8000): 833
snapshot dates, latest 2026-04-22 — Mkt Value $290,227.83, Stock P&L
$80,671.35, FX P&L $826.12, Dividends $8,837.47, Total Return
$90,334.94. Year-end 2025 FX P/L still exact at +$134.59. Full check
pipeline green (ruff/ty/pytest) at **54 passed**. No commits made.

## Plan
- Implement industry mapping per `005-industry-mapping-plan.md` (start
  with Yahoo + manual-overrides layer, defer ETF expansion).
- Read and document the frontend (`static/`) in its own notes file.
- Design pass for cash-balance tracking in snapshots.
- Restart the running uvicorn so `/api/risk` serves the new
  `sharpe_by_frequency` field (current process loaded pre-change code).

## Notes index

| # | Topic | Summary |
|---|-------|---------|
| [001](001-initial-repo-survey.md) | Initial repo survey | Module-by-module map of backend; data layout; key design decisions (realized-P&L pseudo-row, price fallback chain, daily-linked returns, gated snapshot saves); mismatches between `plan.md` / `CLAUDE.md` / code. |
| [002](002-verify-and-fix.md) | Verify and fix | Audit findings (monthly-grid bugs, exposure alias, asyncio deprecation, multi-currency, tooling drift), fixes applied, new `finance.py` + 32 tests, full check pipeline green (ruff/pytest/ty), `python-multipart` added, `ibkr.py` ty errors cleared. |
| [003](003-fix-options-cost-fx-trades.md) | Options, cost basis, FX, trades | Compared pipeline output to real IB Activity Statement; fixed: options keep full contract symbol with multiplier=100, cost basis commission-inclusive, new `ib_statement.py` parser + `__FX__` pseudo-row inject FX P/L, trade history shows option contracts. Schema migration: `sec_type` + `multiplier` columns. Rebuild executed: phantom OPT/FUT seed-positions filtered out, `data/U640574_2025_2025.csv` location standardized, year-end unrealized within $830 of IB, FX P/L exact. 19 new tests; 51 passing. |
| [004](004-cost-basis-monthly-sharpe.md) | Cost-basis seeding, monthly tab, Sharpe Rf | Eliminated +24.71% 2023-09-12 phantom spike by pre-seeding cost basis for pre-existing positions at day-1 market price (two-pass build in `rebuild_history.main`, new `init_costs` arg on `build_daily_positions`); `generate_snapshots` no longer back-patches. `/api/returns/monthly` now defaults to earliest snapshot so yearly rows are calendar years. `finance.risk_metrics` accepts `risk_free_rate`. Post-rebuild yearlies: 2023 +17.7%, 2024 +9.9%, 2025 +28.7%, 2026 YTD +7.9%; Sharpe 0.91 at Rf=4.5%. 51 tests still passing. |
| [006](006-multicurrency-pnl-fixes.md) | Multi-currency P&L fixes | Three follow-up fixes after the initial multi-currency split: cost-basis local/USD separation in `build_daily_positions`, pre-existing-position seed derived from IB avg cost (average-cost invariance) instead of day-1 market price, and `/api/positions` exposes `unrealized_pnl_usd` so Stock + FX = Unreal in the dashboard. Then a second pass: `_enrich_position` (ibkr.py) now always uses local `qty × price` instead of IB's USD-base `marketValue`; `enrich_positions_from_db` derives `market_value_usd` fresh from `mv_local × fx_rate` (was reusing stale snapshot value); frontend uses `unrealized_pnl_usd` in the Unrealized column. AIR went from "-$1683 USD" (corrupted) to correct +$252 USD (small EUR loss + EUR-strengthening FX gain). All foreign holdings reconcile; 54 tests pass. |
| [005](005-industry-mapping-plan.md) | Industry-mapping plan | Design for many-to-many stock→industry tagging: layered resolver (manual overrides → GICS-from-Wikipedia → Yahoo `info` → SEC SIC → ETF holdings expansion) with provenance, `industries`/`symbol_industries` SQLite tables (with weights for ETF expansion), and `/api/exposure/industry` endpoint. Open questions on canonical sector vs multi-tag, override CSV location, ETF expansion vs single thematic tag. Plan only — not implemented. |

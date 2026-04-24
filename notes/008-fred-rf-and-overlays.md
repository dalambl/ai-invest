# FRED risk-free rate + generic FRED overlay + per-stock price modal

Three related dashboard upgrades.

## 1. Time-varying risk-free rate (FRED DGS3MO)

Sharpe used a hard-coded 4.5% Rf, even though the actual T-bill yield
moved between ~3.6% and ~5.6% over the snapshot window
(2023-01-31 → 2026-04-23, 835 daily snapshots).

- New `risk_free.py` returns DGS3MO as `{date: annualized_decimal}`,
  forward-filled across weekends/holidays.
- `finance.risk_metrics` and `finance.sharpe_by_frequency` now accept
  Rf as either a scalar (back-compat) or a `{date: rate}` mapping; an
  internal `_resolve_rf` helper materializes the per-observation Rf
  (as-of-or-before lookup at each return date).
- `/api/risk` defaults to the time-varying series (override via
  `?risk_free_rate=0.045`) and returns metadata:
  `risk_free_rate: {source, kind, min, max, mean, first_date, last_date}`.

Result over full history: Sharpe daily / weekly / monthly = 0.86 /
0.89 / 1.04, Rf range 3.62% – 5.63% (mean 4.78%). Very close to the
prior 0.91 with constant 4.5% — confirms the prior constant was a
fair-ish proxy for the period mean, but the time-varying series is
correct for any horizon where Rf moved.

## 2. Generic FRED overlay on the P&L chart

- New `fred.py` — generic `fetch_series(series_id)` with disk cache at
  `data/fred_<series_id>.csv`, refreshed at most once per day. No API
  key required (uses public `fredgraph.csv?id=...`).
- `risk_free.py` refactored to delegate to `fred.fetch_series("DGS3MO")`
  and own only the percent→decimal conversion + forward-fill policy.
- New `GET /api/fred/{series_id}?from=&to=` returns `{series_id, dates,
  values}` in raw FRED units.
- Performance tab: free-text input + "+ Overlay" button above the
  Cumulative P&L chart. Each overlay renders as a dotted line on its
  own secondary y-axis (raw units; e.g. percent for DGS3MO/DGS10,
  level for VIXCLS). Removable chips. Active overlays persist in
  `localStorage` and auto-refetch when the perf horizon changes.

## 3. Click stock → price modal

- New modal in `index.html` opened by clicking a symbol in the
  Holdings table. Period pills (1M / 3M / 6M / 1Y / 3Y / 5Y).
- `/api/market/history/{symbol}` now falls back to Yahoo
  (`yfinance`, already a dep) when TWS is offline or returns empty.
  Yahoo ticker resolution reuses `YAHOO_TICKER_MAP` so foreign
  tickers (RHM.DE / MC.PA / ORSTED.CO / CSU.TO / PSKY.L) work.
- Modal renders a Plotly candlestick chart with:
  - dashed horizontal reference line at the position's avg cost,
  - up-triangle markers at the bottom for any dividend dates from
    `/api/dividends?symbol=...` in the visible range.
- Esc / backdrop-click closes the modal.

## Tests

`tests/test_finance.py`: 2 new tests confirming a constant
`{date: rate}` Rf produces the same Sharpe as the equivalent scalar,
and that a higher Rf lowers Sharpe. 56 tests pass; ruff/ty clean.

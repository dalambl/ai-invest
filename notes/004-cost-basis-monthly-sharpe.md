# 004 — Cost-basis seeding, monthly-tab horizon, and Sharpe risk-free rate

## Context

After the rebuild from [003](003-fix-options-cost-fx-trades.md), the
dashboard's *Monthly* tab showed inflated annual returns (e.g.
2023 ≈ +72%, 2024 ≈ +50%) and the headline Sharpe ratio looked
implausibly high. Three independent issues were causing this:

1. **Cost-basis cliff artifact.** Pre-existing positions (from the
   `positions` table at the start of the rebuild window) were entering
   the daily replay with `cost_basis = 0` until the *first transaction*
   for that symbol. Any partial sell before that point credited the
   *full* sale proceeds to `__REALIZED__`, and on the day the position
   finally reached zero the cost basis was *post-hoc* back-patched —
   producing visible single-day P/L cliffs (notably **+24.71% on
   2023-09-12** on the cumulative-return chart).
2. **"Yearly" row over the wrong window.** `/api/returns/monthly`
   delegated to `pnl_timeseries` without overriding `from_date`, so it
   inherited that endpoint's rolling-365-day default. The label "2023"
   in the yearly row was actually the trailing-12-months ending in
   April 2024, etc.
3. **Sharpe assumed Rf = 0.** `risk_metrics()` divided
   annualized-return by annualized-vol with no risk-free deduction.
   With cash earning ~4.5% over the period this materially overstates
   risk-adjusted return.

## Changes

### `rebuild_history.py` — two-pass cost basis

`build_daily_positions(transactions, pre_existing, current_positions, init_costs=None)`
now accepts a pre-computed `init_costs` mapping. Logic:

* If `init_costs[sym]` is provided, use it as the seed cost basis.
* Else if the saved `positions.avg_cost` is > 0, use `avg_cost * qty`.
* Else mark the symbol as needing initialization (`needs_cost_init`).

`generate_snapshots` no longer back-patches missing costs — its
docstring now explicitly says "cost bases must already be seeded". The
old `cost_inits` patching loop and `needs_cost_init` parameter were
removed.

`main()` runs in two passes:

```
prices = fetch_all_historical_prices(price_symbols, first_tx_date, end_date)

# For every pre-existing symbol, find the close at-or-before
# first_tx_date and seed cost = qty × that price.
init_costs = {}
for sym, qty in pre_existing.items():
    ...
    init_costs[sym] = round(px * qty, 2)

(...) = build_daily_positions(transactions, pre_existing, current_positions, init_costs)
```

This means the very first snapshot of each pre-existing position has a
realistic cost basis (= mark-to-market at the start of the rebuild
window) and partial sells throughout the window properly amortize
that basis instead of treating it as zero.

### `server.py` — explicit horizon for monthly tab

```python
@app.get("/api/returns/monthly")
async def returns_monthly(from_date: str | None = None, to_date: str | None = None):
    if not from_date:
        dates = await db.get_snapshot_dates()
        from_date = dates[0] if dates else "2000-01-01"
    ts = await pnl_timeseries(from_date, to_date)
    ...
```

The yearly aggregation now sees full calendar years instead of a
trailing-365-day rolling slice.

### `finance.py` — Sharpe with risk-free rate

`risk_metrics(values, pnl, trading_days=252, risk_free_rate=0.0)`
gained a `risk_free_rate` parameter (annualized decimal, e.g. 0.045 for
4.5%). The Sharpe numerator is now excess return:

```python
daily_rf = risk_free_rate / trading_days
excess_mean = mean_r - daily_rf
sharpe = (excess_mean * trading_days) / (std_r * math.sqrt(trading_days))
```

Default remains 0.0, so existing tests are unaffected.

## Verification

Full pipeline green:

```
uv run ruff format && uv run ruff check && uv run ty check && uv run pytest -q
# 51 passed
```

Re-ran `rebuild_history.py` (using the port-8765 workaround for the
VS Code port-8000 squat — see [003](003-fix-options-cost-fx-trades.md)).

Post-rebuild diagnostic on the cumulative-return series:

```
Top single-day moves
  2025-04-09:  +8.06%  (prev_value=$104,833  dPnL=+$8,452)   # post-tariff rally
  2025-04-04:  -6.27%  (prev_value=$116,185  dPnL=-$7,284)
  2023-02-08:  +5.94%  (prev_value=$159,506  dPnL=+$9,473)
  2025-04-03:  -3.68%  (prev_value=$120,627  dPnL=-$4,442)
  ...

Yearly returns
  2023: +17.70%
  2024:  +9.95%
  2025: +28.73%
  2026:  +7.93%   (YTD)

Overall (Rf=4.5%):
  ann_return = 19.06%
  vol        = 16.06%
  sharpe     = 0.91
  max_dd     = 17.57%
```

The 2023-09-12 +24.71% phantom spike is **gone**. Largest single-day
moves now correspond to real macro events (April 2025 tariff
volatility, February 2023 CPI day) and are proportional to portfolio
size on the day.

## Remaining

* `/api/risk` still calls `finance.risk_metrics(...)` without passing
  a `risk_free_rate`. Promoting it to a query parameter (default 0)
  would let the dashboard surface Sharpe with a user-chosen Rf — the
  diagnostic above used 4.5% manually.
* The `notes/overview.md` index is updated to point here.

# Verify, improve, and test portfolio tracker

**Status:** complete

Work initiated from user request: "suggest improvements, verify it's working,
create tests, fix what isn't working" on the portfolio-tracking code.

Corresponding plan file (approved, still authoritative):
`/home/david/.claude/plans/goofy-sprouting-charm.md`.

*Summarized 2026-04-22 by Claude Opus 4.6 (`claude-opus-4-7`).*

## What has been done

### Bugs fixed

1. **Monthly-returns grid (double bug)** in `static/app.js` old `renderMonthlyGrid`.
   - Was computing per-month return as raw `market_value` ratios (`end/start - 1`),
     which conflated investment performance with cash flows (buys/deposits).
   - Was summing simple monthly returns to derive yearly return instead of
     compounding.
   - Fix: new backend endpoint `/api/returns/monthly` that uses the daily-linked
     cumulative-return series already produced by `/api/pnl/timeseries`, chains
     it into month-end index levels, and compounds to yearly. Frontend rewritten
     to consume `{monthly, yearly}` directly.

2. **`exposure/asset_class` was just an alias for `exposure/sector`.** The
   former was implemented as `await exposure_sector()`. The endpoint is named
   asset-class but groups by `sec_type`, not GICS sector.
   - Fix: `/api/exposure/asset_class` is now the real implementation;
     `/api/exposure/sector` kept as a back-compat alias.

3. **`asyncio.get_event_loop()` inside running async code** — deprecated in 3.12+,
   raises in 3.14+. Three call sites in `server.py`. Replaced with
   `asyncio.get_running_loop()`.

4. **Tooling drift.** `pyproject.toml` was Poetry + Python ^3.12; `CLAUDE.md`
   specifies uv + Python 3.13+ and calls `ruff` and `ty`. Migrated to
   uv/PEP 621 + added dev deps (`pytest`, `pytest-asyncio`, `ruff`, `ty`) and
   tool config.

5. **`rebuild_history.py` had duplicate `SKIP_SYMBOLS`** (empty set at top,
   real set further down). Removed the shadow.

6. **19 ruff lint issues** — cleaned up across `db.py`, `ibkr.py`,
   `rebuild_history.py`, `server.py`, `tests/test_finance.py`. Includes:
   `contextlib.suppress` for migration try/except/pass; NaN checks replaced
   with a `_is_real(x)` helper (`x is not None and not math.isnan(x)`);
   `str | None` annotations for query params; moved function-level imports to
   module scope where feasible.

### New module

- **`finance.py`** — pure helpers, the single source of truth for portfolio math:
  - `aggregate_snapshot_timeseries` — group snapshot rows by date; excludes the
    `__REALIZED__` pseudo-row from value/cost but includes it in pnl/total_return.
  - `daily_linked_returns` — `Δpnl / prev_value` chained; separates investment
    performance from cash flows.
  - `drawdown_series`, `max_drawdown_pct`.
  - `risk_metrics` — annualized return, vol, Sharpe, max drawdown. Sharpe is
    floored to 0 when `ann_vol < 1e-9` to avoid FP-noise blowups.
  - `monthly_returns` / `year_returns_from_months`.
  - `weights_by_currency` — exposure by currency (uses `abs(market_value)`).
  - `horizon_start_date` — pill label → date.

### Tests

- **`tests/test_finance.py`** — 32 tests, all passing (`uv run pytest -q` →
  `32 passed`). Covers every finance helper including edge cases
  (empty inputs, `__REALIZED__` handling, cash-inflow-is-ignored, length
  mismatches, constant-return with FP precision).

### Refactors

- `server.py` endpoints delegate to `finance.py`:
  `/api/pnl/timeseries`, `/api/risk`, `/api/pnl`, the new `/api/returns/monthly`.
- New `/api/exposure/currency` endpoint surfaces FX exposure.

### Check pipeline status

- `uv run ruff format` ✓ clean
- `uv run ruff check` ✓ **All checks passed!**
- `uv run pytest -q` ✓ **32 passed in 0.02s**
- `uv run ty check` ✓ **All checks passed!** (17 pre-existing `ibkr.py` errors
  resolved — see "Handoff items completed" below)

## Handoff items completed

1. **Added `python-multipart` dep** via `uv add python-multipart`. Verified
   `uv run python -c 'import server'` succeeds.

2. **Fixed all 17 pre-existing `ibkr.py` ty errors:**
   - Added `assert self._ib is not None` after the `if not self.connected:`
     guard in each method that calls `self._ib.foo(...)` (7 sites:
     `fetch_close_prices`, `get_positions`, `get_portfolio`,
     `get_account_summary`, `get_fills`, `get_completed_orders`,
     `get_historical_data`, `get_live_quote`).
   - For the 3 `round(x, n)` calls in `_enrich_position` where `x` is
     `float | None` at the type-checker level (`price`, `upnl`, `rpnl`),
     wrapped with `float(x or 0.0)`.

3. **Checked `ibkr.py` for dead `from functools import partial`** — not
   present.

## Not committed

User did not authorize commits.

## Key open TODOs (documented, not being fixed)

- **Multi-currency:** portfolio has USD/EUR/CAD/DKK; `market_value` is summed
  across currencies without FX conversion. Needs design work (base currency,
  FX source, snapshot schema migration). Surfaced via new
  `/api/exposure/currency` but not corrected.
- **Cash balance not tracked in snapshots.** "Portfolio value" = open-positions
  market value only. After a sale, realized proceeds disappear from the chart
  unless reinvested.
- **Benchmark fetch** uses IB `reqHistoricalData`; if TWS is down the benchmark
  panel is empty. Could fall back to Yahoo for benchmarks too.

## Files changed in this session

- `pyproject.toml` (Poetry → uv; Python 3.13; dev deps; tool config)
- `finance.py` (new)
- `server.py` (refactored endpoints + new endpoints + type annot fixes +
  event-loop fix + small cleanups)
- `static/app.js` (`renderMonthlyGrid` + `loadPerformance`)
- `db.py` (`contextlib.suppress` for schema-evolution blocks)
- `ibkr.py` (added `_is_real` NaN helper; cleaned up unused var and NaN idioms;
  added `assert self._ib is not None` after connected guards; wrapped
  nullable-numeric `round()` args in `float(x or 0.0)`)
- `pyproject.toml` (added `python-multipart` runtime dep)
- `rebuild_history.py` (removed shadowed `SKIP_SYMBOLS`; moved imports to top;
  removed unused `side_mult`)
- `tests/test_finance.py` (new — 32 tests)
- `tests/__init__.py` (new, empty)
- `notes/002-verify-and-fix.md` (this file)

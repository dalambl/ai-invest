# Multi-currency P&L: cost-basis & UI reconciliation fixes

Three follow-up bugs surfaced after the initial multi-currency split
(`notes/overview.md` 2026-04-22 entry). All resolved here.

## 1. Cost basis mixed local & USD

`build_daily_positions` was adding `tx["net"]` (USD per IB convention)
to `cost_bases[sym]` (local currency). For EUR/CAD/DKK trades this
double-counted FX.

Fix: separate `add_cost_local = qty × price` (local) from
`add_cost_usd = abs(net)` (USD), accumulate into `cost_bases` and
`cost_bases_usd` independently. Sells scale both sides by the same
`sell_fraction`. Updated 2 EUR-denominated tests to use realistic IB
conventions (Net Amount in USD, not local).

## 2. Pre-existing-position seed used wrong cost

Previously the cost-basis seed for positions present before the first
CSV transaction was `pre_qty × day-1 market price`. For RHM this gave
3 × ~207 EUR ≈ 621 EUR — but the actual purchase was around 1432 EUR.
That undercounted RHM cost by roughly 4000 EUR / $4000 USD.

Fix: derive the seed from IB's reported average cost using the
average-cost-method invariance:

```
init_local = ib_avg × (pre_qty + total_buy_qty) − total_buy_local_cost
```

Buys after the seed contribute their actual local cost; the residual
must equal the pre-existing position's cost so that the post-rebuild
average matches IB exactly. For RHM: 3 × 1432 = 4296 EUR seed → total
local cost 10392 EUR (matches IB).

USD-side seed = `init_local × fx(first_tx_date)` — approximate (we
don't have the original buy date), but close enough for FX
decomposition.

## 3. UI didn't reconcile Stock + FX = Unrealized

IB returns `market_value` and `unrealized_pnl` in the **contract's
local currency** for foreign positions (EUR for RHM, CAD for CSU,
DKK for ORSTED). Stock P&L and FX P&L are in USD. Dashboard mixed
currencies in one row, so columns visibly didn't add up.

For RHM: IB reported `unrealized_pnl = -375.05` (EUR), shown next to
`stock_pnl_usd = -441.97` and `fx_pnl_usd = +401.43` (USD). The user
flagged: "stock and fx pnl don't add up to unrealized".

Fix in `enrich_positions_from_db`: when a foreign-currency snapshot
exists, override `market_value` and `unrealized_pnl` with their
USD-denominated equivalents:

```python
if ccy != "USD" and snap.get("market_value_usd") is not None:
    p["market_value"] = p["market_value_usd"]
    p["unrealized_pnl"] = round(p["market_value_usd"] - p["cost_basis_usd"], 2)
    p["total_return"] = round(p["unrealized_pnl"] + p["dividends_cumulative"], 2)
```

Verification across all foreign holdings (post-rebuild, post-restart):

| Sym    | Ccy | MV USD    | Cost USD  | Unreal   | Stock    | FX      | Stock+FX |
|--------|-----|-----------|-----------|----------|----------|---------|----------|
| RHM    | EUR | 11,804.42 | 11,844.97 |   -40.55 |  -441.97 | +401.43 |   -40.54 |
| MC     | EUR | 11,454.43 | 11,304.44 |  +149.99 |  -261.68 | +411.67 |  +149.99 |
| AIR    | EUR |  3,941.88 |  5,625.32 | -1683.44 | -2167.50 | +484.06 | -1683.44 |
| CSU    | CAD |  5,719.22 |  6,899.28 | -1180.06 | -1262.11 |  +82.05 | -1180.06 |
| ORSTED | DKK |    255.26 |    223.38 |   +31.88 |   +38.01 |   -6.12 |   +31.89 |

All positions now reconcile exactly (rounding aside). USD positions
unaffected (override is gated on `ccy != "USD"`).

## Operational note

`urlopen("http://localhost:8000/...")` from `rebuild_history.py`
hung silently for ~37 minutes because VS Code Live Preview squats
:8000. Routed around with `AIINVEST_API=http://localhost:8765`
(server runs on 8765 to avoid the conflict). Background uvicorn
needs `setsid ... < /dev/null > log 2>&1 &` to survive the Bash tool's
session teardown — plain `&` got killed.

## Result

54 tests pass. Latest snapshot (2026-04-22) totals: MV $290,227.83,
Stock P&L $94,673.27, FX P&L $1,373.09, Total Return $104,883.81.
Foreign-position unrealized columns reconcile in the UI.

## Followup: AIR was off by ~$1,900

The fix above (`p["market_value"] = p["market_value_usd"]` in
`enrich_positions_from_db`) had two downstream side-effects that
manifested when TWS was online and `sync_positions` was running:

1. **Snapshot column corruption** — `sync_positions` reads
   `p["market_value"]` to write the snapshot's local-currency
   `market_value`. The mutation made today's row store the USD value
   in the local column, then the next request re-derived
   `stock_pnl_usd` from that wrong "local" value, compounding the
   error. AIR showed mv_local=3941.88 (USD masquerading as EUR)
   instead of 4987.20 EUR.

2. **`ibkr.py` was reading IB's `marketValue` / `unrealizedPNL` as
   local-currency** — but `ib_insync.portfolio()` items return those
   in the **account base currency (USD)**. For AIR this gave
   nonsensical values that didn't even match `qty × price`.

Real fix:

- `_enrich_position` now always computes `mv = qty × price ×
  multiplier` and `upnl = mv − cost` locally. IB's USD `live_mv`
  / `live_upnl` are ignored.
- `enrich_positions_from_db` no longer mutates `p["market_value"]` /
  `p["unrealized_pnl"]`. Instead it derives USD fields fresh from
  today's `mv_local × fx_rate` (vs pulling the *stale* `mv_usd`
  from the previous snapshot row), and exposes new
  `unrealized_pnl_usd` / `total_return_usd` fields.
- Frontend (`renderHoldings`) renders `unrealized_pnl_usd` in the
  Unrealized column so it reconciles with Stock + FX (also USD).
  Falls back to `unrealized_pnl` for USD-only positions.

Verified post-fix (2026-04-22 live snap):

| Sym    | Ccy | MV local | MV USD    | Cost USD  | Unreal USD | Stock   | FX      |
|--------|-----|----------|-----------|-----------|------------|---------|---------|
| AIR    | EUR |  4987.20 |  5,877.11 |  5,625.32 |    +251.79 |  -232.27 | +484.06 |
| RHM    | EUR | 10017.00 | 11,804.42 | 11,844.97 |     -40.55 |  -441.97 | +401.42 |
| MC     | EUR |  9486.00 | 11,178.67 | 11,304.44 |    -125.77 |  -537.44 | +411.67 |
| CSU    | CAD |  7803.30 |  5,719.22 |  6,899.28 |  -1,180.06 | -1263.57 |  +83.51 |
| ORSTED | DKK |  1618.50 |    255.26 |    223.38 |     +31.88 |   +30.28 |   +1.60 |

AIR's actual P&L is **+$252 USD** (small EUR loss offset by EUR
strengthening from ~1.085 at buy to ~1.178 today). The previous
"-$1683 USD" was double-counted FX corruption from the snapshot bug.

# 001 — Initial repo survey

First pass reading the codebase end-to-end to establish a baseline understanding
for future work.

## What exists today

A FastAPI-backed portfolio dashboard wired to Interactive Brokers TWS, with a
plain HTML/JS frontend served from `static/`. SQLite (`data/trading.db`) is the
primary store; daily CSV snapshots in `data/snapshots/` are the portable backup
and also the initial import source.

### Module map

| File | Role |
|------|------|
| `server.py` | FastAPI app. REST endpoints (`/api/positions`, `/api/pnl`, `/api/pnl/timeseries`, `/api/performance`, `/api/risk`, `/api/exposure/*`, `/api/trades`, `/api/dividends`, `/api/market/*`, import/export), two WebSockets (`/ws/prices`, `/ws/pnl`), lifespan that initialises DB + triggers background TWS connect. Contains `enrich_positions_from_db` which compensates for IB's known issue of returning `market_price == avg_cost` when no live quote is available by falling back to last known snapshot price, then Yahoo Finance. |
| `ibkr.py` | `IBConnection` singleton. Runs an `ib_insync` event loop on a dedicated thread; exposes sync methods (`get_positions`, `get_portfolio`, `get_account_summary`, `get_fills`, `get_completed_orders`, `get_historical_data`, `get_live_quote`) that marshal calls via `_call` onto the loop. |
| `db.py` | aiosqlite schema + CRUD. Tables: `positions`, `trades`, `snapshots`, `dividends`, `watchlist`. Handles schema evolution via `ALTER TABLE … ADD COLUMN` best-effort. Imports CSVs on startup. |
| `models.py` | Dataclasses for `Position`, `Trade`, `Snapshot` (CSV row helpers). |
| `snapshot.py` | One-shot script: connect, fetch portfolio, write DB row + snapshot CSV for today. |
| `trade_log.py` | Standalone CLI: pulls trades via IB Flex report or live fills, writes `data/trades.csv` + `data/positions.csv` + `data/reconciliation.csv`. |
| `rebuild_history.py` | Replays a historical IB transactions CSV (`data/U640574.TRANSACTIONS.*.csv`) into daily snapshots. Infers pre-existing positions, computes realized P&L on sells, writes a `__REALIZED__` pseudo-row per snapshot, fetches historical prices via IB first then Yahoo fallback, and backfills `dividends_cumulative` / `purchase_date` / `avg_cost` on positions. |
| `connect_tws.py` | Smoke test that connects to TWS and prints account summary + positions. |
| `static/` | `index.html`, `style.css`, `app.js`, `components/` — frontend (not inspected in this pass). |

### Data currently present

- 803 daily snapshot CSVs in `data/snapshots/` (starting `2023-01-31`), matching schema `date, symbol, quantity, market_price, market_value, day_pnl, cost_basis, dividends_cumulative, total_return`.
- `data/trading.db` (~2.3 MB) is the live DB.
- `data/U640574.TRANSACTIONS.20221230.20260306.csv` — raw IB transaction export used by `rebuild_history.py`.

### Key design decisions baked into the code

- **Realized P&L is carried through time via a synthetic `__REALIZED__` row in each snapshot.** Both `rebuild_history.py` and `server.sync_positions` preserve this row so that P&L totals include gains/losses from closed positions.
- **Price fallback chain (live → last-good snapshot → Yahoo → avg_cost).** Encoded in `server.enrich_positions_from_db`. `get_latest_known_prices` explicitly skips snapshots where `|day_pnl| < 0.01` because those were fallback-to-avg-cost rows.
- **Yahoo ticker mapping** for non-US symbols duplicated in `server.py` and `rebuild_history.py` (`CSU→CSU.TO`, `RHM→RHM.DE`, `MC→MC.PA`, `ORSTED→ORSTED.CO`, `PSKY→PSKY.L`).
- **Daily-linked returns for risk/timeseries**, not simple cumulative: `/api/pnl/timeseries` and `/api/risk` both compute `daily_return = Δpnl / prev_value` and chain them — so cash flows don't distort returns.
- **Snapshot save is gated**: if more than two STK/ETF positions have no real price (`market_price ≈ avg_cost`), `sync_positions` refuses to write the snapshot to avoid poisoning history.

## Discrepancies between `plan.md`, `CLAUDE.md`, and current state

- `plan.md` mentions `scheduler.py`; no such file exists. Snapshots currently happen on startup (via `try_connect_tws` → `sync_positions`) or manually via `POST /api/snapshot` / running `snapshot.py`.
- `pyproject.toml` still says Poetry + `python = "^3.12"`. `CLAUDE.md` directs the workflow to `uv run` and Python 3.13+. Poetry/uv mismatch and Python version mismatch both exist.
- `CLAUDE.md` lists `uv run ty check` as part of the pre-commit workflow; `ty` is not a declared dev dependency in `pyproject.toml`.
- `CLAUDE.md`'s project layout declares only `notes/` — existing top-level `.py` files aren't in a package. That's consistent with "experimental/research application", not a library.
- `plan.md` has a rich phase 3–7 roadmap; most of it is already built in `server.py` + `static/`. Plan appears to pre-date the current implementation.

## Observations worth remembering

- `ibkr.IBConnection._call` has a hard 30s timeout and swallows errors (returns `None`). Callers must null-check. This is deliberate but easy to miss.
- Symbol normalization is ad-hoc: `rebuild_history.SYMBOL_MAP` maps `M6EM6→M6E` and `BIPC.OLD→BIPC`; `SKIP_SYMBOLS` filters `M6E, OXY WS, NVDA, PLTR, QQQ` during price fetching (unclear why NVDA/PLTR/QQQ are skipped — possibly no historical data needed, or known-broken at the time).
- `db.init_db` uses best-effort `ALTER TABLE` for schema evolution. New columns should be added the same way to avoid breaking existing DBs.
- `server.exposure_sector` is a misnomer — it actually groups by `sec_type` (STK/OPT/FUT/…), not GICS sector. `exposure_asset_class` just delegates to it.
- Frontend (`static/`) has not yet been read in this pass.

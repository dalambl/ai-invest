# Trading Dashboard — Implementation Plan

## Goal

Build a browser-based portfolio dashboard connected to Interactive Brokers TWS
that stores positions in CSV, displays positions and P&L with multi-horizon
toggles, and replicates key Koyfin capabilities.

---

## Phase 1: Data Layer — CSV Position Storage

**Objective:** Replace Excel-only output with a CSV-based position store that
serves as the single source of truth.

### Files to create/modify
- `positions.csv` — auto-generated, append-friendly
- `trades.csv` — flat trade log (replaces Excel-only Trade_Log)
- `data/snapshots/` — daily position snapshots for historical P&L calculation
- `models.py` — dataclasses for Position, Trade, Snapshot

### CSV schemas

**positions.csv**
```
timestamp, account, symbol, sec_type, quantity, avg_cost, market_price,
market_value, unrealized_pnl, realized_pnl, currency
```

**trades.csv**
```
trade_date, symbol, description, asset_class, action, quantity, price,
currency, commission, net_amount, exchange, order_type, account, trade_id
```

**snapshots/{YYYY-MM-DD}.csv**
```
date, symbol, quantity, market_price, market_value, day_pnl, cost_basis
```

### Tasks
1. Modify `trade_log.py` to write CSV alongside Excel. I don't need to write excel any more.
2. Create `snapshot.py` — connects to TWS, snapshots all positions + prices,
   writes to `data/snapshots/`
3. Create `scheduler.py` — runs snapshot at market close (or on-demand)



---

## Phase 2: Backend API

**Objective:** Python backend serving position data, trade history, and market
data to the browser frontend.

### Tech choice
- **FastAPI** — async-friendly, pairs well with ib_insync's async methods
- **SQLite** — lightweight persistent store for snapshots/trades (CSV import on
  startup, then DB is primary)
- **WebSocket** — for live price streaming from TWS to browser

### Files to create
- `server.py` — FastAPI app
- `db.py` — SQLite setup, migrations, queries
- `ibkr.py` — IB connection manager (singleton, reconnect logic)

### API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/positions` | Current positions with live prices |
| GET | `/api/positions/history?date=YYYY-MM-DD` | Historical snapshot |
| GET | `/api/trades?from=&to=` | Trade log with filters |
| GET | `/api/pnl?horizon=1d` | P&L for selected horizon |
| GET | `/api/pnl/timeseries?from=&to=` | Daily P&L series for charting |
| GET | `/api/performance?benchmark=SPY` | Cumulative returns vs benchmark |
| GET | `/api/exposure/sector` | Sector breakdown |
| GET | `/api/exposure/asset_class` | Asset class breakdown |
| GET | `/api/risk` | Risk metrics (beta, alpha, Sharpe, max drawdown) |
| GET | `/api/market/quote/{symbol}` | Live quote |
| GET | `/api/market/history/{symbol}?period=1y&bar=1d` | Historical bars |
| GET | `/api/market/movers` | Top gainers/losers from watchlist |
| WS | `/ws/prices` | Live streaming quotes |
| WS | `/ws/pnl` | Live P&L updates |

---

## Phase 3: Browser Frontend — Core Dashboard

**Objective:** Single-page app that loads in any browser, no build step required
for v1 (plain HTML + JS + CSS, or lightweight framework).

### Tech choice
- **HTML/CSS/JS** with **Plotly.js** for charts (already have plotly in venv)
- Optionally migrate to **React + Vite** in a later phase
- **Dark/light theme** toggle (CSS variables)

### Layout (Koyfin-inspired)

```
┌─────────────────────────────────────────────────────────┐
│  Navbar: Account selector | Theme toggle | Connection   │
├────────┬────────────────────────────────────────────────┤
│        │                                                │
│  Side  │  Main Content Area                             │
│  nav   │                                                │
│        │  ┌─ Tab: Holdings ──────────────────────────┐  │
│  Home  │  │  Holdings table with P&L columns         │  │
│  Perf  │  │  Expandable lots per position            │  │
│  Risk  │  │  Sortable, color-coded (green/red)       │  │
│  Expos │  │  Summary row (total MV, total P&L, etc)  │  │
│  Trades│  └──────────────────────────────────────────┘  │
│  Watch │                                                │
│  Market│  ┌─ Tab: Performance ───────────────────────┐  │
│        │  │  Horizon pills:                          │  │
│        │  │  [1D][5D][MTD][1M][QTD][3M][6M]         │  │
│        │  │  [YTD][1Y][3Y][5Y][ALL]                  │  │
│        │  │                                          │  │
│        │  │  Cumulative returns chart (+ benchmark)  │  │
│        │  │  Monthly returns heatmap grid             │  │
│        │  └──────────────────────────────────────────┘  │
│        │                                                │
├────────┴────────────────────────────────────────────────┤
│  Status bar: TWS connected | Last update | Account      │
└─────────────────────────────────────────────────────────┘
```

### Core components

#### 3a. Holdings Table
- Columns: Symbol, Name, Asset Class, Qty, Avg Cost, Last Price, Market Value,
  Day Change ($ and %), Unrealized P&L, Realized P&L, Weight %, Currency
- Green/red color coding on change and P&L columns
- Click row to expand lots (if available from trade history)
- Sort by any column
- Summary row at bottom: totals for MV, P&L, commissions
- Export to CSV button

#### 3b. P&L Horizon Selector
- Pill-style buttons: **1D | 5D | MTD | 1M | QTD | 3M | 6M | YTD | 1Y | 3Y | 5Y | ALL**
- Clicking a pill:
  - Updates the P&L column in the holdings table to show P&L for that period
  - Updates the performance chart below
  - Fetches from `/api/pnl?horizon=<selected>`
- Calculation: compare current market value to snapshot value at horizon start
  date

#### 3c. Performance Chart
- Plotly.js line chart
- Series: portfolio cumulative return, benchmark cumulative return
- Tooltip: date, portfolio %, benchmark %, active return
- Driven by horizon selector
- Toggle: dollar growth vs percentage return

#### 3d. Monthly Returns Grid
- Calendar-style heatmap: rows = years, columns = months
- Cell value = monthly % return
- Color intensity: green (positive) to red (negative)
- Annual total column on the right

---

## Phase 4: Exposure & Risk (Koyfin Parity)

### 4a. Exposure Tab
- **Sector breakdown** — pie chart + table (% of portfolio by GICS sector)
- **Asset class breakdown** — STK, OPT, FUT, CASH, BOND
- **Geographic breakdown** — by exchange country
- **Top holdings** — bar chart of largest positions by weight
- Data source: position data + IB contract details (`reqContractDetails`)

### 4b. Risk Tab
- **Metrics panel:** Annualized Return, Volatility, Sharpe Ratio, Sortino
  Ratio, Beta (vs SPY), Alpha, Max Drawdown, Calmar Ratio
- **Drawdown chart** — time series of drawdown from peak
- **Top drawdowns table** — ranked worst peak-to-trough declines with dates
- **Rolling returns chart** — 1Y rolling annualized return over time
- Calculation: use daily snapshots from `data/snapshots/`

---

## Phase 5: Trade History & Watchlist

### 5a. Trades Tab
- Full trade log table from `trades.csv` / DB
- Filters: date range, symbol, action (buy/sell), asset class
- Columns: Date, Symbol, Action, Qty, Price, Commission, Net Amount, Exchange
- Sortable, paginated
- Running P&L column (cumulative realized P&L)

### 5b. Watchlist
- User-configurable symbol list (stored in `watchlist.json`)
- Columns: Symbol, Last, Change, Change %, Volume, Day Range, 52W Range
- Sparkline mini-chart per row (intraday price via `reqHistoricalData` 1-min
  bars)
- Green/red color coding
- Click symbol to view detail chart
- Live updates via WebSocket

---

## Phase 6: Market Data & Charting

### 6a. Symbol Detail View
- Click any symbol (from holdings, watchlist, or trades) to open detail
- **Price chart** with configurable timeframes and bar sizes
- **Key stats:** market cap, P/E, dividend yield, 52W high/low
- **Recent trades** for that symbol from your trade log
- Data: `reqHistoricalData`, `reqMktData`, `reqFundamentalData`

### 6b. Market Overview Widget
- Top gainers/losers from watchlist or portfolio
- Major index prices (SPY, QQQ, DIA, IWM)
- Data from `reqMktData` streaming

### 6c. Heatmap
- Treemap of portfolio positions
- Size = market value weight
- Color = daily % change (green to red)
- Plotly.js treemap chart

---

## Phase 7: Polish & Advanced Features

### 7a. Dark/Light Theme
- CSS custom properties (`--bg`, `--text`, `--accent`, `--green`, `--red`)
- Toggle button in navbar
- Persist preference in localStorage

### 7b. Keyboard Navigation
- `/` opens command bar (search symbols, jump to sections)
- `1-6` for tab switching
- `d` toggle dark mode

### 7c. Alerts (stretch)
- Price alerts: notify when symbol crosses a threshold
- P&L alerts: notify on daily loss exceeding threshold
- Browser notifications API

### 7d. Multi-Account Support
- Account selector dropdown in navbar
- Filter all views by account or show aggregate

---

## Architecture Summary

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Browser    │◄───►│   FastAPI    │◄───►│   IB TWS     │
│   (Plotly)   │ WS  │   server.py  │     │   Gateway    │
└──────────────┘     └──────┬───────┘     └──────────────┘
                            │
                     ┌──────┴───────┐
                     │   SQLite     │
                     │   + CSV      │
                     │   backups    │
                     └──────────────┘
```

### Data flow
1. `ibkr.py` connects to TWS, streams live data
2. `snapshot.py` takes daily snapshots → CSV + SQLite
3. `server.py` serves REST + WebSocket to browser
4. Browser renders tables, charts, and live updates
5. CSVs remain as portable backup/export format

---

## Implementation Order

| Priority | Phase | Effort | Depends On |
|----------|-------|--------|------------|
| 1 | Phase 1: CSV storage | Small | Existing code |
| 2 | Phase 2: Backend API | Medium | Phase 1 |
| 3 | Phase 3a-b: Holdings + P&L pills | Medium | Phase 2 |
| 4 | Phase 3c-d: Performance chart + monthly grid | Medium | Phase 2 |
| 5 | Phase 4: Exposure + Risk | Medium | Phase 2 + snapshots |
| 6 | Phase 5: Trades + Watchlist | Medium | Phase 2 |
| 7 | Phase 6: Market data + charting | Large | Phase 2 |
| 8 | Phase 7: Polish + advanced | Small | All above |

---

## Key Dependencies to Install

```
fastapi
uvicorn[standard]
aiosqlite
websockets
plotly          # already available
```

## Files to Create (ordered)

```
models.py           # dataclasses
db.py               # SQLite layer
ibkr.py             # IB connection manager
snapshot.py         # daily snapshot writer
server.py           # FastAPI app
static/
  index.html        # single-page app
  style.css         # dark/light theme
  app.js            # dashboard logic
  components/
    holdings.js     # holdings table
    performance.js  # charts + horizon pills
    exposure.js     # sector/asset breakdowns
    risk.js         # risk metrics + drawdown
    trades.js       # trade log table
    watchlist.js    # watchlist with sparklines
    market.js       # market overview + heatmap
```

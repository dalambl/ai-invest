#!/usr/bin/env python3
"""
Rebuild portfolio snapshots from IB transaction history CSV.
Parses buys/sells/dividends, infers pre-existing positions,
fetches historical prices from IB, and generates daily snapshots.
"""

import contextlib
import csv
import json
import logging
import os
import sqlite3
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path

import yfinance as yf

import fx
import ib_statement

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "trading.db"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
CSV_PATH = DATA_DIR / "U640574.TRANSACTIONS.20221230.20260306.csv"

SYMBOL_MAP = {
    "BIPC.OLD": "BIPC",
}

# OCC option symbol pattern: "ROOT  YYMMDD[CP]NNNNNNNN" — root then date+side+strike.
# Description form: "ROOT DDMMMYY STRIKE [P|C]" e.g. "NVDA 17DEC27 110 P".
_OPTION_DESC_TAIL = (" P", " C")
_FUTURES_PREFIXES = ("M6E",)  # extend as needed


def parse_float(s):
    if not s or s == "-":
        return 0.0
    s = s.strip().replace("(1)", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def classify(description: str, raw_symbol: str) -> tuple[str, str, float]:
    """Classify a transaction as (sec_type, normalized_symbol, multiplier).

    - Options: description ends with " P"/" C" and contains an expiry token.
      Returns ("OPT", description, 100).
    - Futures: raw_symbol matches futures-prefix list (e.g. M6EM6).
      Returns ("FUT", description, 1) — futures are excluded from positions
      downstream because we don't have reliable multiplier/price data.
    - Stocks: returns ("STK", cleaned_symbol, 1).
    """
    desc = description.strip()
    sym = raw_symbol.strip()

    if desc.endswith(_OPTION_DESC_TAIL) and len(desc.split()) >= 4:
        return "OPT", desc, 100.0

    if any(sym.startswith(p) and len(sym) > len(p) for p in _FUTURES_PREFIXES):
        return "FUT", desc, 1.0

    # Stock — strip OCC-style spaces, apply alias map
    if " " in sym and len(sym) > 10:
        sym = sym.split()[0]
    sym = SYMBOL_MAP.get(sym, sym)
    return "STK", sym, 1.0


def parse_transactions(csv_path):
    transactions = []
    with open(csv_path) as f:
        for line in f:
            if not line.startswith("Transaction History,Data,"):
                continue
            parts = next(csv.reader([line]))
            if len(parts) < 12:
                continue
            (
                _,
                _,
                dt,
                account,
                description,
                tx_type,
                symbol,
                qty,
                price,
                currency,
                gross,
                commission,
                net,
            ) = parts[:13]

            sec_type, sym, mult = classify(description, symbol)
            if not sym or sec_type == "FUT":
                # Futures lack reliable price/multiplier mapping; exclude.
                continue

            transactions.append(
                {
                    "date": dt.strip(),
                    "account": account.strip(),
                    "description": description.strip(),
                    "type": tx_type.strip(),
                    "symbol": sym,
                    "sec_type": sec_type,
                    "multiplier": mult,
                    "quantity": parse_float(qty),
                    "price": parse_float(price),
                    "currency": currency.strip(),
                    "gross": parse_float(gross),
                    "commission": parse_float(commission),
                    "net": parse_float(net),
                }
            )

    transactions.sort(key=lambda t: t["date"])
    return transactions


def get_current_positions():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    # Only STK rows seed the rebuild — options/futures are re-derived from
    # transactions under their full contract symbol (e.g. "NVDA 17DEC27 110 P"),
    # so the collapsed underlier-only OPT/FUT rows in ``positions`` would just
    # become phantom duplicates.
    rows = db.execute(
        "SELECT symbol, quantity, avg_cost, sec_type, currency FROM positions WHERE sec_type='STK'"
    ).fetchall()
    db.close()
    return {
        r["symbol"]: {
            "quantity": r["quantity"],
            "avg_cost": r["avg_cost"],
            "sec_type": r["sec_type"],
            "currency": r["currency"] or "USD",
        }
        for r in rows
    }


def compute_position_history(transactions, current_positions):
    buys = defaultdict(float)
    sells = defaultdict(float)
    buy_cost = defaultdict(float)

    for tx in transactions:
        sym = tx["symbol"]
        if tx["type"] == "Buy":
            buys[sym] += tx["quantity"]
            # Use ``net`` (commission-inclusive) as cost basis to match IB's
            # methodology. Falls back to gross then to qty*price if missing.
            buy_cost[sym] += abs(tx["net"]) or abs(tx["gross"]) or tx["quantity"] * tx["price"]
        elif tx["type"] == "Sell":
            sells[sym] += abs(tx["quantity"])

    all_symbols = set(
        list(buys.keys())
        + list(sells.keys())
        + [tx["symbol"] for tx in transactions if tx["type"] == "Dividend"]
    )
    for sym in current_positions:
        all_symbols.add(sym)

    pre_existing = {}
    for sym in all_symbols:
        csv_net = buys[sym] - sells[sym]
        current_qty = current_positions.get(sym, {}).get("quantity", 0)
        pre_qty = current_qty - csv_net
        if pre_qty > 0.5:
            pre_existing[sym] = round(pre_qty)

    return pre_existing, buys, sells, buy_cost


def build_daily_positions(
    transactions,
    pre_existing,
    current_positions,
    init_costs=None,
    init_costs_usd=None,
    fx_rates=None,
    pre_existing_currencies=None,
):
    """Replay transactions to get position state at each date.

    Pre-existing positions get their *local-currency* cost basis from
    one of three sources, in priority order: an explicit ``init_costs``
    mapping (typically computed upstream as ``qty × first_market_price``),
    then the ``avg_cost`` from ``current_positions``, then 0 (with the
    symbol added to ``needs_cost_init`` so the caller knows it still
    needs filling in).

    Cost basis is also tracked in **USD** in parallel: each Buy adds
    ``abs(tx.net) × fx_at_trade_date`` to ``cost_bases_usd``, each Sell
    proportionally reduces both, and realized P&L is recorded in USD
    using the sale-day FX rate. ``init_costs_usd`` seeds the USD basis
    for pre-existing positions; if absent, falls back to
    ``init_costs[sym] × fx_at_first_trade_date``.

    Why ``init_costs`` matters: when a pre-existing position is partially
    sold during the replay, ``realized_pnl += proceeds − cost_basis × fraction``.
    If cost_basis starts at 0, every partial sell over-credits realized P&L
    by the entire proceeds, and the offsetting unrealized loss collapses to
    zero in a single day when the position eventually closes — manifesting
    as a one-day return spike. Seeding ``init_costs`` from price data
    eliminates that artifact at the source.

    Also tracks per-symbol ``sec_type``, ``multiplier``, and ``currency``
    so options/futures + foreign-currency positions can be handled
    downstream with correct notional math.
    """
    init_costs = init_costs or {}
    init_costs_usd = init_costs_usd or {}
    fx_rates = fx_rates or {}
    pre_existing_currencies = pre_existing_currencies or {}
    all_dates = sorted(set(tx["date"] for tx in transactions))
    if not all_dates:
        return {}, {}, {}, {}, set(), {}, {}, {}, {}

    first_date = all_dates[0]

    def fx_for(ccy: str, dt: str) -> float:
        if ccy == "USD":
            return 1.0
        return fx.rate_on(fx_rates.get(ccy, {}), dt, default=1.0)

    positions = {}
    cost_bases = {}  # local-currency cost basis
    cost_bases_usd = {}  # USD-denominated cost basis (historical, fixed at trade)
    sec_types: dict[str, str] = {}
    multipliers: dict[str, float] = {}
    currencies: dict[str, str] = {}
    needs_cost_init = set()
    for sym, qty in pre_existing.items():
        positions[sym] = qty
        cp = current_positions.get(sym, {})
        avg = cp.get("avg_cost", 0)
        ccy = pre_existing_currencies.get(sym) or cp.get("currency") or "USD"
        currencies[sym] = ccy
        if sym in init_costs:
            cost_bases[sym] = init_costs[sym]
        elif avg > 0:
            cost_bases[sym] = avg * qty
        else:
            cost_bases[sym] = 0
            needs_cost_init.add(sym)
        if sym in init_costs_usd:
            cost_bases_usd[sym] = init_costs_usd[sym]
        else:
            cost_bases_usd[sym] = cost_bases[sym] * fx_for(ccy, first_date)
        sec_types.setdefault(sym, cp.get("sec_type") or "STK")
        multipliers.setdefault(sym, 100.0 if sec_types[sym] == "OPT" else 1.0)

    dividends_cum = defaultdict(float)
    realized_pnl = 0.0  # in USD

    position_snapshots = {}
    cost_snapshots = {}
    cost_usd_snapshots = {}
    dividend_snapshots = {}
    realized_pnl_snapshots = {}

    position_snapshots[first_date] = dict(positions)
    cost_snapshots[first_date] = dict(cost_bases)
    cost_usd_snapshots[first_date] = dict(cost_bases_usd)
    realized_pnl_snapshots[first_date] = 0.0

    tx_by_date = defaultdict(list)
    for tx in transactions:
        tx_by_date[tx["date"]].append(tx)

    for dt in all_dates:
        for tx in tx_by_date[dt]:
            sym = tx["symbol"]
            sec_types.setdefault(sym, tx.get("sec_type", "STK"))
            multipliers.setdefault(sym, tx.get("multiplier", 1.0))
            ccy = tx.get("currency") or "USD"
            currencies.setdefault(sym, ccy)
            fx_t = fx_for(currencies[sym], dt)
            # IB CSV convention: ``price`` is in trade currency; ``gross``,
            # ``commission``, ``net`` are in account base (USD). So local
            # cost = qty × price; USD cost = abs(net). For USD trades the
            # two coincide (and net includes commission); for foreign
            # trades they differ by fx_t and we treat them independently.
            is_usd_trade = ccy == "USD"
            if tx["type"] == "Buy":
                old_qty = positions.get(sym, 0)
                add_qty = tx["quantity"]
                add_cost_usd = abs(tx["net"]) or abs(tx["gross"]) or add_qty * tx["price"] * fx_t
                add_cost_local = add_cost_usd if is_usd_trade else add_qty * tx["price"]
                positions[sym] = old_qty + add_qty
                cost_bases[sym] = cost_bases.get(sym, 0) + add_cost_local
                cost_bases_usd[sym] = cost_bases_usd.get(sym, 0) + add_cost_usd
            elif tx["type"] == "Sell":
                old_qty = positions.get(sym, 0)
                sell_qty = abs(tx["quantity"])
                sell_proceeds_usd = (
                    abs(tx["net"]) or abs(tx["gross"]) or sell_qty * tx["price"] * fx_t
                )
                sell_proceeds_local = sell_proceeds_usd if is_usd_trade else sell_qty * tx["price"]
                if old_qty > 0:
                    sell_fraction = min(sell_qty / old_qty, 1.0)
                    cost_sold_usd = cost_bases_usd.get(sym, 0) * sell_fraction
                    # Realized total = USD proceeds − historical USD cost.
                    # Splits cleanly into stock_pnl_usd = (proceeds−cost_local)*fx_t
                    # plus fx_pnl_usd = cost_local*fx_t − cost_usd_historical.
                    realized_pnl += sell_proceeds_usd - cost_sold_usd
                    cost_bases[sym] = cost_bases.get(sym, 0) * (1 - sell_fraction)
                    cost_bases_usd[sym] = cost_bases_usd.get(sym, 0) * (1 - sell_fraction)
                # `sell_proceeds_local` is unused now but keep the conversion
                # for symmetry / possible future per-leg reporting.
                _ = sell_proceeds_local
                positions[sym] = old_qty - sell_qty
                if positions[sym] <= 0.001:
                    positions.pop(sym, None)
                    cost_bases.pop(sym, None)
                    cost_bases_usd.pop(sym, None)
            elif tx["type"] == "Dividend":
                # Dividends: ``net`` already in USD (account base).
                dividends_cum[sym] += tx["net"]

        position_snapshots[dt] = {s: q for s, q in positions.items() if q > 0.001}
        cost_snapshots[dt] = dict(cost_bases)
        cost_usd_snapshots[dt] = dict(cost_bases_usd)
        dividend_snapshots[dt] = dict(dividends_cum)
        realized_pnl_snapshots[dt] = round(realized_pnl, 2)

    return (
        position_snapshots,
        cost_snapshots,
        cost_usd_snapshots,
        dividend_snapshots,
        realized_pnl_snapshots,
        needs_cost_init,
        sec_types,
        multipliers,
        currencies,
    )


def fetch_yahoo_prices(sym, yahoo_ticker, start_date, end_date):
    """Fetch historical daily close prices from Yahoo Finance."""
    try:
        ticker = yf.Ticker(yahoo_ticker)
        df = ticker.history(start=start_date, end=end_date, auto_adjust=True)
        if df.empty:
            return {}
        result = {}
        for dt, row in df.iterrows():
            date_str = dt.strftime("%Y-%m-%d")
            result[date_str] = round(float(row["Close"]), 4)
        return result
    except Exception as e:
        log.warning(f"    Yahoo fetch for {sym} ({yahoo_ticker}): {e}")
        return {}


# Map IB symbols to Yahoo Finance tickers for non-US stocks
YAHOO_TICKER_MAP = {
    "CSU": "CSU.TO",
    "RHM": "RHM.DE",
    "MC": "MC.PA",
    "ORSTED": "ORSTED.CO",
    "PSKY": "PSKY.L",
}

# Symbols to skip entirely (options, futures, warrants — no meaningful daily price)
SKIP_SYMBOLS = {"M6E", "OXY WS", "NVDA", "PLTR", "QQQ"}


def fetch_all_historical_prices(symbols, start_date, end_date):
    """Fetch historical daily prices from IB API, with Yahoo Finance as fallback."""
    API = os.environ.get("AIINVEST_API", "http://localhost:8000")
    prices = {}

    ib_symbols = [s for s in symbols if s not in SKIP_SYMBOLS and s not in YAHOO_TICKER_MAP]
    yahoo_symbols = [s for s in symbols if s in YAHOO_TICKER_MAP]

    # Fetch from IB
    for i, sym in enumerate(ib_symbols):
        log.info(f"  [IB {i + 1}/{len(ib_symbols)}] Fetching {sym}...")
        try:
            url = f"{API}/api/market/history/{sym}?period=5y"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=60) as resp:
                bars = json.loads(resp.read())
            if bars:
                prices[sym] = {b["date"]: b["close"] for b in bars}
                log.info(f"    {sym}: {len(bars)} bars")
            else:
                log.info(f"    {sym}: no data from IB, trying Yahoo...")
                yahoo_symbols.append(sym)
        except Exception as e:
            log.warning(f"    {sym}: IB error {e}, trying Yahoo...")
            yahoo_symbols.append(sym)

    # Fetch from Yahoo Finance
    for i, sym in enumerate(yahoo_symbols):
        yahoo_ticker = YAHOO_TICKER_MAP.get(sym, sym)
        log.info(f"  [Yahoo {i + 1}/{len(yahoo_symbols)}] Fetching {sym} as {yahoo_ticker}...")
        yprices = fetch_yahoo_prices(sym, yahoo_ticker, start_date, end_date)
        if yprices:
            prices[sym] = yprices
            log.info(f"    {sym}: {len(yprices)} bars from Yahoo")
        else:
            log.warning(f"    {sym}: no data from Yahoo either")

    return prices


def get_trading_days(prices):
    """Extract all unique trading days from price data."""
    all_days = set()
    for sym_prices in prices.values():
        all_days.update(sym_prices.keys())
    return sorted(all_days)


def generate_snapshots(
    position_snapshots,
    cost_snapshots,
    cost_usd_snapshots,
    dividend_snapshots,
    realized_pnl_snapshots,
    prices,
    sec_types=None,
    multipliers=None,
    currencies=None,
    fx_rates=None,
):
    """Generate daily snapshot rows for every trading day.

    Cost bases must already be properly seeded (see ``build_daily_positions``
    + the ``init_costs`` arguments) — this function only computes derived
    fields per snapshot date and does not back-patch missing costs.

    For each row, both *local-currency* fields (``market_value``,
    ``cost_basis``, ``day_pnl``) and *USD* fields
    (``market_value_usd``, ``cost_basis_usd``, ``stock_pnl_usd``,
    ``fx_pnl_usd``) are emitted. The USD fields are what aggregation
    sums across positions; local fields are kept so the dashboard can
    still show the symbol's "native" valuation. Decomposition:

        stock_pnl_usd = (mv_local − cost_local) × fx_t
        fx_pnl_usd    = mv_usd − cost_basis_usd − stock_pnl_usd
                      = cost_local × fx_t − cost_basis_usd

    Includes a __REALIZED__ pseudo-row carrying USD-denominated realized
    P&L. Per-symbol ``sec_type`` and ``multiplier`` flow through so
    options show the contract qty + per-share price + multiplier
    (mv = qty * mult * price).
    """
    sec_types = sec_types or {}
    multipliers = multipliers or {}
    currencies = currencies or {}
    fx_rates = fx_rates or {}
    tx_dates = sorted(position_snapshots.keys())
    if not tx_dates:
        return {}

    def fx_for(ccy: str, dt: str) -> float:
        if ccy == "USD":
            return 1.0
        return fx.rate_on(fx_rates.get(ccy, {}), dt, default=1.0)

    trading_days = get_trading_days(prices)
    if not trading_days:
        trading_days = tx_dates

    first_tx_date = tx_dates[0]
    trading_days = [d for d in trading_days if d >= first_tx_date]
    all_days = sorted(set(trading_days + tx_dates))

    snapshots = {}
    current_positions = {}
    current_costs = {}
    current_costs_usd = {}
    current_divs = {}
    current_realized = 0.0

    for dt in all_days:
        if dt in position_snapshots:
            current_positions = position_snapshots[dt]
            current_costs = cost_snapshots[dt]
            current_costs_usd = cost_usd_snapshots.get(dt, current_costs_usd)
            current_divs = dividend_snapshots.get(dt, current_divs)
            current_realized = realized_pnl_snapshots.get(dt, current_realized)

        rows = []
        for sym, qty in sorted(current_positions.items()):
            if qty <= 0.001:
                continue

            price = 0
            if sym in prices and dt in prices[sym]:
                price = prices[sym][dt]
            elif sym in prices:
                available = sorted(prices[sym].keys())
                earlier = [d for d in available if d <= dt]
                if earlier:
                    price = prices[sym][earlier[-1]]

            cost_basis = current_costs.get(sym, 0)
            cost_basis_usd = current_costs_usd.get(sym, cost_basis)
            mult = multipliers.get(sym, 1.0)
            sec_type = sec_types.get(sym, "STK")
            ccy = currencies.get(sym, "USD")
            fx_t = fx_for(ccy, dt)
            avg_cost_per_share = cost_basis / (qty * mult) if qty and mult else 0

            if not price:
                price = avg_cost_per_share

            mv = round(qty * mult * price, 2)
            pnl = round(mv - cost_basis, 2)
            mv_usd = mv * fx_t
            stock_pnl_usd = (mv - cost_basis) * fx_t
            total_pnl_usd = mv_usd - cost_basis_usd
            fx_pnl_usd = total_pnl_usd - stock_pnl_usd
            div_cum = round(current_divs.get(sym, 0), 2)  # already in USD
            total_ret = round(total_pnl_usd + div_cum, 2)

            rows.append(
                {
                    "symbol": sym,
                    "quantity": qty,
                    "market_price": round(price, 4),
                    "market_value": mv,
                    "day_pnl": pnl,
                    "cost_basis": round(cost_basis, 2),
                    "dividends_cumulative": div_cum,
                    "total_return": total_ret,
                    "sec_type": sec_type,
                    "multiplier": mult,
                    "currency": ccy,
                    "fx_rate": round(fx_t, 6),
                    "market_value_usd": round(mv_usd, 2),
                    "cost_basis_usd": round(cost_basis_usd, 2),
                    "stock_pnl_usd": round(stock_pnl_usd, 2),
                    "fx_pnl_usd": round(fx_pnl_usd, 2),
                }
            )

        # Add a pseudo-row to track cumulative realized P&L (already USD)
        if current_realized != 0:
            rows.append(
                {
                    "symbol": "__REALIZED__",
                    "quantity": 0,
                    "market_price": 0,
                    "market_value": 0,
                    "day_pnl": round(current_realized, 2),
                    "cost_basis": 0,
                    "dividends_cumulative": 0,
                    "total_return": round(current_realized, 2),
                    "sec_type": "PSEUDO",
                    "multiplier": 1,
                    "currency": "USD",
                    "fx_rate": 1.0,
                    "market_value_usd": 0,
                    "cost_basis_usd": 0,
                    "stock_pnl_usd": round(current_realized, 2),
                    "fx_pnl_usd": 0,
                }
            )

        if rows:
            snapshots[dt] = rows

    return snapshots


def inject_fx_pnl_from_statements(snapshots):
    """Add cumulative ``__FX__`` pseudo-rows from any IB Activity Statements.

    Each statement contributes ``fx_realized + fx_unrealized`` for its
    ``period_end`` date. We accumulate across statements (sorted by end date)
    so the per-snapshot value is the running total — analogous to how
    ``__REALIZED__`` accumulates.
    """
    statements = ib_statement.find_statements(DATA_DIR)
    if not statements:
        return

    parsed = sorted((ib_statement.parse(p) for p in statements), key=lambda s: s.period_end)
    cumulative = 0.0
    for st in parsed:
        cumulative += st.fx_realized + st.fx_unrealized
        if st.period_end not in snapshots:
            log.warning(f"  FX P/L: no snapshot for statement period_end={st.period_end}; skipping")
            continue
        snapshots[st.period_end].append(
            {
                "symbol": "__FX__",
                "quantity": 0,
                "market_price": 0,
                "market_value": 0,
                "day_pnl": round(cumulative, 2),
                "cost_basis": 0,
                "dividends_cumulative": 0,
                "total_return": round(cumulative, 2),
                "sec_type": "PSEUDO",
                "multiplier": 1,
                "currency": "USD",
                "fx_rate": 1.0,
                "market_value_usd": 0,
                "cost_basis_usd": 0,
                "stock_pnl_usd": 0,
                "fx_pnl_usd": round(cumulative, 2),
            }
        )
        log.info(
            f"  FX P/L: {st.period_end} += realized {st.fx_realized:+.2f} + "
            f"unrealized {st.fx_unrealized:+.2f}  (cumulative {cumulative:+.2f})"
        )


def save_all_to_db(snapshots, dividends_list, trades_list=None):
    db = sqlite3.connect(str(DB_PATH))

    # Forward-compatible column adds (idempotent — sqlite raises if column exists).
    for col, decl in [
        ("sec_type", "TEXT DEFAULT 'STK'"),
        ("multiplier", "REAL DEFAULT 1"),
        ("currency", "TEXT DEFAULT 'USD'"),
        ("fx_rate", "REAL DEFAULT 1.0"),
        ("market_value_usd", "REAL"),
        ("cost_basis_usd", "REAL"),
        ("stock_pnl_usd", "REAL"),
        ("fx_pnl_usd", "REAL"),
    ]:
        with contextlib.suppress(sqlite3.OperationalError):
            db.execute(f"ALTER TABLE snapshots ADD COLUMN {col} {decl}")

    db.execute("DELETE FROM snapshots")
    for dt, rows in sorted(snapshots.items()):
        for r in rows:
            db.execute(
                """
                INSERT OR REPLACE INTO snapshots
                (date, symbol, quantity, market_price, market_value, day_pnl, cost_basis,
                 dividends_cumulative, total_return, sec_type, multiplier,
                 currency, fx_rate, market_value_usd, cost_basis_usd,
                 stock_pnl_usd, fx_pnl_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    dt,
                    r["symbol"],
                    r["quantity"],
                    r["market_price"],
                    r["market_value"],
                    r["day_pnl"],
                    r["cost_basis"],
                    r.get("dividends_cumulative", 0),
                    r.get("total_return", 0),
                    r.get("sec_type", "STK"),
                    r.get("multiplier", 1),
                    r.get("currency", "USD"),
                    r.get("fx_rate", 1.0),
                    r.get("market_value_usd"),
                    r.get("cost_basis_usd"),
                    r.get("stock_pnl_usd"),
                    r.get("fx_pnl_usd"),
                ),
            )

    db.execute("DELETE FROM dividends")
    for d in dividends_list:
        db.execute(
            """
            INSERT OR IGNORE INTO dividends (date, symbol, amount, description)
            VALUES (?, ?, ?, ?)
        """,
            (d["date"], d["symbol"], d["amount"], d.get("description", "")),
        )

    if trades_list:
        for t in trades_list:
            db.execute(
                """
                INSERT OR IGNORE INTO trades (trade_date, symbol, description, asset_class,
                    action, quantity, price, currency, commission, net_amount,
                    exchange, order_type, account, trade_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    t["trade_date"],
                    t["symbol"],
                    t["description"],
                    t["asset_class"],
                    t["action"],
                    t["quantity"],
                    t["price"],
                    t["currency"],
                    t["commission"],
                    t["net_amount"],
                    t["exchange"],
                    t["order_type"],
                    t["account"],
                    t["trade_id"],
                ),
            )

    db.commit()

    row = db.execute("SELECT COUNT(DISTINCT date) as cnt FROM snapshots").fetchone()
    log.info(f"Saved {row[0]} snapshot dates")
    row = db.execute("SELECT COUNT(*) as cnt FROM dividends").fetchone()
    log.info(f"Saved {row[0]} dividend records")
    row = db.execute("SELECT COUNT(*) as cnt FROM trades").fetchone()
    log.info(f"Saved {row[0]} trade records")

    db.close()


def save_snapshot_csvs(snapshots):
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    for existing in SNAPSHOTS_DIR.glob("*.csv"):
        existing.unlink()

    fieldnames = [
        "date",
        "symbol",
        "quantity",
        "market_price",
        "market_value",
        "day_pnl",
        "cost_basis",
        "dividends_cumulative",
        "total_return",
        "sec_type",
        "multiplier",
        "currency",
        "fx_rate",
        "market_value_usd",
        "cost_basis_usd",
        "stock_pnl_usd",
        "fx_pnl_usd",
    ]
    defaults = {"sec_type": "STK", "multiplier": 1, "currency": "USD", "fx_rate": 1.0}

    for dt, rows in sorted(snapshots.items()):
        csv_path = SNAPSHOTS_DIR / f"{dt}.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                row = {"date": dt}
                for k in fieldnames:
                    if k == "date":
                        continue
                    row[k] = r.get(k, defaults.get(k, 0))
                writer.writerow(row)


def compute_purchase_dates_and_costs(transactions, pre_existing):
    """Compute first purchase date and weighted avg cost per symbol from CSV."""
    first_date = sorted(set(t["date"] for t in transactions))[0] if transactions else "2023-01-31"

    purchase_dates = {}
    buy_dates_per_sym = defaultdict(set)
    cost_lots = defaultdict(lambda: {"qty": 0, "cost": 0})

    for sym in pre_existing:
        purchase_dates[sym] = first_date
        buy_dates_per_sym[sym].add(first_date)

    for tx in sorted(transactions, key=lambda t: t["date"]):
        sym = tx["symbol"]
        if tx["type"] == "Buy":
            if sym not in purchase_dates:
                purchase_dates[sym] = tx["date"]
            buy_dates_per_sym[sym].add(tx["date"])
            lot = cost_lots[sym]
            add_cost = abs(tx["gross"]) if tx["gross"] else tx["quantity"] * tx["price"]
            lot["qty"] += tx["quantity"]
            lot["cost"] += add_cost
        elif tx["type"] == "Sell":
            lot = cost_lots[sym]
            sell_qty = abs(tx["quantity"])
            if lot["qty"] > 0:
                fraction = min(sell_qty / lot["qty"], 1.0)
                lot["cost"] *= 1 - fraction
                lot["qty"] -= sell_qty
                if lot["qty"] <= 0.001:
                    lot["qty"] = 0
                    lot["cost"] = 0

    # Mark symbols with multiple buy dates as "Multiple"
    for sym, dates in buy_dates_per_sym.items():
        if len(dates) > 1 and sym in purchase_dates:
            purchase_dates[sym] = "Multiple"

    avg_costs = {}
    for sym, lot in cost_lots.items():
        if lot["qty"] > 0:
            avg_costs[sym] = round(lot["cost"] / lot["qty"], 4)

    return purchase_dates, avg_costs


def update_positions_from_csv(dividend_snapshots, purchase_dates, avg_costs):
    """Update positions table with dividends, purchase dates, and cost basis from CSV."""
    divs = {}
    if dividend_snapshots:
        last_date = sorted(dividend_snapshots.keys())[-1]
        divs = dividend_snapshots[last_date]

    db = sqlite3.connect(str(DB_PATH))
    updated = 0
    for row in db.execute("SELECT symbol FROM positions").fetchall():
        sym = row[0]
        div_cum = round(divs.get(sym, 0), 2)
        pdate = purchase_dates.get(sym)
        avg = avg_costs.get(sym)

        updates = ["dividends_cumulative = ?", "total_return = COALESCE(unrealized_pnl, 0) + ?"]
        params = [div_cum, div_cum]

        if pdate:
            updates.append("purchase_date = ?")
            params.append(pdate)
        if avg:
            updates.append("avg_cost = ?")
            params.append(avg)

        params.append(sym)
        db.execute(f"UPDATE positions SET {', '.join(updates)} WHERE symbol = ?", params)
        updated += 1

    db.commit()
    db.close()
    log.info(f"Updated {updated} positions with purchase dates, costs, and dividends")


def main():
    log.info("Parsing transactions...")
    transactions = parse_transactions(CSV_PATH)
    log.info(f"Found {len(transactions)} transactions")

    buys = [t for t in transactions if t["type"] == "Buy"]
    sells = [t for t in transactions if t["type"] == "Sell"]
    divs = [t for t in transactions if t["type"] == "Dividend"]
    log.info(f"  Buys: {len(buys)}, Sells: {len(sells)}, Dividends: {len(divs)}")

    current_positions = get_current_positions()
    log.info(f"Current positions: {len(current_positions)}")

    pre_existing, _, _, _ = compute_position_history(transactions, current_positions)
    log.info(f"Pre-existing positions: {pre_existing}")

    # Two-pass build: fetch prices first so we can seed pre-existing cost
    # bases at the day-1 market price. Without this, partial sells of
    # pre-existing positions over-credit __REALIZED__ by the full proceeds
    # (because cost_sold = 0 × fraction = 0), and the offsetting unrealized
    # loss collapses in one day when the position closes — producing fake
    # one-day return spikes of double-digit percent.
    tx_stock_syms = {t["symbol"] for t in transactions if t.get("sec_type", "STK") == "STK"}
    price_symbols = sorted(set(pre_existing) | tx_stock_syms)
    first_tx_date = sorted(set(t["date"] for t in transactions))[0]
    end_date = date.today().isoformat()

    log.info(
        f"Fetching historical prices for {len(price_symbols)} stock symbols from {first_tx_date} to {end_date}..."
    )
    prices = fetch_all_historical_prices(price_symbols, first_tx_date, end_date)
    log.info(f"Got prices for {len(prices)} symbols")

    # Per-symbol currency: prefer trade-CSV currency (authoritative), fall back
    # to live-positions currency for pre-existing symbols with no trade.
    sym_currency: dict[str, str] = {}
    for tx in transactions:
        sym_currency.setdefault(tx["symbol"], tx.get("currency") or "USD")
    for sym, cp in current_positions.items():
        sym_currency.setdefault(sym, cp.get("currency") or "USD")
    pre_existing_currencies = {sym: sym_currency.get(sym, "USD") for sym in pre_existing}

    needed_currencies = {c for c in sym_currency.values() if c and c != "USD" and c in fx._YAHOO_FX}
    fx_rates: dict[str, dict[str, float]] = {}
    if needed_currencies:
        log.info(f"Fetching FX rates for currencies {sorted(needed_currencies)}...")
        fx_rates = fx.fetch_fx_rates(needed_currencies, first_tx_date, end_date)
        for ccy, series in fx_rates.items():
            log.info(f"  FX {ccy}USD: {len(series)} bars")

    def fx_seed(ccy: str, dt: str) -> float:
        if ccy == "USD":
            return 1.0
        return fx.rate_on(fx_rates.get(ccy, {}), dt, default=1.0)

    # Pre-compute total buy quantity & local cost per symbol from the
    # transaction history — needed to back out the pre-existing-position
    # seed cost from IB's reported avg_cost.
    buy_qty_local: dict[str, float] = defaultdict(float)
    buy_cost_local: dict[str, float] = defaultdict(float)
    for tx in transactions:
        if tx["type"] != "Buy":
            continue
        sym = tx["symbol"]
        ccy = tx.get("currency") or "USD"
        buy_qty_local[sym] += tx["quantity"]
        if ccy == "USD":
            # USD trades: ``net`` already in account base = local; includes commission.
            buy_cost_local[sym] += abs(tx["net"]) or abs(tx["quantity"] * tx["price"])
        else:
            # Foreign trades: ``net`` is USD; use price × qty in local ccy.
            buy_cost_local[sym] += tx["quantity"] * tx["price"]

    init_costs = {}
    init_costs_usd = {}
    for sym, qty in pre_existing.items():
        ccy = pre_existing_currencies.get(sym, "USD")
        cp = current_positions.get(sym, {})
        ib_avg = cp.get("avg_cost") or 0
        ib_qty = cp.get("quantity") or 0
        # Preferred path: derive pre-existing cost from IB avg_cost using
        # average-cost-method invariance (avg_cost survives sells unchanged):
        #   initial_cost = ib_avg × (pre_qty + total_buy_qty) - total_buy_cost
        if ib_avg > 0 and ib_qty > 0:
            implied_total_cost_local = ib_avg * (qty + buy_qty_local.get(sym, 0))
            local_cost = round(max(0.0, implied_total_cost_local - buy_cost_local.get(sym, 0)), 2)
            init_costs[sym] = local_cost
            init_costs_usd[sym] = round(local_cost * fx_seed(ccy, first_tx_date), 2)
            log.info(
                f"  Init cost for {sym} (from IB avg): {qty} × ~{(local_cost / qty if qty else 0):.2f} {ccy}"
                f" = {local_cost:.2f} {ccy} (≈ ${init_costs_usd[sym]:.2f})"
            )
            continue
        # Fallback: use day-1 market price × pre_existing_qty.
        if sym not in prices:
            continue
        available = sorted(prices[sym].keys())
        near = [d for d in available if d <= first_tx_date]
        px = prices[sym][near[-1]] if near else (prices[sym][available[0]] if available else 0)
        if px > 0:
            local_cost = round(px * qty, 2)
            init_costs[sym] = local_cost
            init_costs_usd[sym] = round(local_cost * fx_seed(ccy, first_tx_date), 2)
            log.info(
                f"  Init cost for {sym} (price fallback): {qty} × {px:.2f} {ccy}"
                f" = {local_cost:.2f} {ccy} (≈ ${init_costs_usd[sym]:.2f})"
            )

    log.info("Building daily position history...")
    (
        position_snaps,
        cost_snaps,
        cost_usd_snaps,
        dividend_snaps,
        realized_pnl_snaps,
        needs_cost_init,
        sec_types,
        multipliers,
        currencies,
    ) = build_daily_positions(
        transactions,
        pre_existing,
        current_positions,
        init_costs=init_costs,
        init_costs_usd=init_costs_usd,
        fx_rates=fx_rates,
        pre_existing_currencies=pre_existing_currencies,
    )
    log.info(f"Position snapshots for {len(position_snaps)} dates")
    if needs_cost_init:
        log.warning(
            f"Positions still without cost basis after price seeding: {sorted(needs_cost_init)}"
        )

    log.info("Generating snapshots...")
    snapshots = generate_snapshots(
        position_snaps,
        cost_snaps,
        cost_usd_snaps,
        dividend_snaps,
        realized_pnl_snaps,
        prices,
        sec_types=sec_types,
        multipliers=multipliers,
        currencies=currencies,
        fx_rates=fx_rates,
    )
    log.info(f"Generated {len(snapshots)} snapshot dates")

    inject_fx_pnl_from_statements(snapshots)

    dividends_list = [
        {
            "date": t["date"],
            "symbol": t["symbol"],
            "amount": t["net"],
            "description": t["description"],
        }
        for t in transactions
        if t["type"] == "Dividend"
    ]

    trades_list = []
    for tx in transactions:
        if tx["type"] in ("Buy", "Sell"):
            trades_list.append(
                {
                    "trade_date": tx["date"],
                    "symbol": tx["symbol"],
                    "description": tx["description"],
                    "asset_class": tx.get("sec_type", "STK"),
                    "action": tx["type"].upper(),
                    "quantity": tx["quantity"],
                    "price": tx["price"],
                    "currency": tx["currency"],
                    "commission": abs(tx["commission"]),
                    "net_amount": round(tx["net"], 2),
                    "exchange": "",
                    "order_type": "",
                    "account": tx["account"],
                    "trade_id": f"{tx['date']}_{tx['symbol']}_{tx['quantity']}_{tx['price']}",
                }
            )

    log.info("Saving to database...")
    save_all_to_db(snapshots, dividends_list, trades_list)

    log.info("Saving CSV files...")
    save_snapshot_csvs(snapshots)

    log.info("Computing purchase dates and costs from CSV...")
    purchase_dates, avg_costs = compute_purchase_dates_and_costs(transactions, pre_existing)
    for sym in sorted(purchase_dates):
        cost_str = f"  avg_cost=${avg_costs[sym]:,.4f}" if sym in avg_costs else ""
        log.info(f"  {sym}: purchased={purchase_dates[sym]}{cost_str}")

    log.info("Updating positions...")
    update_positions_from_csv(dividend_snaps, purchase_dates, avg_costs)

    log.info("Done!")

    last_snap = sorted(snapshots.keys())[-1] if snapshots else None
    if last_snap:
        total_mv = sum((r.get("market_value_usd") or 0) for r in snapshots[last_snap])
        total_stock = sum((r.get("stock_pnl_usd") or 0) for r in snapshots[last_snap])
        total_fx = sum((r.get("fx_pnl_usd") or 0) for r in snapshots[last_snap])
        total_div = sum(r["dividends_cumulative"] for r in snapshots[last_snap])
        total_ret = sum(r["total_return"] for r in snapshots[last_snap])
        log.info(f"Latest snapshot ({last_snap}) — all USD:")
        log.info(f"  Market Value: ${total_mv:,.2f}")
        log.info(f"  Stock P&L:    ${total_stock:,.2f}")
        log.info(f"  FX P&L:       ${total_fx:,.2f}")
        log.info(f"  Dividends:    ${total_div:,.2f}")
        log.info(f"  Total Return: ${total_ret:,.2f}")


if __name__ == "__main__":
    main()

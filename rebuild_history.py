#!/usr/bin/env python3
"""
Rebuild portfolio snapshots from IB transaction history CSV.
Parses buys/sells/dividends, infers pre-existing positions,
fetches historical prices from IB, and generates daily snapshots.
"""

import csv
import json
import logging
import sqlite3
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path

import yfinance as yf

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "trading.db"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
CSV_PATH = DATA_DIR / "U640574.TRANSACTIONS.20221230.20260306.csv"

SYMBOL_MAP = {
    "M6EM6": "M6E",
    "BIPC.OLD": "BIPC",
}


def parse_float(s):
    if not s or s == "-":
        return 0.0
    s = s.strip().replace("(1)", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


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

            symbol = symbol.strip()
            if " " in symbol and len(symbol) > 10:
                base = symbol.split()[0]
                symbol = base

            symbol = SYMBOL_MAP.get(symbol, symbol)
            if not symbol or symbol in SKIP_SYMBOLS:
                continue

            transactions.append(
                {
                    "date": dt.strip(),
                    "account": account.strip(),
                    "description": description.strip(),
                    "type": tx_type.strip(),
                    "symbol": symbol,
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
    rows = db.execute("SELECT symbol, quantity, avg_cost, sec_type FROM positions").fetchall()
    db.close()
    return {
        r["symbol"]: {
            "quantity": r["quantity"],
            "avg_cost": r["avg_cost"],
            "sec_type": r["sec_type"],
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
            buy_cost[sym] += abs(tx["gross"]) if tx["gross"] else tx["quantity"] * tx["price"]
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


def build_daily_positions(transactions, pre_existing, current_positions):
    """Replay transactions to get position state at each date.

    For pre-existing positions with unknown cost, cost_basis is set to 0
    and will be filled in by generate_snapshots using market price on first date.
    Tracks realized P&L from sales to preserve gains/losses of closed positions.
    """
    all_dates = sorted(set(tx["date"] for tx in transactions))
    if not all_dates:
        return {}, {}, {}, {}

    first_date = all_dates[0]

    positions = {}
    cost_bases = {}
    needs_cost_init = set()  # pre-existing symbols needing cost from first price
    for sym, qty in pre_existing.items():
        positions[sym] = qty
        cp = current_positions.get(sym, {})
        avg = cp.get("avg_cost", 0)
        if avg > 0:
            cost_bases[sym] = avg * qty
        else:
            cost_bases[sym] = 0
            needs_cost_init.add(sym)

    dividends_cum = defaultdict(float)
    realized_pnl = 0.0  # cumulative realized P&L from closed positions

    position_snapshots = {}
    cost_snapshots = {}
    dividend_snapshots = {}
    realized_pnl_snapshots = {}

    position_snapshots[first_date] = dict(positions)
    cost_snapshots[first_date] = dict(cost_bases)
    realized_pnl_snapshots[first_date] = 0.0

    tx_by_date = defaultdict(list)
    for tx in transactions:
        tx_by_date[tx["date"]].append(tx)

    for dt in all_dates:
        for tx in tx_by_date[dt]:
            sym = tx["symbol"]
            if tx["type"] == "Buy":
                old_qty = positions.get(sym, 0)
                add_qty = tx["quantity"]
                add_cost = abs(tx["gross"]) if tx["gross"] else add_qty * tx["price"]
                positions[sym] = old_qty + add_qty
                cost_bases[sym] = cost_bases.get(sym, 0) + add_cost
            elif tx["type"] == "Sell":
                old_qty = positions.get(sym, 0)
                sell_qty = abs(tx["quantity"])
                sell_proceeds = abs(tx["gross"]) if tx["gross"] else sell_qty * tx["price"]
                if old_qty > 0:
                    sell_fraction = min(sell_qty / old_qty, 1.0)
                    cost_sold = cost_bases.get(sym, 0) * sell_fraction
                    realized_pnl += sell_proceeds - cost_sold
                    cost_bases[sym] = cost_bases.get(sym, 0) * (1 - sell_fraction)
                positions[sym] = old_qty - sell_qty
                if positions[sym] <= 0.001:
                    positions.pop(sym, None)
                    cost_bases.pop(sym, None)
            elif tx["type"] == "Dividend":
                dividends_cum[sym] += tx["net"]

        position_snapshots[dt] = {s: q for s, q in positions.items() if q > 0.001}
        cost_snapshots[dt] = dict(cost_bases)
        dividend_snapshots[dt] = dict(dividends_cum)
        realized_pnl_snapshots[dt] = round(realized_pnl, 2)

    return (
        position_snapshots,
        cost_snapshots,
        dividend_snapshots,
        realized_pnl_snapshots,
        needs_cost_init,
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
    API = "http://localhost:8000"
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
    dividend_snapshots,
    realized_pnl_snapshots,
    needs_cost_init,
    prices,
):
    """Generate daily snapshot rows for every trading day.

    For pre-existing positions with unknown cost, uses the market price on
    first appearance as cost basis (so P&L starts at 0 for those).
    Includes a __REALIZED__ pseudo-row to track cumulative realized P&L.
    """
    tx_dates = sorted(position_snapshots.keys())
    if not tx_dates:
        return {}

    trading_days = get_trading_days(prices)
    if not trading_days:
        trading_days = tx_dates

    first_tx_date = tx_dates[0]
    trading_days = [d for d in trading_days if d >= first_tx_date]
    all_days = sorted(set(trading_days + tx_dates))

    # Resolve initial cost for pre-existing positions with unknown cost
    # Use market price on first available date as cost basis (P&L starts at 0)
    cost_inits = {}
    for sym in needs_cost_init:
        qty = position_snapshots.get(first_tx_date, {}).get(sym, 0)
        if qty <= 0:
            continue
        price = 0
        if sym in prices:
            available = sorted(prices[sym].keys())
            near = [d for d in available if d <= first_tx_date]
            if near:
                price = prices[sym][near[-1]]
            elif available:
                price = prices[sym][available[0]]
        if price > 0:
            cost_inits[sym] = round(price * qty, 2)
            log.info(f"  Init cost for {sym}: {qty} × {price:.2f} = {cost_inits[sym]:.2f}")

    # Patch cost_snapshots for the initial dates
    for dt in tx_dates:
        for sym, cost in cost_inits.items():
            if sym in cost_snapshots.get(dt, {}) and cost_snapshots[dt][sym] == 0:
                cost_snapshots[dt][sym] = cost

    snapshots = {}
    current_positions = {}
    current_costs = {}
    current_divs = {}
    current_realized = 0.0

    for dt in all_days:
        if dt in position_snapshots:
            current_positions = position_snapshots[dt]
            current_costs = cost_snapshots[dt]
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
            avg_cost = cost_basis / qty if qty else 0

            if not price:
                price = avg_cost

            mv = round(qty * price, 2)
            pnl = round(mv - cost_basis, 2)
            div_cum = round(current_divs.get(sym, 0), 2)
            total_ret = round(pnl + div_cum, 2)

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
                }
            )

        # Add a pseudo-row to track cumulative realized P&L
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
                }
            )

        if rows:
            snapshots[dt] = rows

    return snapshots


def save_all_to_db(snapshots, dividends_list, trades_list=None):
    db = sqlite3.connect(str(DB_PATH))

    db.execute("DELETE FROM snapshots")
    for dt, rows in sorted(snapshots.items()):
        for r in rows:
            db.execute(
                """
                INSERT OR REPLACE INTO snapshots
                (date, symbol, quantity, market_price, market_value, day_pnl, cost_basis,
                 dividends_cumulative, total_return)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    ]

    for dt, rows in sorted(snapshots.items()):
        csv_path = SNAPSHOTS_DIR / f"{dt}.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow({"date": dt, **{k: r.get(k, 0) for k in fieldnames if k != "date"}})


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

    log.info("Building daily position history...")
    position_snaps, cost_snaps, dividend_snaps, realized_pnl_snaps, needs_cost_init = (
        build_daily_positions(transactions, pre_existing, current_positions)
    )
    log.info(f"Position snapshots for {len(position_snaps)} dates")
    if needs_cost_init:
        log.info(f"Positions needing cost init from price: {sorted(needs_cost_init)}")

    all_symbols = set()
    for dt_positions in position_snaps.values():
        all_symbols.update(dt_positions.keys())
    log.info(f"Unique symbols across history: {sorted(all_symbols)}")

    all_dates = sorted(position_snaps.keys())
    start_date = all_dates[0] if all_dates else "2023-01-31"
    end_date = date.today().isoformat()

    log.info(
        f"Fetching historical prices for {len(all_symbols)} symbols from {start_date} to {end_date}..."
    )
    prices = fetch_all_historical_prices(sorted(all_symbols), start_date, end_date)
    log.info(f"Got prices for {len(prices)} symbols")

    log.info("Generating snapshots...")
    snapshots = generate_snapshots(
        position_snaps, cost_snaps, dividend_snaps, realized_pnl_snaps, needs_cost_init, prices
    )
    log.info(f"Generated {len(snapshots)} snapshot dates")

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
                    "asset_class": "STK",
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
        total_mv = sum(r["market_value"] for r in snapshots[last_snap])
        total_pnl = sum(r["day_pnl"] for r in snapshots[last_snap])
        total_div = sum(r["dividends_cumulative"] for r in snapshots[last_snap])
        total_ret = sum(r["total_return"] for r in snapshots[last_snap])
        log.info(f"Latest snapshot ({last_snap}):")
        log.info(f"  Market Value: ${total_mv:,.2f}")
        log.info(f"  P&L:          ${total_pnl:,.2f}")
        log.info(f"  Dividends:    ${total_div:,.2f}")
        log.info(f"  Total Return: ${total_ret:,.2f}")


if __name__ == "__main__":
    main()

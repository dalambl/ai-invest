import asyncio
import json
import csv
import io
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse

import db
from ibkr import ib_conn

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
DATA_DIR = Path(__file__).parent / "data"

YAHOO_TICKER_MAP = {
    "CSU": "CSU.TO",
    "RHM": "RHM.DE",
    "MC": "MC.PA",
    "ORSTED": "ORSTED.CO",
    "PSKY": "PSKY.L",
}

def fetch_yahoo_price(sym):
    """Fetch latest close price from Yahoo Finance (synchronous)."""
    try:
        import yfinance as yf
        yahoo_ticker = YAHOO_TICKER_MAP.get(sym, sym)
        ticker = yf.Ticker(yahoo_ticker)
        df = ticker.history(period="5d", auto_adjust=True)
        if not df.empty:
            return round(float(df["Close"].iloc[-1]), 4)
    except Exception as e:
        log.warning(f"Yahoo price fetch for {sym}: {e}")
    return None


async def enrich_positions_from_db(positions):
    """Fix positions where IB didn't provide a real price (market_price == avg_cost).

    Uses the latest known-good snapshot price as fallback and recomputes
    market_value/unrealized_pnl. Also merges purchase_date, dividends_cumulative,
    and total_return from DB.
    """
    existing = await db.get_positions()
    existing_by_sym = {p["symbol"]: p for p in existing}
    div_cum = await db.get_dividends_cumulative_by_symbol()

    # Find symbols that need price fixing: STK where price == avg_cost, or any with pnl=0
    needs_fix = set()
    for p in positions:
        sym = p["symbol"]
        avg_cost = p["avg_cost"]
        sec_type = p.get("sec_type", "STK")
        if sec_type in ("STK", "ETF"):
            if abs(p["market_price"] - avg_cost) < 0.001 and avg_cost > 0:
                needs_fix.add(sym)
        # Options/futures: skip — their prices are per-share with multiplier
        # and we don't have the multiplier here to recompute correctly

    # For symbols needing fix, find the latest snapshot with a real price
    fallback_prices = {}
    if needs_fix:
        fallback_prices = await db.get_latest_known_prices(needs_fix)

    # For symbols still without a fallback price, try Yahoo Finance
    still_missing = needs_fix - set(fallback_prices.keys())
    if still_missing:
        loop = asyncio.get_event_loop()
        for sym in still_missing:
            try:
                price = await loop.run_in_executor(None, fetch_yahoo_price, sym)
                if price and price > 0:
                    fallback_prices[sym] = price
                    log.info(f"Yahoo fallback price for {sym}: {price}")
            except Exception as e:
                log.warning(f"Yahoo fallback failed for {sym}: {e}")

    for p in positions:
        sym = p["symbol"]
        qty = p["quantity"]
        avg_cost = p["avg_cost"]

        if sym in needs_fix and sym in fallback_prices:
            price = fallback_prices[sym]
            if price and price > 0:
                p["market_price"] = price
                p["market_value"] = round(qty * price, 2)
                cost = qty * avg_cost
                p["unrealized_pnl"] = round(p["market_value"] - cost, 2)

        # Merge DB metadata
        ex = existing_by_sym.get(sym, {})
        if ex.get("purchase_date") and ex["purchase_date"] != "2025-01-01":
            p["purchase_date"] = ex["purchase_date"]
        d = div_cum.get(sym, 0)
        p["dividends_cumulative"] = round(d, 2)
        p["total_return"] = round(p.get("unrealized_pnl", 0) + d, 2)

    return positions


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    await db.import_csv_snapshots()
    asyncio.create_task(try_connect_tws())
    yield
    ib_conn.disconnect()

app = FastAPI(lifespan=lifespan)

async def try_connect_tws():
    await asyncio.sleep(1)
    connected = await ib_conn.connect()
    if connected:
        log.info("TWS connected, fetching close prices...")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, ib_conn.fetch_close_prices)
        log.info("Syncing positions...")
        await sync_positions()


async def sync_positions():
    try:
        portfolio = ib_conn.get_portfolio()
        if not portfolio:
            portfolio = ib_conn.get_positions()
        if portfolio:
            portfolio = await enrich_positions_from_db(portfolio)
            await db.upsert_positions(portfolio)
            today = date.today().isoformat()
            # Only save snapshot if most positions have real prices
            fake_count = sum(1 for p in portfolio
                             if p.get("sec_type", "STK") in ("STK", "ETF")
                             and abs(p["market_price"] - p["avg_cost"]) < 0.001
                             and p["avg_cost"] > 0)
            if fake_count <= 2:
                snap_rows = [{"symbol": p["symbol"], "quantity": p["quantity"],
                              "market_price": p["market_price"], "market_value": p["market_value"],
                              "day_pnl": p["unrealized_pnl"],
                              "cost_basis": p["avg_cost"] * p["quantity"],
                              "dividends_cumulative": p.get("dividends_cumulative", 0),
                              "total_return": p.get("total_return", 0)} for p in portfolio]
                # Carry forward __REALIZED__ pseudo-row from latest snapshot
                snap_dates = await db.get_snapshot_dates()
                prev_dates = [d for d in snap_dates if d < today]
                if prev_dates:
                    prev_snap = await db.get_snapshot(prev_dates[-1])
                    for row in prev_snap:
                        if row["symbol"] == "__REALIZED__":
                            snap_rows.append({
                                "symbol": "__REALIZED__", "quantity": 0,
                                "market_price": 0, "market_value": 0,
                                "day_pnl": row["day_pnl"], "cost_basis": 0,
                                "dividends_cumulative": 0,
                                "total_return": row.get("total_return", row["day_pnl"]),
                            })
                            break
                await db.save_snapshot(today, snap_rows)
            else:
                log.warning(f"Skipping snapshot save: {fake_count} positions have no real price")
            log.info(f"Synced {len(portfolio)} positions")

        fills = ib_conn.get_fills() or []
        completed = ib_conn.get_completed_orders() or []
        all_trades = {t["trade_id"]: t for t in fills + completed}
        if all_trades:
            await db.insert_trades(list(all_trades.values()))
            log.info(f"Synced {len(all_trades)} trades")
    except Exception as e:
        log.warning(f"Sync error: {e}")


# --- REST API ---

@app.get("/api/status")
async def status():
    return {"tws_connected": ib_conn.connected, "timestamp": datetime.now().isoformat()}

@app.get("/api/positions")
async def positions():
    if ib_conn.connected:
        portfolio = ib_conn.get_portfolio()
        if not portfolio:
            portfolio = ib_conn.get_positions()
        if portfolio:
            portfolio = await enrich_positions_from_db(portfolio)
            await db.upsert_positions(portfolio)
            return portfolio
    return await db.get_positions()

@app.get("/api/positions/history")
async def positions_history(date: str = Query(...)):
    return await db.get_snapshot(date)

@app.get("/api/account")
async def account():
    if ib_conn.connected:
        return ib_conn.get_account_summary()
    return {}

@app.get("/api/trades")
async def trades(symbol: str = None, from_date: str = None, to_date: str = None, limit: int = 1000):
    return await db.get_trades(symbol, from_date, to_date, limit)

@app.get("/api/pnl")
async def pnl(horizon: str = "1d"):
    horizon_map = {
        "1d": 1, "5d": 5, "1m": 30, "3m": 90, "6m": 180,
        "ytd": None, "1y": 365, "3y": 1095, "5y": 1825, "all": None,
    }
    days = horizon_map.get(horizon)
    today = date.today()

    if horizon == "ytd":
        start = date(today.year, 1, 1).isoformat()
    elif horizon == "mtd":
        start = date(today.year, today.month, 1).isoformat()
    elif horizon == "qtd":
        q_month = ((today.month - 1) // 3) * 3 + 1
        start = date(today.year, q_month, 1).isoformat()
    elif days is None:
        start = "2000-01-01"
    else:
        start = (today - timedelta(days=days)).isoformat()

    start_snap_date = await db.get_nearest_snapshot(start)
    current_positions = await db.get_positions()

    if not start_snap_date:
        return {"horizon": horizon, "start_date": start, "positions": [],
                "total_pnl": 0, "total_pnl_pct": 0}

    start_snap = await db.get_snapshot(start_snap_date)
    start_by_sym = {s["symbol"]: s for s in start_snap if s["symbol"] != "__REALIZED__"}

    # Get the latest snapshot to compute total portfolio P&L change
    snap_dates = await db.get_snapshot_dates()
    end_snap_date = snap_dates[-1] if snap_dates else today.isoformat()
    end_snap = await db.get_snapshot(end_snap_date)

    # Total P&L from snapshots (includes realized gains via __REALIZED__ row)
    start_total_pnl = sum(s.get("day_pnl", 0) for s in start_snap)
    end_total_pnl = sum(s.get("day_pnl", 0) for s in end_snap)
    start_total_mv = sum(s.get("market_value", 0) for s in start_snap if s["symbol"] != "__REALIZED__")

    # Per-symbol P&L for current positions (for the table)
    curr_by_sym = {p["symbol"]: p for p in current_positions}
    result = []
    for sym, c in sorted(curr_by_sym.items()):
        s = start_by_sym.get(sym, {})
        curr_val = c.get("market_value", 0)
        if s:
            start_val = s.get("market_value", 0)
        else:
            # Position opened during period — P&L is just unrealized from cost
            start_val = c.get("avg_cost", 0) * c.get("quantity", 0)

        pnl_val = curr_val - start_val
        pnl_pct = (pnl_val / abs(start_val) * 100) if start_val else 0
        result.append({"symbol": sym, "start_value": round(start_val, 2),
                        "current_value": round(curr_val, 2),
                        "pnl": round(pnl_val, 2), "pnl_pct": round(pnl_pct, 2)})

    total_pnl = round(end_total_pnl - start_total_pnl, 2)
    total_pnl_pct = round((total_pnl / start_total_mv * 100) if start_total_mv else 0, 2)

    return {"horizon": horizon, "start_date": start_snap_date,
            "positions": result, "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct}

@app.get("/api/pnl/timeseries")
async def pnl_timeseries(from_date: str = None, to_date: str = None):
    if not from_date:
        from_date = (date.today() - timedelta(days=365)).isoformat()
    if not to_date:
        to_date = date.today().isoformat()

    snapshots = await db.get_snapshots_range(from_date, to_date)
    by_date = {}
    for s in snapshots:
        d = s["date"]
        if d not in by_date:
            by_date[d] = {"value": 0, "pnl": 0, "cost": 0, "dividends": 0, "total_return": 0}
        by_date[d]["value"] += s["market_value"]
        by_date[d]["pnl"] += s.get("day_pnl", 0)
        by_date[d]["cost"] += s.get("cost_basis", 0)
        by_date[d]["dividends"] += s.get("dividends_cumulative", 0)
        by_date[d]["total_return"] += s.get("total_return", 0)

    dates = sorted(by_date.keys())
    if not dates:
        return {"dates": [], "values": [], "returns": [], "pnl": [],
                "dividends": [], "total_return": []}

    base_pnl = by_date[dates[0]]["pnl"]
    base_divs = by_date[dates[0]]["dividends"]
    base_tr = by_date[dates[0]]["total_return"]
    values = [round(by_date[d]["value"], 2) for d in dates]
    pnl = [round(by_date[d]["pnl"] - base_pnl, 2) for d in dates]
    dividends = [round(by_date[d]["dividends"] - base_divs, 2) for d in dates]
    total_return = [round(by_date[d]["total_return"] - base_tr, 2) for d in dates]

    # Compute daily-linked cumulative returns (adjusts for cash flows)
    # daily_return = change_in_pnl / previous_day_portfolio_value
    returns = [0.0]
    idx = 1.0
    for i in range(1, len(dates)):
        prev_val = by_date[dates[i-1]]["value"]
        pnl_change = (by_date[dates[i]]["pnl"] - by_date[dates[i-1]]["pnl"])
        daily_ret = pnl_change / prev_val if prev_val else 0
        idx *= (1 + daily_ret)
        returns.append(round((idx - 1) * 100, 2))

    return {"dates": dates, "values": values, "returns": returns,
            "pnl": pnl, "dividends": dividends, "total_return": total_return}

@app.get("/api/pnl/cumulative")
async def pnl_cumulative(period: str = "1y"):
    positions = await db.get_positions()
    if not positions:
        return {"dates": [], "pnl": [], "values": []}

    duration_map = {"1m": "1 M", "3m": "3 M", "6m": "6 M", "ytd": "1 Y",
                    "1y": "1 Y", "3y": "3 Y", "5y": "5 Y", "all": "5 Y"}
    duration = duration_map.get(period, "1 Y")

    all_series = {}
    loop = asyncio.get_event_loop()
    for p in positions:
        sym = p["symbol"]
        qty = p["quantity"]
        avg_cost = p["avg_cost"]
        sec_type = p.get("sec_type", "STK")
        if sec_type not in ("STK", "ETF"):
            continue
        try:
            bars = await loop.run_in_executor(
                None, lambda s=sym: ib_conn.get_historical_data(s, duration=duration))
            if bars:
                for bar in bars:
                    d = bar["date"]
                    mv = qty * bar["close"]
                    cost = qty * avg_cost
                    pnl_val = mv - cost
                    if d not in all_series:
                        all_series[d] = {"value": 0, "pnl": 0, "cost": 0}
                    all_series[d]["value"] += mv
                    all_series[d]["pnl"] += pnl_val
                    all_series[d]["cost"] += cost
        except Exception as e:
            log.warning(f"Historical fetch failed for {sym}: {e}")

    if not all_series:
        return {"dates": [], "pnl": [], "values": []}

    dates = sorted(all_series.keys())
    pnl_series = [round(all_series[d]["pnl"], 2) for d in dates]
    value_series = [round(all_series[d]["value"], 2) for d in dates]
    cost_series = [round(all_series[d]["cost"], 2) for d in dates]
    returns = [round((all_series[d]["value"] / all_series[d]["cost"] - 1) * 100, 2)
               if all_series[d]["cost"] else 0 for d in dates]

    return {"dates": dates, "pnl": pnl_series, "values": value_series,
            "cost": cost_series, "returns": returns}

@app.get("/api/performance")
async def performance(benchmark: str = "SPY", from_date: str = None, to_date: str = None):
    ts = await pnl_timeseries(from_date, to_date)
    bench_data = []
    if ib_conn.connected and ts["dates"]:
        days = (date.today() - date.fromisoformat(ts["dates"][0])).days
        duration = f"{max(days, 1)} D" if days < 365 else f"{max(days // 365, 1)} Y"
        try:
            bench_data = ib_conn.get_historical_data(benchmark, duration=duration)
        except Exception as e:
            log.warning(f"Benchmark fetch failed: {e}")

    bench_dates = [b["date"] for b in bench_data]
    bench_close = [b["close"] for b in bench_data]
    bench_returns = []
    if bench_close:
        base = bench_close[0]
        bench_returns = [round((c / base - 1) * 100, 2) for c in bench_close]

    return {"portfolio": ts, "benchmark": {"dates": bench_dates,
            "values": bench_close, "returns": bench_returns, "symbol": benchmark}}

@app.get("/api/exposure/sector")
async def exposure_sector():
    positions = await db.get_positions()
    by_type = {}
    total = 0
    for p in positions:
        t = p.get("sec_type", "OTHER") or "OTHER"
        by_type[t] = by_type.get(t, 0) + abs(p.get("market_value", 0))
        total += abs(p.get("market_value", 0))
    return [{"category": k, "value": round(v, 2),
             "weight": round(v / total * 100, 2) if total else 0}
            for k, v in sorted(by_type.items(), key=lambda x: -x[1])]

@app.get("/api/exposure/asset_class")
async def exposure_asset_class():
    return await exposure_sector()

@app.get("/api/risk")
async def risk():
    ts = await pnl_timeseries()
    values = ts["values"]
    pnl_series = ts["pnl"]
    if len(values) < 2:
        return {"annualized_return": 0, "volatility": 0, "sharpe": 0,
                "max_drawdown": 0, "max_drawdown_pct": 0}

    import math

    # Compute daily returns adjusted for cash flows: daily_pnl_change / prev_value
    daily_returns = []
    for i in range(1, len(values)):
        prev_val = values[i-1]
        pnl_change = pnl_series[i] - pnl_series[i-1]
        daily_returns.append(pnl_change / prev_val if prev_val else 0)

    if not daily_returns:
        return {"annualized_return": 0, "volatility": 0, "sharpe": 0,
                "max_drawdown": 0, "max_drawdown_pct": 0}

    n = len(daily_returns)
    mean_r = sum(daily_returns) / n
    var_r = sum((r - mean_r) ** 2 for r in daily_returns) / max(n - 1, 1)
    std_r = math.sqrt(var_r)

    ann_return = round(mean_r * 252 * 100, 2)
    ann_vol = round(std_r * math.sqrt(252) * 100, 2)
    sharpe = round((mean_r * 252) / (std_r * math.sqrt(252)), 2) if std_r else 0

    # Drawdown from daily-linked return index
    idx = 1.0
    peak = 1.0
    max_dd_pct = 0
    for r in daily_returns:
        idx *= (1 + r)
        if idx > peak:
            peak = idx
        dd_pct = (peak - idx) / peak if peak else 0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct

    return {"annualized_return": ann_return, "volatility": ann_vol,
            "sharpe": sharpe, "max_drawdown_pct": round(max_dd_pct * 100, 2)}

@app.get("/api/market/quote/{symbol}")
async def market_quote(symbol: str):
    if ib_conn.connected:
        return ib_conn.get_live_quote(symbol.upper())
    return {"symbol": symbol.upper(), "error": "TWS not connected"}

@app.get("/api/market/history/{symbol}")
async def market_history(symbol: str, period: str = "1y", bar: str = "1 day"):
    duration_map = {"1d": "1 D", "5d": "5 D", "1m": "1 M", "3m": "3 M",
                    "6m": "6 M", "1y": "1 Y", "3y": "3 Y", "5y": "5 Y"}
    duration = duration_map.get(period, "1 Y")
    if ib_conn.connected:
        return ib_conn.get_historical_data(symbol.upper(), duration=duration, bar_size=bar)
    return []

@app.get("/api/market/movers")
async def market_movers():
    positions = await db.get_positions()
    movers = sorted(positions, key=lambda p: abs(p.get("unrealized_pnl", 0)), reverse=True)
    return movers[:20]

@app.get("/api/watchlist")
async def get_watchlist():
    return await db.get_watchlist()

@app.post("/api/watchlist")
async def update_watchlist(symbols: list[str]):
    await db.set_watchlist(symbols)
    return {"ok": True}

@app.get("/api/dividends")
async def dividends(symbol: str = None, from_date: str = None, to_date: str = None):
    return await db.get_dividends(symbol, from_date, to_date)

@app.get("/api/dividends/summary")
async def dividends_summary():
    return await db.get_dividends_cumulative_by_symbol()

@app.get("/api/snapshot/dates")
async def snapshot_dates():
    return await db.get_snapshot_dates()

@app.post("/api/snapshot")
async def take_snapshot_now():
    if not ib_conn.connected:
        return {"error": "TWS not connected"}
    await sync_positions()
    return {"ok": True, "date": date.today().isoformat()}

@app.post("/api/import/trades")
async def import_trades_csv(file: UploadFile = File(...)):
    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode()))
    trades = list(reader)
    await db.insert_trades(trades)
    return {"imported": len(trades)}

@app.post("/api/import/positions")
async def import_positions_csv(file: UploadFile = File(...)):
    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode()))
    positions = list(reader)
    await db.upsert_positions(positions)
    return {"imported": len(positions)}

@app.get("/api/export/trades")
async def export_trades():
    trades = await db.get_trades(limit=100000)
    output = io.StringIO()
    if trades:
        writer = csv.DictWriter(output, fieldnames=[k for k in trades[0].keys() if k != "id"])
        writer.writeheader()
        for t in trades:
            row = {k: v for k, v in t.items() if k != "id"}
            writer.writerow(row)
    return StreamingResponse(io.BytesIO(output.getvalue().encode()),
                             media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=trades.csv"})

@app.get("/api/export/positions")
async def export_positions():
    positions = await db.get_positions()
    output = io.StringIO()
    if positions:
        writer = csv.DictWriter(output, fieldnames=[k for k in positions[0].keys() if k != "id"])
        writer.writeheader()
        for p in positions:
            row = {k: v for k, v in p.items() if k != "id"}
            writer.writerow(row)
    return StreamingResponse(io.BytesIO(output.getvalue().encode()),
                             media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=positions.csv"})


# --- WebSocket ---

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        for ws in self.active[:]:
            try:
                await ws.send_json(data)
            except:
                self.active.remove(ws)

ws_manager = ConnectionManager()

@app.websocket("/ws/prices")
async def ws_prices(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        while True:
            positions = await db.get_positions()
            if ib_conn.connected:
                live = ib_conn.get_portfolio()
                if not live:
                    live = ib_conn.get_positions()
                if live:
                    positions = await enrich_positions_from_db(live)
            if positions:
                await ws.send_json({"type": "positions", "data": positions})
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)

@app.websocket("/ws/pnl")
async def ws_pnl(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        while True:
            positions = await db.get_positions()
            total_pnl = sum(p.get("unrealized_pnl", 0) for p in positions)
            total_val = sum(p.get("market_value", 0) for p in positions)
            await ws.send_json({"type": "pnl", "total_pnl": round(total_pnl, 2),
                                "total_value": round(total_val, 2),
                                "positions": positions})
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)


# --- Static files ---
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

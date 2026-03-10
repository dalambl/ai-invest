import asyncio
import csv
from datetime import date
from pathlib import Path

from ibkr import ib_conn
from db import init_db, save_snapshot, upsert_positions, insert_trades

SNAPSHOTS_DIR = Path(__file__).parent / "data" / "snapshots"


async def take_snapshot():
    await init_db()
    connected = await ib_conn.connect()
    if not connected:
        print("Cannot connect to TWS")
        return

    portfolio = ib_conn.get_portfolio()
    if not portfolio:
        print("No positions found")
        ib_conn.disconnect()
        return

    await upsert_positions(portfolio)
    print(f"Saved {len(portfolio)} positions")

    today = date.today().isoformat()
    snap_rows = []
    for p in portfolio:
        snap_rows.append({
            "symbol": p["symbol"],
            "quantity": p["quantity"],
            "market_price": p["market_price"],
            "market_value": p["market_value"],
            "day_pnl": p["unrealized_pnl"],
            "cost_basis": p["avg_cost"] * p["quantity"],
        })
    await save_snapshot(today, snap_rows)
    print(f"Snapshot saved for {today}")

    fills = ib_conn.get_fills()
    completed = ib_conn.get_completed_orders()
    all_trades = {t["trade_id"]: t for t in fills + completed}
    if all_trades:
        await insert_trades(list(all_trades.values()))
        print(f"Saved {len(all_trades)} trades")

    ib_conn.disconnect()
    print("Done")


if __name__ == "__main__":
    asyncio.run(take_snapshot())

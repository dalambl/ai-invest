import contextlib
import csv
from pathlib import Path

import aiosqlite

DB_PATH = Path(__file__).parent / "data" / "trading.db"
SNAPSHOTS_DIR = Path(__file__).parent / "data" / "snapshots"


async def get_db():
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    return db


async def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await get_db()
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, account TEXT, symbol TEXT, sec_type TEXT,
            quantity REAL, avg_cost REAL, market_price REAL,
            market_value REAL, unrealized_pnl REAL, realized_pnl REAL,
            currency TEXT, purchase_date TEXT
        );
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT, symbol TEXT, description TEXT, asset_class TEXT,
            action TEXT, quantity REAL, price REAL, currency TEXT,
            commission REAL, net_amount REAL, exchange TEXT,
            order_type TEXT, account TEXT, trade_id TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, symbol TEXT, quantity REAL, market_price REAL,
            market_value REAL, day_pnl REAL, cost_basis REAL,
            dividends_cumulative REAL DEFAULT 0,
            total_return REAL DEFAULT 0,
            UNIQUE(date, symbol)
        );
        CREATE TABLE IF NOT EXISTS dividends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, symbol TEXT, amount REAL, description TEXT,
            UNIQUE(date, symbol, amount)
        );
        CREATE TABLE IF NOT EXISTS watchlist (
            symbol TEXT PRIMARY KEY
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_date ON snapshots(date);
        CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(trade_date);
        CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
        CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
    """)
    for col, default in [
        ("purchase_date", "TEXT"),
        ("dividends_cumulative", "REAL DEFAULT 0"),
        ("total_return", "REAL DEFAULT 0"),
    ]:
        with contextlib.suppress(Exception):
            await db.execute(f"ALTER TABLE positions ADD COLUMN {col} {default}")
    for col in ["dividends_cumulative", "total_return"]:
        with contextlib.suppress(Exception):
            await db.execute(f"ALTER TABLE snapshots ADD COLUMN {col} REAL DEFAULT 0")
    await db.commit()
    await db.close()


async def import_csv_snapshots():
    if not SNAPSHOTS_DIR.exists():
        return
    db = await get_db()
    for csv_file in sorted(SNAPSHOTS_DIR.glob("*.csv")):
        with open(csv_file) as f:
            reader = csv.DictReader(f)
            for row in reader:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO snapshots (date, symbol, quantity, market_price, market_value, day_pnl, cost_basis)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        row["date"],
                        row["symbol"],
                        float(row["quantity"]),
                        float(row["market_price"]),
                        float(row["market_value"]),
                        float(row["day_pnl"]),
                        float(row["cost_basis"]),
                    ),
                )
    await db.commit()
    await db.close()


async def upsert_positions(positions: list[dict]):
    db = await get_db()
    await db.execute("DELETE FROM positions")
    for p in positions:
        await db.execute(
            """
            INSERT INTO positions (timestamp, account, symbol, sec_type, quantity, avg_cost,
                market_price, market_value, unrealized_pnl, realized_pnl, currency, purchase_date,
                dividends_cumulative, total_return)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                p["timestamp"],
                p["account"],
                p["symbol"],
                p["sec_type"],
                p["quantity"],
                p["avg_cost"],
                p["market_price"],
                p["market_value"],
                p["unrealized_pnl"],
                p["realized_pnl"],
                p["currency"],
                p.get("purchase_date", "2025-01-01"),
                p.get("dividends_cumulative", 0),
                p.get("total_return", 0),
            ),
        )
    await db.commit()
    await db.close()


async def insert_trades(trades: list[dict]):
    db = await get_db()
    for t in trades:
        await db.execute(
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
    await db.commit()
    await db.close()


async def save_snapshot(snapshot_date: str, rows: list[dict]):
    db = await get_db()
    for r in rows:
        await db.execute(
            """
            INSERT OR REPLACE INTO snapshots (date, symbol, quantity, market_price, market_value,
                day_pnl, cost_basis, dividends_cumulative, total_return)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                snapshot_date,
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
    await db.commit()
    await db.close()
    csv_path = SNAPSHOTS_DIR / f"{snapshot_date}.csv"
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
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
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {"date": snapshot_date, **{k: r.get(k, 0) for k in fieldnames if k != "date"}}
            )


async def insert_dividends(dividends: list[dict]):
    db = await get_db()
    for d in dividends:
        await db.execute(
            """
            INSERT OR IGNORE INTO dividends (date, symbol, amount, description)
            VALUES (?, ?, ?, ?)
        """,
            (d["date"], d["symbol"], d["amount"], d.get("description", "")),
        )
    await db.commit()
    await db.close()


async def get_dividends(symbol=None, from_date=None, to_date=None):
    db = await get_db()
    query = "SELECT * FROM dividends WHERE 1=1"
    params = []
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    if from_date:
        query += " AND date >= ?"
        params.append(from_date)
    if to_date:
        query += " AND date <= ?"
        params.append(to_date)
    query += " ORDER BY date"
    rows = await db.execute_fetchall(query, params)
    await db.close()
    return [dict(r) for r in rows]


async def get_dividends_cumulative_by_symbol(up_to_date=None):
    db = await get_db()
    query = "SELECT symbol, SUM(amount) as total FROM dividends"
    params = []
    if up_to_date:
        query += " WHERE date <= ?"
        params.append(up_to_date)
    query += " GROUP BY symbol"
    rows = await db.execute_fetchall(query, params)
    await db.close()
    return {r["symbol"]: r["total"] for r in rows}


async def get_positions():
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT timestamp, account, symbol, sec_type, quantity, avg_cost, "
        "market_price, market_value, unrealized_pnl, realized_pnl, currency, "
        "COALESCE(purchase_date, '2025-01-01') as purchase_date, "
        "COALESCE(dividends_cumulative, 0) as dividends_cumulative, "
        "COALESCE(total_return, 0) as total_return "
        "FROM positions ORDER BY symbol"
    )
    await db.close()
    return [dict(r) for r in rows]


async def get_trades(symbol=None, from_date=None, to_date=None, limit=1000):
    db = await get_db()
    query = "SELECT * FROM trades WHERE 1=1"
    params = []
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    if from_date:
        query += " AND trade_date >= ?"
        params.append(from_date)
    if to_date:
        query += " AND trade_date <= ?"
        params.append(to_date)
    query += " ORDER BY trade_date DESC LIMIT ?"
    params.append(limit)
    rows = await db.execute_fetchall(query, params)
    await db.close()
    return [dict(r) for r in rows]


async def get_snapshot(snap_date: str):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM snapshots WHERE date = ?", (snap_date,))
    await db.close()
    return [dict(r) for r in rows]


async def get_snapshots_range(from_date: str, to_date: str):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM snapshots WHERE date >= ? AND date <= ? ORDER BY date, symbol",
        (from_date, to_date),
    )
    await db.close()
    return [dict(r) for r in rows]


async def get_snapshot_dates():
    db = await get_db()
    rows = await db.execute_fetchall("SELECT DISTINCT date FROM snapshots ORDER BY date")
    await db.close()
    return [r["date"] for r in rows]


async def get_nearest_snapshot(target_date: str):
    db = await get_db()
    row = await db.execute_fetchall(
        "SELECT DISTINCT date FROM snapshots WHERE date <= ? ORDER BY date DESC LIMIT 1",
        (target_date,),
    )
    if not row:
        row = await db.execute_fetchall("SELECT DISTINCT date FROM snapshots ORDER BY date LIMIT 1")
    await db.close()
    if row:
        return row[0]["date"]
    return None


async def get_watchlist():
    db = await get_db()
    rows = await db.execute_fetchall("SELECT symbol FROM watchlist ORDER BY symbol")
    await db.close()
    return [r["symbol"] for r in rows]


async def set_watchlist(symbols: list[str]):
    db = await get_db()
    await db.execute("DELETE FROM watchlist")
    for s in symbols:
        await db.execute("INSERT OR IGNORE INTO watchlist (symbol) VALUES (?)", (s.upper(),))
    await db.commit()
    await db.close()


async def get_latest_known_prices(symbols: set[str]):
    """Get the latest snapshot price for each symbol where price != cost_basis/quantity.

    This finds a real market price, skipping snapshots where the price was
    a fallback to avg_cost (i.e. where day_pnl == 0 and cost_basis == market_value).
    """
    db = await get_db()
    result = {}
    for sym in symbols:
        # Find latest snapshot where the price is NOT equal to avg_cost
        # (day_pnl != 0 means market_value != cost_basis, so price is real)
        row = await db.execute_fetchall(
            """
            SELECT market_price FROM snapshots
            WHERE symbol = ? AND ABS(day_pnl) > 0.01
            ORDER BY date DESC LIMIT 1
        """,
            (sym,),
        )
        if row:
            result[sym] = row[0]["market_price"]
    await db.close()
    return result

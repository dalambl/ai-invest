"""
Pull trades from IB TWS, write to CSV files, fetch positions,
and reconcile positions against trade history.

Usage:
  python trade_log.py --flex-token YOUR_TOKEN --flex-query-id YOUR_QUERY_ID
  python trade_log.py --live
  python trade_log.py --flex-token TOKEN --flex-query-id QID --live
  python trade_log.py --flex-file flex_report.xml
"""

import argparse
import csv
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from ib_insync import IB
from ib_insync.flexreport import FlexReport


OUTPUT_DIR = Path(__file__).parent / "data"
TRADES_CSV = OUTPUT_DIR / "trades.csv"
POSITIONS_CSV = OUTPUT_DIR / "positions.csv"
RECONCILIATION_CSV = OUTPUT_DIR / "reconciliation.csv"

TRADE_COLUMNS = [
    "trade_date", "symbol", "description", "asset_class", "action",
    "quantity", "price", "currency", "commission", "net_amount",
    "exchange", "order_type", "account", "trade_id",
]
POSITION_COLUMNS = [
    "timestamp", "account", "symbol", "sec_type", "quantity", "avg_cost",
    "market_price", "market_value", "unrealized_pnl", "realized_pnl", "currency",
]


def fetch_flex_trades(token=None, query_id=None, path=None):
    if path:
        print(f"Loading Flex report from {path}...")
        report = FlexReport(path=path)
    elif token and query_id:
        print("Downloading Flex report...")
        report = FlexReport(token=token, queryId=query_id)
    else:
        return pd.DataFrame(columns=TRADE_COLUMNS)

    report.save(OUTPUT_DIR / "flex_report_backup.xml")
    print(f"  Topics: {report.topics()}")

    for topic in ("Trade", "TradeConfirm", "Order"):
        trades = report.extract(topic)
        if trades:
            print(f"  Found {len(trades)} records under '{topic}'")
            break
    else:
        print("  No trade data found.")
        return pd.DataFrame(columns=TRADE_COLUMNS)

    rows = []
    for t in trades:
        d = t.__dict__
        rows.append({
            "trade_date": d.get("tradeDate") or d.get("dateTime", ""),
            "symbol": d.get("symbol", ""),
            "description": d.get("description", ""),
            "asset_class": d.get("assetCategory", d.get("secType", "")),
            "action": d.get("buySell", d.get("side", "")),
            "quantity": d.get("quantity", 0),
            "price": d.get("tradePrice", d.get("price", 0)),
            "currency": d.get("currency", ""),
            "commission": d.get("ibCommission", d.get("commission", 0)),
            "net_amount": d.get("netCash", d.get("proceeds", 0)),
            "exchange": d.get("exchange", ""),
            "order_type": d.get("orderType", ""),
            "account": d.get("accountId", d.get("acctAlias", "")),
            "trade_id": d.get("tradeID", d.get("execId", "")),
        })

    df = pd.DataFrame(rows, columns=TRADE_COLUMNS)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df.sort_values("trade_date", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def fetch_live_trades(ib):
    rows = []

    fills = ib.fills()
    print(f"  Session fills: {len(fills)}")
    for fill in fills:
        ex = fill.execution
        c = fill.contract
        rows.append({
            "trade_date": str(ex.time),
            "symbol": c.symbol,
            "description": c.localSymbol or c.symbol,
            "asset_class": c.secType,
            "action": "BUY" if ex.side == "BOT" else "SELL",
            "quantity": float(ex.shares),
            "price": float(ex.price),
            "currency": c.currency,
            "commission": float(fill.commissionReport.commission) if fill.commissionReport else 0,
            "net_amount": round(float(ex.shares) * float(ex.price) * (1 if ex.side == "BOT" else -1), 2),
            "exchange": ex.exchange,
            "order_type": "",
            "account": ex.acctNumber,
            "trade_id": ex.execId,
        })

    completed = ib.reqCompletedOrders(apiOnly=False)
    print(f"  Completed orders: {len(completed)}")
    for trade in completed:
        order = trade.order
        c = trade.contract
        for fill in trade.fills:
            ex = fill.execution
            rows.append({
                "trade_date": str(ex.time),
                "symbol": c.symbol,
                "description": c.localSymbol or c.symbol,
                "asset_class": c.secType,
                "action": "BUY" if ex.side == "BOT" else "SELL",
                "quantity": float(ex.shares),
                "price": float(ex.price),
                "currency": c.currency,
                "commission": float(fill.commissionReport.commission) if fill.commissionReport else 0,
                "net_amount": round(float(ex.shares) * float(ex.price) * (1 if ex.side == "BOT" else -1), 2),
                "exchange": ex.exchange,
                "order_type": order.orderType,
                "account": ex.acctNumber,
                "trade_id": ex.execId,
            })

    df = pd.DataFrame(rows, columns=TRADE_COLUMNS)
    if not df.empty:
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
        df.drop_duplicates(subset=["trade_id"], keep="first", inplace=True)
        df.sort_values("trade_date", inplace=True)
        df.reset_index(drop=True, inplace=True)
    return df


def fetch_positions(ib):
    positions = ib.positions()
    print(f"  Positions: {len(positions)}")
    now = datetime.now().isoformat()
    rows = []
    for pos in positions:
        c = pos.contract
        rows.append({
            "timestamp": now,
            "account": pos.account,
            "symbol": c.symbol,
            "sec_type": c.secType,
            "quantity": float(pos.position),
            "avg_cost": round(pos.avgCost, 4),
            "market_price": 0,
            "market_value": round(float(pos.position) * pos.avgCost, 2),
            "unrealized_pnl": 0,
            "realized_pnl": 0,
            "currency": c.currency,
        })
    return pd.DataFrame(rows, columns=POSITION_COLUMNS)


def reconcile(positions_df, trades_df):
    if trades_df.empty and positions_df.empty:
        return pd.DataFrame(columns=["symbol", "position_qty", "trade_net_qty", "difference", "status"])

    if not trades_df.empty:
        tn = trades_df.copy()
        tn["signed"] = tn.apply(
            lambda r: r["quantity"] if str(r["action"]).upper() in ("BUY", "BOT") else -r["quantity"], axis=1)
        ts = tn.groupby("symbol")["signed"].sum().reset_index().rename(columns={"signed": "trade_net_qty"})
    else:
        ts = pd.DataFrame(columns=["symbol", "trade_net_qty"])

    if not positions_df.empty:
        ps = positions_df.groupby("symbol")["quantity"].sum().reset_index().rename(columns={"quantity": "position_qty"})
    else:
        ps = pd.DataFrame(columns=["symbol", "position_qty"])

    merged = pd.merge(ps, ts, on="symbol", how="outer").fillna(0)
    merged["difference"] = merged["position_qty"] - merged["trade_net_qty"]
    merged["status"] = merged["difference"].apply(lambda d: "MATCHED" if d == 0 else "MISMATCH")
    return merged


def write_csv(trades_df, positions_df, reconciliation_df):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    trades_df.to_csv(TRADES_CSV, index=False)
    print(f"  {TRADES_CSV}: {len(trades_df)} trades")
    positions_df.to_csv(POSITIONS_CSV, index=False)
    print(f"  {POSITIONS_CSV}: {len(positions_df)} positions")
    reconciliation_df.to_csv(RECONCILIATION_CSV, index=False)
    print(f"  {RECONCILIATION_CSV}: {len(reconciliation_df)} symbols")
    mismatches = (reconciliation_df["status"] == "MISMATCH").sum()
    if mismatches:
        print(f"\n  *** {mismatches} MISMATCHES ***")
    else:
        print("\n  All positions reconcile.")


def main():
    parser = argparse.ArgumentParser(description="Pull IB trades, build Trade_Log CSV, reconcile")
    parser.add_argument("--flex-token")
    parser.add_argument("--flex-query-id")
    parser.add_argument("--flex-file")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7496)
    parser.add_argument("--client-id", type=int, default=2)
    args = parser.parse_args()

    if not args.flex_token and not args.flex_file and not args.live:
        parser.error("Provide --flex-token/--flex-query-id, --flex-file, or --live")

    trades_df = pd.DataFrame(columns=TRADE_COLUMNS)
    positions_df = pd.DataFrame(columns=POSITION_COLUMNS)
    ib = None

    if args.flex_token or args.flex_file:
        flex_trades = fetch_flex_trades(token=args.flex_token, query_id=args.flex_query_id, path=args.flex_file)
        trades_df = pd.concat([trades_df, flex_trades], ignore_index=True)

    if args.live:
        ib = IB()
        print(f"Connecting to TWS at {args.host}:{args.port}...")
        ib.connect(args.host, args.port, clientId=args.client_id)
        print("Connected.\n")
        live_trades = fetch_live_trades(ib)
        trades_df = pd.concat([trades_df, live_trades], ignore_index=True)
        trades_df.drop_duplicates(subset=["trade_id"], keep="first", inplace=True)
        positions_df = fetch_positions(ib)

    if not trades_df.empty:
        trades_df.sort_values("trade_date", inplace=True)
        trades_df.reset_index(drop=True, inplace=True)

    reconciliation_df = reconcile(positions_df, trades_df)

    print("\n=== SUMMARY ===")
    if not trades_df.empty:
        print(f"  {trades_df['trade_date'].min()} to {trades_df['trade_date'].max()}")
        print(f"  {len(trades_df)} trades, {trades_df['symbol'].nunique()} symbols")
        print(f"  Commissions: {trades_df['commission'].sum():,.2f}")

    write_csv(trades_df, positions_df, reconciliation_df)

    if ib:
        ib.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    main()

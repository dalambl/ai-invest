"""Tests for the transaction parser & cost-basis math in rebuild_history."""

from __future__ import annotations

from pathlib import Path

import pytest

from rebuild_history import build_daily_positions, classify, parse_transactions


def test_classify_stock():
    sec, sym, mult = classify("LENNAR CORP-A", "LEN")
    assert sec == "STK"
    assert sym == "LEN"
    assert mult == 1


def test_classify_option_from_description():
    sec, sym, mult = classify("NVDA 17DEC27 110 P", "NVDA  271217P00110000")
    assert sec == "OPT"
    assert sym == "NVDA 17DEC27 110 P"
    assert mult == 100


def test_classify_call_option():
    sec, sym, mult = classify("AAPL 19SEP25 250 C", "AAPL  250919C00250000")
    assert sec == "OPT"
    assert sym == "AAPL 19SEP25 250 C"
    assert mult == 100


def test_classify_future_excluded_via_prefix():
    sec, sym, mult = classify("M6E 15JUN26", "M6EM6")
    assert sec == "FUT"
    assert sym == "M6E 15JUN26"
    assert mult == 1


def test_classify_alias_remapped():
    sec, sym, mult = classify("BROOKFIELD INFRASTRUCTURE-A", "BIPC.OLD")
    assert sec == "STK"
    assert sym == "BIPC"


def _write_tx_csv(tmp_path: Path, rows: list[dict]) -> Path:
    """Write a minimal IB transaction-history CSV from row dicts."""
    p = tmp_path / "tx.csv"
    cols = [
        "Date",
        "Account",
        "Description",
        "Transaction Type",
        "Symbol",
        "Quantity",
        "Price",
        "Price Currency",
        "Gross Amount",
        "Commission",
        "Net Amount",
    ]
    lines = ["Statement,Header,Field Name,Field Value\n"]
    lines.append("Transaction History,Header," + ",".join(cols) + "\n")
    for r in rows:
        vals = [str(r.get(c, "")) for c in cols]
        lines.append("Transaction History,Data," + ",".join(vals) + "\n")
    p.write_text("".join(lines))
    return p


def test_parse_transactions_skips_futures(tmp_path):
    p = _write_tx_csv(
        tmp_path,
        [
            {
                "Date": "2025-01-02",
                "Account": "U1",
                "Description": "M6E 15JUN26",
                "Transaction Type": "Buy",
                "Symbol": "M6EM6",
                "Quantity": 1,
                "Price": 1.18,
                "Price Currency": "USD",
                "Gross Amount": -14750,
                "Commission": -0.41,
                "Net Amount": -14750.41,
            },
            {
                "Date": "2025-01-03",
                "Account": "U1",
                "Description": "BOEING CO/THE",
                "Transaction Type": "Buy",
                "Symbol": "BA",
                "Quantity": 10,
                "Price": 200.0,
                "Price Currency": "USD",
                "Gross Amount": -2000,
                "Commission": -1.0,
                "Net Amount": -2001.0,
            },
        ],
    )
    txs = parse_transactions(p)
    assert {t["symbol"] for t in txs} == {"BA"}


def test_parse_transactions_preserves_option_symbol(tmp_path):
    p = _write_tx_csv(
        tmp_path,
        [
            {
                "Date": "2025-12-02",
                "Account": "U1",
                "Description": "NVDA 17DEC27 110 P",
                "Transaction Type": "Buy",
                "Symbol": "NVDA  271217P00110000",
                "Quantity": 1,
                "Price": 10.5,
                "Price Currency": "USD",
                "Gross Amount": -1050.0,
                "Commission": -0.5941,
                "Net Amount": -1050.5941,
            },
        ],
    )
    txs = parse_transactions(p)
    assert len(txs) == 1
    assert txs[0]["symbol"] == "NVDA 17DEC27 110 P"
    assert txs[0]["sec_type"] == "OPT"
    assert txs[0]["multiplier"] == 100


def test_cost_basis_uses_net_includes_commission(tmp_path):
    p = _write_tx_csv(
        tmp_path,
        [
            {
                "Date": "2025-01-03",
                "Account": "U1",
                "Description": "BOEING CO/THE",
                "Transaction Type": "Buy",
                "Symbol": "BA",
                "Quantity": 80,
                "Price": 154.10,
                "Price Currency": "USD",
                "Gross Amount": -12328.0,
                "Commission": -1.25,
                "Net Amount": -12329.25,
            },
        ],
    )
    txs = parse_transactions(p)
    (positions, costs, _, _, realized, _, sec_types, mults, _) = build_daily_positions(txs, {}, {})
    last = sorted(costs.keys())[-1]
    assert costs[last]["BA"] == pytest.approx(12329.25, abs=0.01)
    assert positions[last]["BA"] == 80
    assert sec_types["BA"] == "STK"
    assert mults["BA"] == 1


def test_realized_pnl_subtracts_sell_commission(tmp_path):
    p = _write_tx_csv(
        tmp_path,
        [
            {
                "Date": "2025-01-03",
                "Account": "U1",
                "Description": "BOEING CO/THE",
                "Transaction Type": "Buy",
                "Symbol": "BA",
                "Quantity": 10,
                "Price": 100.0,
                "Price Currency": "USD",
                "Gross Amount": -1000.0,
                "Commission": -1.0,
                "Net Amount": -1001.0,
            },
            {
                "Date": "2025-06-01",
                "Account": "U1",
                "Description": "BOEING CO/THE",
                "Transaction Type": "Sell",
                "Symbol": "BA",
                "Quantity": -10,
                "Price": 200.0,
                "Price Currency": "USD",
                "Gross Amount": 2000.0,
                "Commission": -1.5,
                "Net Amount": 1998.5,
            },
        ],
    )
    txs = parse_transactions(p)
    (_, _, _, _, realized, _, _, _, _) = build_daily_positions(txs, {}, {})
    # Realized = net proceeds (1998.50) - cost basis (1001.00) = 997.50
    assert realized["2025-06-01"] == pytest.approx(997.50, abs=0.01)


def test_eur_buy_records_usd_cost_basis_at_trade_fx(tmp_path):
    """Buy 10 RHM @ 100 EUR with EUR/USD = 1.20 → cost = 1000 EUR / $1200.

    IB convention: ``Price`` in trade currency, ``Gross/Net`` in account
    base (USD). So Net = -1200 USD = -1000 EUR × 1.20.
    """
    p = _write_tx_csv(
        tmp_path,
        [
            {
                "Date": "2025-03-01",
                "Account": "U1",
                "Description": "RHEINMETALL AG",
                "Transaction Type": "Buy",
                "Symbol": "RHM",
                "Quantity": 10,
                "Price": 100.0,
                "Price Currency": "EUR",
                "Gross Amount": -1200.0,
                "Commission": 0.0,
                "Net Amount": -1200.0,
            },
        ],
    )
    txs = parse_transactions(p)
    fx_rates = {"EUR": {"2025-03-01": 1.20}}
    (_, costs, costs_usd, _, _, _, _, _, currencies) = build_daily_positions(
        txs, {}, {}, fx_rates=fx_rates
    )
    last = sorted(costs.keys())[-1]
    assert costs[last]["RHM"] == pytest.approx(1000.0, abs=0.01)
    assert costs_usd[last]["RHM"] == pytest.approx(1200.0, abs=0.01)
    assert currencies["RHM"] == "EUR"


def test_eur_sell_realized_uses_sale_day_fx(tmp_path):
    """Buy at 1.10, sell at 1.20: stock P&L 0, FX P&L = 1000 × (1.20−1.10) = $100.

    Realized = sale_proceeds_usd − cost_usd
             = 1000 × 1.20 − 1000 × 1.10 = $100.
    """
    p = _write_tx_csv(
        tmp_path,
        [
            {
                "Date": "2025-01-02",
                "Account": "U1",
                "Description": "RHEINMETALL AG",
                "Transaction Type": "Buy",
                "Symbol": "RHM",
                "Quantity": 10,
                "Price": 100.0,
                "Price Currency": "EUR",
                "Gross Amount": -1100.0,
                "Commission": 0.0,
                "Net Amount": -1100.0,
            },
            {
                "Date": "2025-06-02",
                "Account": "U1",
                "Description": "RHEINMETALL AG",
                "Transaction Type": "Sell",
                "Symbol": "RHM",
                "Quantity": -10,
                "Price": 100.0,
                "Price Currency": "EUR",
                "Gross Amount": 1200.0,
                "Commission": 0.0,
                "Net Amount": 1200.0,
            },
        ],
    )
    txs = parse_transactions(p)
    fx_rates = {"EUR": {"2025-01-02": 1.10, "2025-06-02": 1.20}}
    (_, _, _, _, realized, _, _, _, _) = build_daily_positions(txs, {}, {}, fx_rates=fx_rates)
    assert realized["2025-06-02"] == pytest.approx(100.0, abs=0.01)


def test_option_buy_cost_basis_is_total_premium(tmp_path):
    p = _write_tx_csv(
        tmp_path,
        [
            {
                "Date": "2025-12-02",
                "Account": "U1",
                "Description": "NVDA 17DEC27 110 P",
                "Transaction Type": "Buy",
                "Symbol": "NVDA  271217P00110000",
                "Quantity": 1,
                "Price": 10.5,
                "Price Currency": "USD",
                "Gross Amount": -1050.0,
                "Commission": -0.5941,
                "Net Amount": -1050.5941,
            },
        ],
    )
    txs = parse_transactions(p)
    (_, costs, _, _, _, _, sec_types, mults, _) = build_daily_positions(txs, {}, {})
    last = sorted(costs.keys())[-1]
    sym = "NVDA 17DEC27 110 P"
    # Cost basis equals the dollar amount paid (option premium × multiplier, plus comm).
    assert costs[last][sym] == pytest.approx(1050.5941, abs=0.01)
    assert sec_types[sym] == "OPT"
    assert mults[sym] == 100

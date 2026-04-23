"""Tests for the IB Activity Statement parser using the real 2025 statement."""

from __future__ import annotations

from pathlib import Path

import pytest

import ib_statement

STATEMENT = Path(__file__).resolve().parent.parent / "data" / "U640574_2025_2025.csv"


@pytest.fixture(scope="module")
def parsed():
    if not STATEMENT.exists():
        pytest.skip(f"missing {STATEMENT.name}")
    return ib_statement.parse(STATEMENT)


def test_period(parsed):
    assert parsed.period_start == "2025-01-01"
    assert parsed.period_end == "2025-12-31"


def test_open_positions_includes_stocks_and_options(parsed):
    syms = {p.symbol for p in parsed.open_positions}
    assert "AES" in syms
    assert "ORSTED" in syms
    assert "NVDA 17DEC27 110 P" in syms
    assert "PLTR 15JAN27 50 P" in syms


def test_open_positions_carry_correct_fields(parsed):
    aes = next(p for p in parsed.open_positions if p.symbol == "AES")
    assert aes.quantity == 700
    assert aes.cost_basis == pytest.approx(8242.79678, rel=1e-6)
    assert aes.value == pytest.approx(10038.0, rel=1e-6)
    assert aes.unrealized_pnl == pytest.approx(1795.20322, rel=1e-6)
    assert aes.currency == "USD"
    assert aes.multiplier == 1


def test_options_carry_multiplier(parsed):
    nvda = next(p for p in parsed.open_positions if p.symbol == "NVDA 17DEC27 110 P")
    assert nvda.multiplier == 100
    assert nvda.value == pytest.approx(1035.0, rel=1e-6)


def test_realized_total_matches_statement(parsed):
    # Cross-check vs "Total (All Assets)" line in the activity statement.
    assert parsed.realized_total == pytest.approx(11395.35, abs=0.01)


def test_unrealized_total_matches_statement(parsed):
    assert parsed.unrealized_total == pytest.approx(32024.13, abs=0.01)


def test_fx_realized_and_unrealized(parsed):
    # Cash FX Translation Gain/Loss = $72.044245 in base currency.
    assert parsed.fx_realized == pytest.approx(72.044245, abs=0.01)
    # Forex unrealized: EUR +63.5, DKK -0.955755, total +62.54
    assert parsed.fx_unrealized == pytest.approx(62.544245, abs=0.01)


def test_realized_by_symbol_excludes_forex(parsed):
    # Forex P/L is bucketed into fx_*, not realized_by_symbol.
    assert "DKK" not in parsed.realized_by_symbol
    assert "EUR" not in parsed.realized_by_symbol
    # A few sanity stocks
    assert parsed.realized_by_symbol["BAC"] == pytest.approx(1389.36, abs=0.01)
    assert parsed.realized_by_symbol["FXI"] == pytest.approx(3801.11, abs=0.01)

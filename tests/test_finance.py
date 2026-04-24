"""Tests for the pure finance math helpers."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from finance import (
    FX_ROW,
    REALIZED_ROW,
    aggregate_snapshot_timeseries,
    daily_linked_returns,
    drawdown_series,
    horizon_start_date,
    max_drawdown_pct,
    monthly_returns,
    risk_metrics,
    weights_by_currency,
    year_returns_from_months,
)

# --- aggregate_snapshot_timeseries -----------------------------------------


def test_aggregate_empty():
    assert aggregate_snapshot_timeseries([]) == {}


def test_aggregate_sums_per_date():
    rows = [
        {
            "date": "2024-01-01",
            "symbol": "AAA",
            "market_value": 100.0,
            "day_pnl": 10.0,
            "cost_basis": 90.0,
            "dividends_cumulative": 1.0,
            "total_return": 11.0,
        },
        {
            "date": "2024-01-01",
            "symbol": "BBB",
            "market_value": 200.0,
            "day_pnl": -5.0,
            "cost_basis": 205.0,
            "dividends_cumulative": 2.0,
            "total_return": -3.0,
        },
        {
            "date": "2024-01-02",
            "symbol": "AAA",
            "market_value": 110.0,
            "day_pnl": 20.0,
            "cost_basis": 90.0,
            "dividends_cumulative": 1.5,
            "total_return": 21.5,
        },
    ]
    out = aggregate_snapshot_timeseries(rows)
    assert out["2024-01-01"]["value"] == 300.0
    assert out["2024-01-01"]["pnl"] == 5.0
    assert out["2024-01-01"]["cost"] == 295.0
    assert out["2024-01-01"]["dividends"] == 3.0
    assert out["2024-01-01"]["total_return"] == 8.0
    assert out["2024-01-02"]["value"] == 110.0


def test_aggregate_excludes_realized_row_from_value_and_cost():
    rows = [
        {
            "date": "2024-01-01",
            "symbol": "AAA",
            "market_value": 100.0,
            "day_pnl": 10.0,
            "cost_basis": 90.0,
            "dividends_cumulative": 0,
            "total_return": 10.0,
        },
        {
            "date": "2024-01-01",
            "symbol": REALIZED_ROW,
            "market_value": 0.0,
            "day_pnl": 50.0,
            "cost_basis": 0.0,
            "dividends_cumulative": 0,
            "total_return": 50.0,
        },
    ]
    out = aggregate_snapshot_timeseries(rows)
    # value and cost exclude the pseudo-row; pnl and total_return include it
    assert out["2024-01-01"]["value"] == 100.0
    assert out["2024-01-01"]["cost"] == 90.0
    assert out["2024-01-01"]["pnl"] == 60.0
    assert out["2024-01-01"]["total_return"] == 60.0


def test_aggregate_uses_usd_fields_when_present():
    # EUR position: market_value=1000 EUR, market_value_usd=1100; should
    # aggregate the USD value, not the EUR.
    rows = [
        {
            "date": "2025-06-01",
            "symbol": "RHM",
            "market_value": 1000.0,
            "cost_basis": 900.0,
            "day_pnl": 100.0,
            "market_value_usd": 1100.0,
            "cost_basis_usd": 950.0,
            "stock_pnl_usd": 110.0,
            "fx_pnl_usd": 40.0,
            "dividends_cumulative": 0,
            "total_return": 150.0,
            "currency": "EUR",
        },
        {
            "date": "2025-06-01",
            "symbol": "AAA",
            "market_value": 200.0,
            "cost_basis": 180.0,
            "day_pnl": 20.0,
            "market_value_usd": 200.0,
            "cost_basis_usd": 180.0,
            "stock_pnl_usd": 20.0,
            "fx_pnl_usd": 0.0,
            "dividends_cumulative": 0,
            "total_return": 20.0,
            "currency": "USD",
        },
    ]
    out = aggregate_snapshot_timeseries(rows)
    assert out["2025-06-01"]["value"] == 1300.0
    assert out["2025-06-01"]["cost"] == 1130.0
    assert out["2025-06-01"]["stock_pnl"] == 130.0
    assert out["2025-06-01"]["fx_pnl"] == 40.0
    assert out["2025-06-01"]["pnl"] == 170.0


def test_aggregate_excludes_fx_row_from_value_and_cost():
    rows = [
        {
            "date": "2025-12-31",
            "symbol": "AAA",
            "market_value": 100.0,
            "day_pnl": 10.0,
            "cost_basis": 90.0,
            "dividends_cumulative": 0,
            "total_return": 10.0,
        },
        {
            "date": "2025-12-31",
            "symbol": FX_ROW,
            "market_value": 0.0,
            "day_pnl": 134.5,
            "cost_basis": 0.0,
            "dividends_cumulative": 0,
            "total_return": 134.5,
        },
    ]
    out = aggregate_snapshot_timeseries(rows)
    assert out["2025-12-31"]["value"] == 100.0
    assert out["2025-12-31"]["cost"] == 90.0
    assert out["2025-12-31"]["pnl"] == 144.5
    assert out["2025-12-31"]["total_return"] == 144.5


# --- daily_linked_returns --------------------------------------------------


def test_daily_linked_returns_empty():
    assert daily_linked_returns([], [], []) == []


def test_daily_linked_returns_constant_value_and_pnl_is_flat():
    # No P&L change → return stays at 0
    r = daily_linked_returns(["2024-01-01", "2024-01-02", "2024-01-03"], [100, 100, 100], [0, 0, 0])
    assert r == [0.0, 0.0, 0.0]


def test_daily_linked_returns_cash_inflow_is_ignored():
    # value doubles from a deposit; P&L unchanged → return must stay at 0.
    r = daily_linked_returns(["2024-01-01", "2024-01-02"], [100, 200], [0, 0])
    assert r == [0.0, 0.0]


def test_daily_linked_returns_single_day_pnl():
    # $10 P&L on a $100 base → exactly 10% return.
    r = daily_linked_returns(["2024-01-01", "2024-01-02"], [100, 110], [0, 10])
    assert r[0] == 0.0
    assert r[1] == pytest.approx(10.0)


def test_daily_linked_returns_chains_correctly():
    # +10% on day 2 ($100→$110 via $10 P&L), then another +10% on day 3
    # ($110→$121 via $11 P&L). Cumulative = 21%.
    r = daily_linked_returns(
        ["2024-01-01", "2024-01-02", "2024-01-03"],
        [100, 110, 121],
        [0, 10, 21],
    )
    assert r[2] == pytest.approx(21.0, abs=1e-3)


def test_daily_linked_returns_length_mismatch_raises():
    with pytest.raises(AssertionError):
        daily_linked_returns(["a", "b"], [1.0], [0.0, 0.0])


# --- drawdown_series / max_drawdown_pct ------------------------------------


def test_drawdown_zero_on_monotonic_gains():
    cum = [0.0, 5.0, 10.0, 15.0]
    assert drawdown_series(cum) == [0.0, 0.0, 0.0, 0.0]
    assert max_drawdown_pct(cum) == 0.0


def test_drawdown_captures_peak_to_trough():
    # 0%, +20%, +10%, -5% relative to start
    cum = [0.0, 20.0, 10.0, -5.0]
    dd = drawdown_series(cum)
    # peak is at 120; trough is at 95 → 25/120 = 20.8333...%
    assert dd[-1] == pytest.approx(-25.0 / 120.0 * 100.0, abs=1e-6)
    assert max_drawdown_pct(cum) == pytest.approx(25.0 / 120.0 * 100.0, abs=1e-3)


# --- risk_metrics ----------------------------------------------------------


def test_risk_metrics_short_series():
    m = risk_metrics([100.0], [0.0])
    assert m == {
        "annualized_return": 0.0,
        "volatility": 0.0,
        "sharpe": 0.0,
        "max_drawdown_pct": 0.0,
    }


def test_risk_metrics_time_varying_rf_matches_scalar_when_constant():
    # 5 daily values; same Rf passed scalar vs per-date dict should yield same Sharpe.
    dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
    values = [100.0, 101.0, 100.5, 102.0, 103.0]
    pnl = [0.0, 1.0, 0.5, 2.0, 3.0]
    rf_const = 0.045
    rf_dict = dict.fromkeys(dates, 0.045)
    m_scalar = risk_metrics(values, pnl, risk_free_rate=rf_const)
    m_dict = risk_metrics(values, pnl, risk_free_rate=rf_dict, dates=dates)
    assert m_scalar["sharpe"] == m_dict["sharpe"]


def test_risk_metrics_time_varying_rf_responds_to_changes():
    # Higher Rf → lower Sharpe.
    dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
    values = [100.0, 101.0, 100.5, 102.0, 103.0]
    pnl = [0.0, 1.0, 0.5, 2.0, 3.0]
    low_rf = dict.fromkeys(dates, 0.01)
    high_rf = dict.fromkeys(dates, 0.10)
    s_low = risk_metrics(values, pnl, risk_free_rate=low_rf, dates=dates)["sharpe"]
    s_high = risk_metrics(values, pnl, risk_free_rate=high_rf, dates=dates)["sharpe"]
    assert s_low > s_high


def test_risk_metrics_constant_return():
    # Every day: +1% on previous value. Values compound; so does P&L.
    values = [100.0]
    pnl = [0.0]
    v = 100.0
    for _ in range(252):
        gain = v * 0.01
        pnl.append(pnl[-1] + gain)
        v += gain
        values.append(v)
    m = risk_metrics(values, pnl)
    # 252 days of exactly +1% → mean daily return 0.01, stdev ≈ 0
    assert m["annualized_return"] == pytest.approx(252.0, abs=1e-2)
    assert m["volatility"] == pytest.approx(0.0, abs=1e-2)
    assert m["sharpe"] == 0.0  # vol≈0 guard returns 0
    assert m["max_drawdown_pct"] == 0.0


# --- monthly_returns / year_returns_from_months ----------------------------


def test_monthly_returns_empty():
    assert monthly_returns([], []) == {}


def test_monthly_returns_compound_back_to_total():
    # Build a synthetic 14-month history: each month the index level
    # multiplies by a distinct factor. Daily-linked cumulative series has one
    # observation per month-end for simplicity.
    dates = [f"2024-{m:02d}-28" for m in range(1, 13)] + [f"2025-{m:02d}-28" for m in (1, 2)]
    factors = [1.01, 1.02, 0.99, 1.005, 1.03, 0.97, 1.01, 1.04, 1.0, 0.98, 1.02, 1.01, 1.015, 0.995]
    idx = 1.0
    cum = []
    for f in factors:
        idx *= f
        cum.append((idx - 1) * 100)
    monthly = monthly_returns(dates, cum)
    assert len(monthly) == 14
    # First month uses the anchor (index at dates[0] = factors[0] = 1.01 → 1%)
    assert monthly["2024-01"] == pytest.approx(0.0, abs=1e-6)
    # Second month chains to factors[1]
    assert monthly["2024-02"] == pytest.approx((1.02 - 1) * 100, abs=1e-3)
    # Compound monthly returns back to yearly
    yearly = year_returns_from_months(monthly)
    year_2024_level = 1.0
    for f in factors[:12]:
        year_2024_level *= f
    # But monthly_returns uses the first-day anchor, so Jan's return was 0%;
    # the compounded yearly is really factors[1..11], not 0..11.
    # Expected = product of factors[1..11] - 1.
    expected_2024 = math.prod(factors[1:12]) - 1
    assert yearly["2024"] == pytest.approx(expected_2024 * 100, abs=1e-2)


def test_monthly_returns_uses_latest_in_month():
    # Two observations in January; the later one drives January's monthly return.
    # Index levels: 1.00 → 1.05 → 1.10 → 1.155 (= 1.10 * 1.05).
    dates = ["2024-01-05", "2024-01-20", "2024-01-31", "2024-02-10"]
    cum = [0.0, 5.0, 10.0, 15.5]
    m = monthly_returns(dates, cum)
    # Jan: anchor 1.00 → last-in-month 1.10 → 10%
    assert m["2024-01"] == pytest.approx(10.0, abs=1e-3)
    # Feb: chains from Jan-end 1.10 to Feb-end 1.155 → 5%
    assert m["2024-02"] == pytest.approx(5.0, abs=1e-3)


# --- weights_by_currency ---------------------------------------------------


def test_weights_by_currency_empty():
    assert weights_by_currency([]) == {}


def test_weights_by_currency_single_ccy():
    out = weights_by_currency([{"currency": "USD", "market_value": 50.0}])
    assert out == {"USD": 100.0}


def test_weights_by_currency_multi_ccy_sums_to_100():
    positions = [
        {"currency": "USD", "market_value": 60.0},
        {"currency": "EUR", "market_value": 30.0},
        {"currency": "CAD", "market_value": 10.0},
    ]
    out = weights_by_currency(positions)
    assert sum(out.values()) == pytest.approx(100.0, abs=1e-6)
    assert out["USD"] == 60.0


def test_weights_by_currency_abs_values():
    # A short position shouldn't reduce the exposure weight.
    positions = [
        {"currency": "USD", "market_value": 100.0},
        {"currency": "EUR", "market_value": -100.0},
    ]
    out = weights_by_currency(positions)
    assert out == {"USD": 50.0, "EUR": 50.0}


# --- horizon_start_date ----------------------------------------------------


def test_horizon_start_date_ytd():
    assert horizon_start_date("ytd", date(2024, 7, 15)) == date(2024, 1, 1)


def test_horizon_start_date_mtd_qtd():
    assert horizon_start_date("mtd", date(2024, 7, 15)) == date(2024, 7, 1)
    assert horizon_start_date("qtd", date(2024, 7, 15)) == date(2024, 7, 1)
    assert horizon_start_date("qtd", date(2024, 8, 15)) == date(2024, 7, 1)
    assert horizon_start_date("qtd", date(2024, 2, 15)) == date(2024, 1, 1)
    assert horizon_start_date("qtd", date(2024, 12, 15)) == date(2024, 10, 1)


def test_horizon_start_date_all():
    assert horizon_start_date("all", date(2024, 7, 15)) == date(2000, 1, 1)


@pytest.mark.parametrize(
    "horizon,days",
    [
        ("1d", 1),
        ("5d", 5),
        ("1m", 30),
        ("3m", 90),
        ("6m", 180),
        ("1y", 365),
        ("3y", 1095),
        ("5y", 1825),
    ],
)
def test_horizon_start_date_rolling(horizon, days):
    today = date(2024, 7, 15)
    assert horizon_start_date(horizon, today) == today - timedelta(days=days)


def test_horizon_start_date_unknown_raises():
    with pytest.raises(AssertionError):
        horizon_start_date("bogus", date(2024, 1, 1))

"""Pure portfolio-math helpers. No I/O, no globals — easy to unit-test."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta

REALIZED_ROW = "__REALIZED__"
FX_ROW = "__FX__"
PSEUDO_ROWS = frozenset({REALIZED_ROW, FX_ROW})
TRADING_DAYS_PER_YEAR = 252


def aggregate_snapshot_timeseries(
    snapshots: Iterable[Mapping],
) -> dict[str, dict[str, float]]:
    """Group snapshot rows by date, summing the numerical fields **in USD**.

    Foreign-currency positions store ``market_value`` / ``cost_basis`` /
    ``day_pnl`` in their *local* currency, plus parallel ``*_usd``
    fields. Aggregation prefers the USD fields (so cross-position sums
    are valid); it falls back to the local field only when the USD
    field is absent (older rows pre-dating the schema migration, where
    we treat the row as USD already).

    ``value`` / ``cost`` are summed only across real positions (not the
    ``__REALIZED__`` / ``__FX__`` pseudo-rows). ``pnl``, ``stock_pnl``,
    ``fx_pnl``, ``dividends``, ``total_return`` include the pseudo-rows
    so realized gains and cash-FX translation flow through.
    """
    by_date: dict[str, dict[str, float]] = {}
    for s in snapshots:
        d = s["date"]
        bucket = by_date.setdefault(
            d,
            {
                "value": 0.0,
                "pnl": 0.0,
                "cost": 0.0,
                "dividends": 0.0,
                "total_return": 0.0,
                "stock_pnl": 0.0,
                "fx_pnl": 0.0,
            },
        )
        sym = s.get("symbol")
        mv_usd = s.get("market_value_usd")
        if mv_usd is None:
            mv_usd = s.get("market_value", 0) or 0
        cost_usd = s.get("cost_basis_usd")
        if cost_usd is None:
            cost_usd = s.get("cost_basis", 0) or 0
        stock_pnl = s.get("stock_pnl_usd")
        fx_pnl = s.get("fx_pnl_usd")
        if stock_pnl is None and fx_pnl is None:
            # Pre-multi-ccy row: treat all P&L as stock P&L in USD.
            stock_pnl = s.get("day_pnl", 0) or 0
            fx_pnl = 0.0
        else:
            stock_pnl = stock_pnl or 0
            fx_pnl = fx_pnl or 0
        if sym not in PSEUDO_ROWS:
            bucket["value"] += mv_usd
            bucket["cost"] += cost_usd
        bucket["pnl"] += stock_pnl + fx_pnl
        bucket["stock_pnl"] += stock_pnl
        bucket["fx_pnl"] += fx_pnl
        bucket["dividends"] += s.get("dividends_cumulative", 0) or 0
        bucket["total_return"] += s.get("total_return", 0) or 0
    return by_date


def daily_linked_returns(
    dates: Sequence[str], values: Sequence[float], pnl: Sequence[float]
) -> list[float]:
    """Chain daily returns into a cumulative-return index (percent).

    The per-day return is ``ΔP&L / prev_value`` — this isolates
    investment performance from cash flows (buys/sells/deposits that move
    ``market_value`` without representing a gain or loss).

    Returns a list the same length as ``dates``, starting at 0.0.
    """
    assert len(dates) == len(values) == len(pnl), "inputs must be same length"
    if not dates:
        return []
    out = [0.0]
    idx = 1.0
    for i in range(1, len(dates)):
        prev_val = values[i - 1]
        pnl_change = pnl[i] - pnl[i - 1]
        daily = pnl_change / prev_val if prev_val else 0.0
        idx *= 1 + daily
        out.append(round((idx - 1) * 100, 4))
    return out


def drawdown_series(return_pct: Sequence[float]) -> list[float]:
    """Convert a cumulative-return series (percent) into a drawdown series (percent, ≤0)."""
    if not return_pct:
        return []
    index = [100 * (1 + r / 100) for r in return_pct]
    out = []
    peak = index[0]
    for v in index:
        peak = max(peak, v)
        out.append(((v - peak) / peak) * 100 if peak else 0.0)
    return out


def max_drawdown_pct(return_pct: Sequence[float]) -> float:
    """Worst peak-to-trough drawdown as a non-negative percent."""
    dd = drawdown_series(return_pct)
    if not dd:
        return 0.0
    # abs() normalizes -0.0 to 0.0 for monotonically increasing series
    return round(abs(min(dd)), 4)


def risk_metrics(
    values: Sequence[float],
    pnl: Sequence[float],
    trading_days: int = TRADING_DAYS_PER_YEAR,
    risk_free_rate: float = 0.0,
) -> dict[str, float]:
    """Annualized return, volatility, Sharpe, and max drawdown from value + P&L series.

    ``risk_free_rate`` is the *annualized* risk-free rate as a decimal
    (0.045 = 4.5%). It's converted to a per-period rate before being
    subtracted from each daily return for the Sharpe numerator.
    """
    n_obs = len(values)
    assert n_obs == len(pnl)
    if n_obs < 2:
        return {"annualized_return": 0.0, "volatility": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0}

    daily = []
    for i in range(1, n_obs):
        prev = values[i - 1]
        change = pnl[i] - pnl[i - 1]
        daily.append(change / prev if prev else 0.0)

    n = len(daily)
    mean_r = sum(daily) / n
    var_r = sum((r - mean_r) ** 2 for r in daily) / max(n - 1, 1)
    std_r = math.sqrt(var_r)
    ann_return = mean_r * trading_days * 100
    ann_vol = std_r * math.sqrt(trading_days) * 100
    daily_rf = risk_free_rate / trading_days
    excess_mean = mean_r - daily_rf
    # Guard against FP noise near-zero: if volatility is effectively zero,
    # Sharpe is undefined — report 0 rather than a blown-up ratio.
    sharpe = (
        (excess_mean * trading_days) / (std_r * math.sqrt(trading_days)) if ann_vol > 1e-9 else 0.0
    )

    cum = daily_linked_returns(
        [""] * n_obs, values, pnl
    )  # dates are unused; any same-length sequence works
    return {
        "annualized_return": round(ann_return, 2),
        "volatility": round(ann_vol, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": max_drawdown_pct(cum),
    }


def _sharpe_from_returns(
    period_returns: Sequence[float],
    periods_per_year: int,
    risk_free_rate: float,
) -> float:
    """Annualized Sharpe from a sequence of *per-period* arithmetic returns
    (decimals, not percent). Returns 0 when stdev is effectively zero."""
    n = len(period_returns)
    if n < 2:
        return 0.0
    mean_r = sum(period_returns) / n
    var_r = sum((r - mean_r) ** 2 for r in period_returns) / max(n - 1, 1)
    std_r = math.sqrt(var_r)
    if std_r < 1e-12:
        return 0.0
    period_rf = risk_free_rate / periods_per_year
    excess = mean_r - period_rf
    return (excess * periods_per_year) / (std_r * math.sqrt(periods_per_year))


def sharpe_by_frequency(
    dates: Sequence[str],
    values: Sequence[float],
    pnl: Sequence[float],
    risk_free_rate: float = 0.0,
    trading_days: int = TRADING_DAYS_PER_YEAR,
) -> dict[str, float]:
    """Annualized Sharpe ratios computed from daily, weekly, and monthly
    cash-flow-adjusted returns derived from ``values`` + ``pnl``.

    The daily return series is built from ``ΔP&L / prev_value`` (same as
    ``daily_linked_returns``). For weekly/monthly we chain the daily
    returns into period-end index levels, then take period-over-period
    arithmetic returns. Each is annualized using its native period count
    (252 / 52 / 12)."""
    assert len(dates) == len(values) == len(pnl)
    if len(dates) < 2:
        return {"daily": 0.0, "weekly": 0.0, "monthly": 0.0}

    # Daily arithmetic returns (cash-flow neutral).
    daily = []
    for i in range(1, len(dates)):
        prev = values[i - 1]
        change = pnl[i] - pnl[i - 1]
        daily.append(change / prev if prev else 0.0)

    # Build a daily index level so we can resample to weekly/monthly.
    levels = [1.0]
    for r in daily:
        levels.append(levels[-1] * (1 + r))
    # `levels` aligns with `dates`: levels[i] is the index level on dates[i].
    assert len(levels) == len(dates)

    def _iso_year_week(d: str) -> tuple[int, int]:
        y, m, day = (int(x) for x in d.split("-"))
        iso = date(y, m, day).isocalendar()
        return (iso[0], iso[1])

    last_level_by_week: dict[tuple[int, int], float] = {}
    last_level_by_month: dict[str, float] = {}
    for d, lvl in zip(dates, levels, strict=True):
        last_level_by_week[_iso_year_week(d)] = lvl
        last_level_by_month[d[:7]] = lvl

    def _period_returns(levels_by_key: dict) -> list[float]:
        out = []
        prev = None
        for k in sorted(levels_by_key.keys()):
            cur = levels_by_key[k]
            if prev is not None and prev > 0:
                out.append(cur / prev - 1)
            prev = cur
        return out

    weekly = _period_returns(last_level_by_week)
    monthly = _period_returns(last_level_by_month)

    return {
        "daily": round(_sharpe_from_returns(daily, trading_days, risk_free_rate), 2),
        "weekly": round(_sharpe_from_returns(weekly, 52, risk_free_rate), 2),
        "monthly": round(_sharpe_from_returns(monthly, 12, risk_free_rate), 2),
    }


def monthly_returns(dates: Sequence[str], cumulative_pct: Sequence[float]) -> dict[str, float]:
    """Chain a daily cumulative-return series into monthly returns (percent).

    ``cumulative_pct[t]`` is assumed to be ``(idx_t / idx_0 - 1) * 100`` for
    a common base ``idx_0`` — the format produced by :func:`daily_linked_returns`.
    The monthly return is the chain ratio of index levels between the last
    observation of the prior month (or the anchor at t=0) and the last
    observation of the current month.
    """
    assert len(dates) == len(cumulative_pct)
    if not dates:
        return {}
    last_level_by_month: dict[str, float] = {}
    for d, r in zip(dates, cumulative_pct, strict=True):
        last_level_by_month[d[:7]] = 1 + r / 100

    months_sorted = sorted(last_level_by_month.keys())
    anchor_level = 1 + cumulative_pct[0] / 100
    result: dict[str, float] = {}
    prev_level = anchor_level
    for m in months_sorted:
        end_level = last_level_by_month[m]
        ret = (end_level / prev_level - 1) * 100 if prev_level else 0.0
        result[m] = round(ret, 4)
        prev_level = end_level
    return result


def year_returns_from_months(monthly: Mapping[str, float]) -> dict[str, float]:
    """Compound monthly returns (percent) into yearly returns (percent)."""
    by_year: dict[str, float] = {}
    for month in sorted(monthly.keys()):
        year = month[:4]
        prev_index = by_year.get(year, 1.0)
        by_year[year] = prev_index * (1 + monthly[month] / 100)
    return {y: round((lvl - 1) * 100, 4) for y, lvl in by_year.items()}


def weights_by_currency(positions: Iterable[Mapping]) -> dict[str, float]:
    """Percent exposure by currency. Market values are summed within-currency
    (no FX conversion); this surfaces FX risk rather than hiding it."""
    by_ccy: dict[str, float] = defaultdict(float)
    for p in positions:
        ccy = p.get("currency") or "UNKNOWN"
        by_ccy[ccy] += abs(p.get("market_value", 0) or 0)
    total = sum(by_ccy.values())
    if not total:
        return {}
    return {k: round(v / total * 100, 2) for k, v in by_ccy.items()}


def horizon_start_date(horizon: str, today: date) -> date:
    """Map a horizon pill label to the earliest date it should cover."""
    horizon = horizon.lower()
    if horizon == "ytd":
        return date(today.year, 1, 1)
    if horizon == "mtd":
        return date(today.year, today.month, 1)
    if horizon == "qtd":
        q_month = ((today.month - 1) // 3) * 3 + 1
        return date(today.year, q_month, 1)
    if horizon == "all":
        return date(2000, 1, 1)
    day_map = {"1d": 1, "5d": 5, "1m": 30, "3m": 90, "6m": 180, "1y": 365, "3y": 1095, "5y": 1825}
    days = day_map.get(horizon)
    assert days is not None, f"unknown horizon {horizon!r}"
    return today - timedelta(days=days)

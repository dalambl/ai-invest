"""Historical FX rate fetcher (USD per local currency).

USD is always 1.0. Foreign currencies are fetched from Yahoo Finance
using the ``<CCY>=X`` ticker convention (e.g. ``EUR=X``), which gives
the close price as USD per 1 unit of the foreign currency.
"""

from __future__ import annotations

from collections.abc import Iterable

import yfinance as yf

# Yahoo "X" tickers return the *spot* of `<CCY>USD` close, i.e. USD per 1 CCY.
_YAHOO_FX = {
    "EUR": "EURUSD=X",
    "CAD": "CADUSD=X",
    "DKK": "DKKUSD=X",
    "GBP": "GBPUSD=X",
    "JPY": "JPYUSD=X",
    "CHF": "CHFUSD=X",
    "AUD": "AUDUSD=X",
    "NOK": "NOKUSD=X",
    "SEK": "SEKUSD=X",
    "HKD": "HKDUSD=X",
}


def fetch_fx_rates(
    currencies: Iterable[str], start_date: str, end_date: str
) -> dict[str, dict[str, float]]:
    """Return ``{currency: {date: usd_per_local}}``. USD entries are absent
    (callers should default to 1.0). Unknown currencies raise."""
    out: dict[str, dict[str, float]] = {}
    for ccy in sorted(set(currencies)):
        if ccy == "USD":
            continue
        ticker_sym = _YAHOO_FX.get(ccy)
        assert ticker_sym, f"unknown FX ticker for currency {ccy!r} — extend _YAHOO_FX"
        ticker = yf.Ticker(ticker_sym)
        df = ticker.history(start=start_date, end=end_date, auto_adjust=False)
        if df.empty:
            out[ccy] = {}
            continue
        out[ccy] = {
            dt.strftime("%Y-%m-%d"): round(float(row["Close"]), 6) for dt, row in df.iterrows()
        }
    return out


def rate_on(rates: dict[str, float], dt: str, default: float = 1.0) -> float:
    """Return the FX rate at or before ``dt``. Falls back to the earliest
    available rate, then to ``default`` (caller-chosen) if none."""
    if not rates:
        return default
    if dt in rates:
        return rates[dt]
    available = sorted(rates.keys())
    earlier = [d for d in available if d <= dt]
    if earlier:
        return rates[earlier[-1]]
    return rates[available[0]]

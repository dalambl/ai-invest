"""3-Month US Treasury (DGS3MO) rates from FRED — used as the risk-free
rate in Sharpe calculations.

DGS3MO is published in *annualized percent*; we convert to a decimal
and forward-fill weekends/holidays so per-day Rf lookup is gap-free
over the cached range.
"""

from __future__ import annotations

from datetime import date, datetime

import fred

SERIES_ID = "DGS3MO"


def _forward_fill(rates: dict[str, float], end: str) -> dict[str, float]:
    """Fill every calendar day from min(rates) to ``end`` with the last
    known observation. Lets callers do simple per-date lookups without
    weekend/holiday gaps."""
    if not rates:
        return {}
    days = sorted(rates.keys())
    start_d = datetime.strptime(days[0], "%Y-%m-%d").date()
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    if end_d < start_d:
        return dict(rates)
    out: dict[str, float] = {}
    last = rates[days[0]]
    cur = start_d
    while cur <= end_d:
        key = cur.isoformat()
        if key in rates:
            last = rates[key]
        out[key] = last
        cur = date.fromordinal(cur.toordinal() + 1)
    return out


def load_dgs3mo(end_date: str | None = None) -> dict[str, float]:
    """Return DGS3MO as ``{YYYY-MM-DD: annualized_decimal}`` forward-filled
    through ``end_date`` (or today)."""
    raw_pct = fred.fetch_series(SERIES_ID)
    raw_decimal = {d: v / 100.0 for d, v in raw_pct.items()}
    return _forward_fill(raw_decimal, end_date or date.today().isoformat())


def rate_on(rates: dict[str, float], dt: str, default: float = 0.0) -> float:
    """Annualized decimal Rf at or before ``dt``. Falls back to the
    earliest observation, then ``default`` if the series is empty."""
    if not rates:
        return default
    if dt in rates:
        return rates[dt]
    available = sorted(rates.keys())
    earlier = [d for d in available if d <= dt]
    if earlier:
        return rates[earlier[-1]]
    return rates[available[0]]

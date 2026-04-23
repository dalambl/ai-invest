"""Parse IB Activity Statement CSV (year-end / period statements).

The transaction-history CSV used by ``rebuild_history.py`` only carries
buys / sells / dividends and lacks IB-computed cost bases, FX P/L, and
end-of-period positions. The Activity Statement (``U*_YYYY_YYYY.csv``)
is much richer and is used here as ground truth for end-of-period values
and FX P/L.
"""

from __future__ import annotations

import contextlib
import csv
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_MONTHS = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}


@dataclass
class OpenPosition:
    asset_category: str  # "Stocks" | "Equity and Index Options" | ...
    currency: str
    symbol: str
    quantity: float
    multiplier: float
    cost_price: float
    cost_basis: float
    close_price: float
    value: float
    unrealized_pnl: float


@dataclass
class StatementSummary:
    period_start: str  # YYYY-MM-DD
    period_end: str  # YYYY-MM-DD
    open_positions: list[OpenPosition] = field(default_factory=list)
    realized_by_symbol: dict[str, float] = field(default_factory=dict)
    unrealized_by_symbol: dict[str, float] = field(default_factory=dict)
    realized_total: float = 0.0
    unrealized_total: float = 0.0
    fx_realized: float = 0.0  # Cash FX Translation Gain/Loss (base currency)
    fx_unrealized: float = 0.0  # Sum of unrealized FX position P/L


def _parse_period(value: str) -> tuple[str, str]:
    # "January 1, 2025 - December 31, 2025"
    start, end = (s.strip() for s in value.split(" - "))
    return _to_iso(start), _to_iso(end)


def _to_iso(human: str) -> str:
    # "December 31, 2025"
    parts = human.replace(",", "").split()
    assert len(parts) == 3, f"unexpected date format: {human!r}"
    month = _MONTHS[parts[0]]
    day = int(parts[1])
    year = int(parts[2])
    return f"{year:04d}-{month:02d}-{day:02d}"


def _rows(path: Path) -> Iterator[list[str]]:
    with open(path, encoding="utf-8-sig") as f:
        yield from csv.reader(f)


def _f(s: str) -> float:
    return float(s) if s not in ("", "--") else 0.0


def parse(path: Path) -> StatementSummary:
    period_start = period_end = ""
    summary = StatementSummary(period_start="", period_end="")

    for row in _rows(path):
        if len(row) < 4:
            continue

        sec, hdr = row[0], row[1]

        if sec == "Statement" and hdr == "Data" and len(row) >= 4 and row[2] == "Period":
            period_start, period_end = _parse_period(row[3])
            summary.period_start = period_start
            summary.period_end = period_end

        # Open Positions: data rows describe each lot/summary line.
        # Header column count varies between Stocks (17) and Options (17 too).
        if sec == "Open Positions" and hdr == "Data" and row[2] == "Summary":
            try:
                op = OpenPosition(
                    asset_category=row[3],
                    currency=row[4],
                    symbol=row[5],
                    quantity=_f(row[6]),
                    multiplier=_f(row[7]),
                    cost_price=_f(row[8]),
                    cost_basis=_f(row[9]),
                    close_price=_f(row[10]),
                    value=_f(row[11]),
                    unrealized_pnl=_f(row[12]),
                )
            except (IndexError, ValueError):
                continue
            summary.open_positions.append(op)

        # Realized & Unrealized Performance Summary — per-symbol Data rows
        if (
            sec == "Realized & Unrealized Performance Summary"
            and hdr == "Data"
            and len(row) >= 17
            and row[2] not in ("Total", "Total (All Assets)")
        ):
            asset_cat = row[2]
            symbol = row[3]
            try:
                realized_total = _f(row[9])
                unrealized_total = _f(row[14])
            except (IndexError, ValueError):
                continue
            # Skip header rows (Asset Category column is the literal "Asset Category")
            if asset_cat in ("Asset Category", ""):
                continue
            # Track per-symbol P/L; for forex, accumulate into fx_*
            if asset_cat == "Forex":
                summary.fx_unrealized += unrealized_total
            else:
                summary.realized_by_symbol[symbol] = (
                    summary.realized_by_symbol.get(symbol, 0.0) + realized_total
                )
                summary.unrealized_by_symbol[symbol] = (
                    summary.unrealized_by_symbol.get(symbol, 0.0) + unrealized_total
                )

        # "Total (All Assets)" line carries the cross-category totals
        if (
            sec == "Realized & Unrealized Performance Summary"
            and hdr == "Data"
            and len(row) >= 17
            and row[2] == "Total (All Assets)"
        ):
            summary.realized_total = _f(row[9])
            summary.unrealized_total = _f(row[14])

        # Cash Report → "Cash FX Translation Gain/Loss" → Base Currency Summary
        if (
            sec == "Cash Report"
            and hdr == "Data"
            and len(row) >= 5
            and row[2] == "Cash FX Translation Gain/Loss"
            and row[3] == "Base Currency Summary"
        ):
            with contextlib.suppress(IndexError, ValueError):
                summary.fx_realized = _f(row[4])

    return summary


def find_statements(data_dir: Path) -> list[Path]:
    """Return sorted list of activity-statement CSVs in ``data_dir``.

    Files are expected to follow IB's naming convention
    ``U<account>_<startYear>_<endYear>.csv`` and live alongside
    ``trading.db`` (i.e. directly in ``data/``).
    """
    return sorted(data_dir.glob("U*_[0-9][0-9][0-9][0-9]_[0-9][0-9][0-9][0-9].csv"))


def parse_when_generated(path: Path) -> str | None:
    """Return ISO timestamp from ``Statement,Data,WhenGenerated`` if present."""
    for row in _rows(path):
        if (
            len(row) >= 4
            and row[0] == "Statement"
            and row[1] == "Data"
            and row[2] == "WhenGenerated"
        ):
            try:
                # "2026-01-15, 18:56:16 EDT" → take date part
                return (
                    datetime.strptime(row[3].split(",")[0].strip(), "%Y-%m-%d").date().isoformat()
                )
            except (ValueError, IndexError):
                return None
    return None

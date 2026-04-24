"""Generic FRED (St. Louis Fed) series fetcher.

Uses the public ``fredgraph.csv?id=<series>`` endpoint — no API key
required. Each series is cached on disk at
``data/fred_<series_id>.csv`` and refreshed at most once per day.
Returns raw FRED values (e.g. percent for rates, level for indices) —
callers are responsible for unit conversions and forward-filling.
"""

from __future__ import annotations

import csv
import urllib.request
from datetime import date, datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def _url(series_id: str) -> str:
    return f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"


def _cache_path(series_id: str) -> Path:
    return DATA_DIR / f"fred_{series_id}.csv"


def _is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    return datetime.fromtimestamp(path.stat().st_mtime).date() == date.today()


def _download(series_id: str) -> str:
    with urllib.request.urlopen(_url(series_id), timeout=30) as resp:
        return resp.read().decode("utf-8")


def _parse_csv(text: str) -> dict[str, float]:
    """Return ``{YYYY-MM-DD: value}``. Drops missing-data rows (FRED
    uses ``.`` for holidays / unobserved dates)."""
    out: dict[str, float] = {}
    reader = csv.reader(text.splitlines())
    header = next(reader, None)
    assert header and len(header) >= 2, f"unexpected FRED header: {header}"
    for row in reader:
        if len(row) < 2:
            continue
        d, v = row[0], row[1].strip()
        if not v or v == ".":
            continue
        out[d] = float(v)
    return out


def _save(series_id: str, rates: dict[str, float]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(_cache_path(series_id), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "value"])
        for d in sorted(rates.keys()):
            w.writerow([d, f"{rates[d]:.6f}"])


def _load(series_id: str) -> dict[str, float]:
    out: dict[str, float] = {}
    with open(_cache_path(series_id)) as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) < 2:
                continue
            out[row[0]] = float(row[1])
    return out


def fetch_series(series_id: str) -> dict[str, float]:
    """Return ``{YYYY-MM-DD: raw_value}`` for the FRED series, refetching
    from FRED when the cache is older than today."""
    series_id = series_id.upper()
    path = _cache_path(series_id)
    if _is_fresh(path):
        return _load(series_id)
    parsed = _parse_csv(_download(series_id))
    if not parsed:
        # Don't overwrite a good cache with empty data on a transient failure.
        if path.exists():
            return _load(series_id)
        raise ValueError(f"FRED series {series_id!r} returned no observations")
    _save(series_id, parsed)
    return parsed

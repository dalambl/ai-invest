# Industry mapping — design plan

We want a stable, queryable mapping from each portfolio symbol to one or
more industries (and ideally a sector / sub-industry hierarchy). Single
sector classification is too coarse for the kind of risk and exposure
slicing we want to do (e.g. NVDA is *Semiconductors* but is also *AI
infrastructure*; XOM is *Oil & Gas* but also *Dividends/Income*; UNH is
*Managed Care* and *Healthcare Insurance*). The mapping should be
many-to-many.

## Goals

1. Each stock can belong to multiple industries.
2. Industries form a controlled vocabulary (no free-form drift).
3. Mapping is stored locally so the dashboard works offline.
4. Refresh is incremental: when a new symbol enters the portfolio, we
   look it up automatically.
5. Source is auditable — we record where each tag came from so that we
   can re-classify if the source changes its taxonomy.

## Data-source options

Options ranked roughly by usefulness for this project.

| # | Source | Multi-industry? | Cost | Stability | Notes |
|---|--------|-----------------|------|-----------|-------|
| 1 | **Yahoo Finance via `yfinance`** (`Ticker(...).info["sector"]`, `["industry"]`, `["industryKey"]`) | No (single sector + single industry) | Free | Medium — taxonomy occasionally changes | Already a dependency. Easiest first pass; gives ~80% coverage. |
| 2 | **GICS via Wikipedia** (S&P 500 / S&P/TSX 60 component lists) | No (single Sector + single Sub-Industry) | Free | High (GICS is a standard) | Best taxonomy quality; only covers index constituents. Scrape Wikipedia tables once per quarter. |
| 3 | **OpenFIGI** (`https://www.openfigi.com/api`) | Yes (CFI + classification) | Free up to 25 req/min | High | Returns figiCode + market sector (e.g. *Equity*, *Corp*) but not deep industry; useful as ID-canonicalizer. |
| 4 | **SEC EDGAR company facts** (`https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json`) | Yes (SIC code + business segments) | Free | High | SIC codes are dated but stable; segment data via XBRL Items per filing — can derive multi-industry exposure from segment revenue. US listings only. |
| 5 | **FMP (financialmodelingprep.com)** `profile` endpoint | No (sector + industry) | Free tier 250 req/day | Medium | Mostly mirrors Yahoo. |
| 6 | **Refinitiv / Bloomberg / FactSet / MSCI** (TRBC, BICS, GICS subscriptions) | Yes (TRBC has up to 5 levels; FactSet RBICS has multi-industry weights) | $$$ | Highest | Out of scope for now. |
| 7 | **Manual overrides** (`data/industry_overrides.csv`) | Yes | Free | n/a | Always needed for the long tail (options, EU/CA tickers, ETF themes). |
| 8 | **ETF holdings expansion** (e.g. issuer JSON feeds — iShares, SPDR, Vanguard) | Yes (each ETF has many industries by holdings weight) | Free | Medium | Required if we want ETFs (XLE, INDA, FXI, etc.) to contribute industry exposure rather than appearing as a single bucket. |

## Recommended approach

A **layered resolver** with explicit precedence so we always know where
a tag came from:

1. **Manual overrides** (`data/industry_overrides.csv`) — wins
   everything. Schema:
   `symbol,industry,source,notes` — many rows per symbol allowed.
   Used for: option contracts, ETFs we want re-tagged thematically
   (e.g. "AI", "Defense", "Income"), foreign tickers Yahoo gets wrong.
2. **GICS-from-Wikipedia** for known index constituents (S&P 500,
   S&P/TSX 60, EURO STOXX 50). Cached locally as a single CSV per
   index, refreshed quarterly. Provides Sector + Sub-Industry.
3. **Yahoo `info`** as a catch-all for everything not covered by 1–2.
   Provides Sector + Industry (single each).
4. **SEC EDGAR SIC** (only for US-listed CIKs that resolve) as a
   secondary tag for cross-checking and to add a coarse
   government-standard tag.
5. **ETF expansion** — for each ETF position, fetch the issuer's
   holdings JSON, multiply the position's USD market value by each
   holding's weight, and add to industry exposure proportionally.
   Stored as a separate `etf_holdings` cache keyed by ETF symbol +
   as-of date.

A symbol's final industry tag set = union of all layers that returned a
result, deduplicated. We keep the per-source provenance so the UI can
say "tagged *Defense* by override; tagged *Aerospace & Defense* by
GICS".

## Storage

New tables in `data/trading.db`:

```sql
CREATE TABLE IF NOT EXISTS industries (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    parent_id INTEGER REFERENCES industries(id),  -- for sector → sub-industry
    taxonomy TEXT  -- 'GICS', 'YAHOO', 'SIC', 'CUSTOM'
);

CREATE TABLE IF NOT EXISTS symbol_industries (
    symbol TEXT NOT NULL,
    industry_id INTEGER NOT NULL REFERENCES industries(id),
    weight REAL DEFAULT 1.0,  -- for ETF expansion: fractional weight
    source TEXT NOT NULL,     -- 'override', 'wiki_gics', 'yahoo', 'sec_sic', 'etf_holdings'
    as_of DATE,
    PRIMARY KEY (symbol, industry_id, source)
);
```

Weights matter for ETFs (a holding's industry slice is a fraction of
the ETF's market value). For non-ETF stocks, default weight = 1.0. The
sum of weights for a single (symbol, source) tuple ≤ 1.0 only matters
for ETF expansion; for stocks with multiple non-ETF tags, weights stay
at 1.0 each (the symbol contributes its full exposure to each
industry, and exposure aggregation in the UI normalizes per slice).

## Initial implementation

Smallest useful slice (one PR / notes-worthy commit):

1. New module `industry.py` exposing:
   - `resolve_industries(symbol: str) -> list[Tag]` (Tag = `(name, source, weight)`).
   - `refresh_all(symbols: Iterable[str], db) -> None` — populates the
     two tables.
2. Wire `refresh_all` into the end of `rebuild_history.main()` so it
   picks up new symbols on every rebuild.
3. New `/api/exposure/industry` endpoint that joins `symbol_industries`
   with the latest snapshot's `market_value_usd` and groups by
   industry, summing `weight * market_value_usd`.
4. New "Exposure → Industry" pie/bar in the dashboard alongside the
   existing sector pie.
5. Manual-override CSV ships with seed entries for the user's options
   and any tickers Yahoo can't classify.

## Open questions before implementation

- Do we want a **single canonical sector** (for the existing exposure
  pie) plus the multi-tag list, or replace the existing
  sector pie with the multi-tag breakdown?
- Should the override CSV live under `data/` (gets backed up, not
  version-controlled) or in the repo (version-controlled but exposes
  the user's symbols in git history)?
- For ETFs, do we expand into industries (preferred) or keep them as a
  single "ETF: <theme>" tag for simplicity? The user holds several
  thematic ETFs (XLE, XLI, INDA, FXI, VWO, IDV, PXH, HYG, SHY, SPY)
  where expansion would change the picture significantly.

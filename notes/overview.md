# Project overview

A personal investment platform: pulls positions and trades from Interactive
Brokers TWS, stores daily snapshots in SQLite + CSV, and serves a browser
dashboard (FastAPI + plain HTML/JS) with holdings, P&L across horizons,
performance vs benchmark, exposure, and risk metrics.

## Current status

**2026-04-21** — Verify/fix pass complete (see
[002](002-verify-and-fix.md)). Tooling on uv + Python 3.13. New pure
`finance.py` module + 32 passing tests. Frontend monthly-returns grid
fixed. Multiple bugs fixed (asyncio deprecation, exposure alias, currency
mixing surfaced). Full check pipeline (`ruff format`, `ruff check`,
`pytest`, `ty check`) is green. `python-multipart` added so `server.py`
imports cleanly. No commits made (user did not authorize).

## Plan

- Read and document the frontend (`static/`) in its own notes file.
- Design pass for multi-currency (base currency + FX source + snapshot schema).
- Design pass for cash-balance tracking in snapshots.

## Notes index

| # | Topic | Summary |
|---|-------|---------|
| [001](001-initial-repo-survey.md) | Initial repo survey | Module-by-module map of backend; data layout; key design decisions (realized-P&L pseudo-row, price fallback chain, daily-linked returns, gated snapshot saves); mismatches between `plan.md` / `CLAUDE.md` / code. |
| [002](002-verify-and-fix.md) | Verify and fix | Audit findings (monthly-grid bugs, exposure alias, asyncio deprecation, multi-currency, tooling drift), fixes applied, new `finance.py` + 32 tests, full check pipeline green (ruff/pytest/ty), `python-multipart` added, `ibkr.py` ty errors cleared. |

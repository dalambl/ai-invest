# Multi-CSV transaction loading + dedup

The activity statement CSV (`U640574.TRANSACTIONS.20221230.20260306.csv`)
ended 2026-03-06, but the user continued trading into April. Any
symbol bought after March 6 — e.g. AIR (bought 2026-04-21) —
looked "pre-existing" to `rebuild_history`, got its `purchase_date`
stamped as the first CSV date (2023-01-31), and was back-seeded into
833 days of phantom snapshot history. Purchase date in the UI was a
lie by 3 years.

## Change

- `CSV_PATH` replaced with `CSV_GLOB = "U640574.TRANSACTIONS*.csv"`.
- `main()` globs `data/U640574.TRANSACTIONS*.csv`, parses each with
  `parse_transactions`, concatenates, then deduplicates via
  `_dedupe_transactions` using key
  `(date, symbol, type, round(qty, 4), round(price, 4), round(net, 2))`.
- Supports overlapping statements: the user added a YTD export
  covering 2026-01-01 → 2026-04-22 alongside the 2022-12-30 →
  2026-03-06 historical export. Overlap region (Jan–Mar 2026) has
  34 duplicate rows, all dropped cleanly.

## Result

```
U640574.TRANSACTIONS.20221230.20260306.csv: 333 rows
U640574.TRANSACTIONS.YTD.csv:               113 rows
Found 412 transactions (34 duplicates dropped)
```

AIR now shows `purchased=2026-04-21, avg_cost=$202.83`; no longer in
`pre_existing`. Post-rebuild 2026-04-22 totals: MV $308,544.07,
Stock P&L $102,334.60, FX P&L $179.57, Dividends $11,011.22,
Total Return $113,525.39. All foreign holdings reconcile (Stock +
FX = Unreal USD) to the cent.

54 tests still pass; ruff/ty clean.

## Followup: stale-data risk

User wants regular automated re-exports so the CSV never gets
behind current holdings again. That needs IB Flex Web Service:
Flex Query ID + Token from IB Account Management, plus a small
fetcher (two-step `SendRequest` → poll `GetStatement`) and a
systemd user timer or cron entry. Credentials not yet supplied —
tracked as open work.

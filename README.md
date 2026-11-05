# SPY Gamma Structure & Regime Historical Archive (`horsey`)

Automated hourly (`8:00 AM – 3:00 PM CT`, weekdays) and post-close archive of SPY dealer gamma exposure (GEX), key call/put walls, gamma flip, `$735` strike node activity, 200-day SMA distance, VIX term structure, AI-complex (`NVDA`, `^SOX`, `SMH`, `SOXX`, `VRT`) & KBW Bank (`^BKX`, `KBWB`) relative strength, and Globex futures (`/ES`, `/NQ`).

Strictly verified public market data, levels, and regime description — zero trade ideas.

## 📊 Live Dashboard: **[https://brahaymin.github.io/horsey/](https://brahaymin.github.io/horsey/)**

Free, zero-login interactive dashboard (GitHub Pages) with a historical snapshot selector. Quick links: [`latest.md`](latest.md) · [`latest.json`](latest.json) · [`history/timeseries.csv`](history/timeseries.csv) · [`snapshots/`](snapshots/) ([index](snapshots/index.json)). All times Central Time (CT).

## Repository Layout

- `latest.md` / `latest.json` — Most recent snapshot in MCP-ready Markdown and structured JSON.
- `history/timeseries.jsonl` / `history/timeseries.csv` — Append-only hourly time-series log (one record per run) for historical recall, charting, and MCP ingestion.
- `snapshots/YYYY/MM/YYYY-MM-DD/<HHMM>-ct-<tag>.md` & `.json` — Immutable point-in-time hourly and post-close reports with full `$700–$810` strike-by-strike GEX profiles.
- `index.html` — Static GitHub Pages dashboard (reads `snapshots/index.json` + snapshot JSON).
- `scripts/spy_gamma_monitor.py` & `scripts/sync_github_archive.py` — Data pipeline and GitHub archive builder.
- `wash_sale_tracker_v2.py` & `workflows/wash_sale_tracker.yml` — Wash-sale tracking utility (preserved).

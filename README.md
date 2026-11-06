# SPY Gamma Structure & Regime Historical Archive (`horsey`)

Automated hourly (`8:00 AM – 3:00 PM CT`, weekdays) and post-close archive of SPY dealer gamma exposure (GEX), key call/put walls, gamma flip, `$735` strike node activity, 200-day SMA distance, VIX term structure, AI-complex (`NVDA`, `^SOX`, `SMH`, `SOXX`, `VRT`) & KBW Bank (`^BKX`, `KBWB`) relative strength, and Globex futures (`/ES`, `/NQ`).

Strictly verified public market data, levels, and regime description — zero trade ideas.

## 📊 Live Dashboard: **[https://brahaymin.github.io/horsey/](https://brahaymin.github.io/horsey/)**

Free, zero-login interactive dashboard (GitHub Pages) with a historical snapshot selector. Quick links: [`latest.md`](latest.md) · [`latest.json`](latest.json) · [`history/timeseries.csv`](history/timeseries.csv) · [`snapshots/`](snapshots/) ([index](snapshots/index.json)). All times Central Time (CT).

**Docs:** [Day-trading methodologies & 50 mistakes](docs/day_trading_methodologies_and_50_mistakes.md) · [Live feed spec (`SPYGammaFeed`)](docs/live_feed_spec.md)

**Agent Pod:** [CEO Architecture Manual](docs/agent_pod_architecture.md) · [Dashboard Tab 6 — Agent Pod & Blotter](https://brahaymin.github.io/horsey/#pod) · [`pod/roster.json`](pod/roster.json) · [`pod/scorecard.json`](pod/scorecard.json) · engine: `python3 scripts/pod_engine.py status|size-ticket|log-signal|grade` (paper / architecture demo — not financial advice)

## Repository Layout

- `latest.md` / `latest.json` — Most recent snapshot in MCP-ready Markdown and structured JSON.
- `history/timeseries.jsonl` / `history/timeseries.csv` — Append-only hourly time-series log (one record per run) for historical recall, charting, and MCP ingestion.
- `snapshots/YYYY/MM/YYYY-MM-DD/<HHMM>-ct-<tag>.md` & `.json` — Immutable point-in-time hourly and post-close reports with full `$700–$810` strike-by-strike GEX profiles.
- `index.html` — Static GitHub Pages dashboard v2 (tabs; never overwritten by this script; `js/feed-adapter.js` live-feed hook) (reads `snapshots/index.json` + snapshot JSON).
- `scripts/spy_gamma_monitor.py` & `scripts/sync_github_archive.py` — Data pipeline and GitHub archive builder.
- `wash_sale_tracker_v2.py` & `workflows/wash_sale_tracker.yml` — Wash-sale tracking utility (preserved).

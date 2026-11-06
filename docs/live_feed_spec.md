# Live Feed Spec: `window.SPYGammaFeed`

The dashboard (`index.html`) never fetches SPY/GEX snapshot data directly. It subscribes to **`js/feed-adapter.js`**:

```js
SPYGammaFeed.onSnapshot((snap, meta) => renderSnapshot(snap, meta)); // full re-render
SPYGammaFeed.onTick(tick => applyLiveTick(tick));                     // hero strip, level ruler, live badge
SPYGammaFeed.onStatus(s => ...);                                      // "Source: GitHub Archive · Auto-Refresh 60s"
SPYGammaFeed.onPoll(info => ...);                                     // after every pollNow (timer / wake / manual)
```

`LiveHub` in `index.html` also polls `pod/memos_index.json`, `pod/signals/latest_pod_state.json`, `pod/ledger/paper_blotter.jsonl`, `pod/ceo_questions.json`, and `pod/ceo_decisions/latest_decisions.json` on each `onPoll`, and raises in-dashboard toast banners when fingerprints change (never on initial boot baseline).

## Modes

| Mode | Config | Behavior |
|---|---|---|
| `github_archive` (default) | `pollSec`, `autoRefresh` | Loads `snapshots/index.json`, the selected snapshot JSON and `history/timeseries.jsonl`. Polls the index every `pollSec` seconds and auto-loads a newer snapshot if you are viewing the latest one. |
| `rest` | `restUrl`, `pollSec` | Polls `restUrl`. If the JSON has `gex_scopes`, it is rendered as a full snapshot (same schema as `latest.json`); otherwise it is applied as a tick. |
| `websocket` | `wsUrl` | Each message is either `{"type":"snapshot","data":{...latest.json schema...}}` or a tick object. |
| `alpaca_iex` | Paper keys in `localStorage["horsey_alpaca_paper"]` | Connects to `wss://stream.data.alpaca.markets/v2/iex`, auths with Paper key/secret, subscribes to SPY trades/quotes → `applyLiveTick`. **Keeps GitHub archive polling in parallel** for GEX snapshots + LiveHub briefings. |


Set the mode in the gear (⚙) modal, with `SPYGammaFeed.configure({mode:"rest", restUrl:"https://…", pollSec:15})`, or with URL params: `?feed=websocket&url=wss://…`.
The config is saved in the browser's `localStorage`.

## `pollNow()` / `checkUpdates()` + phone wake catch-up

```js
await SPYGammaFeed.pollNow("manual");   // alias: checkUpdates()
```

Fires on:

- the auto-refresh interval (`pollSec`, default 60s) when `autoRefresh` is enabled
- `document.visibilitychange` → `visible` (phone unlock / tab return)
- `window` `focus`
- `online`

Checks are throttled to **≥ 5 seconds** apart (pass `"force"` to bypass). Live fetches append `?_cb=<15s-bucket>` and use `{ cache: "no-store" }` so GitHub Pages `max-age=600` does not stall trading-hour updates.

## Tick schema (all fields optional)

```json
{ "ts": "2026-11-06T09:31:00-06:00", "spy_price": 731.2, "spy_change_pct": -0.25, "spy_volume": 12345678,
  "vix": 27.9, "vix9d": 31.0, "vix3m": 25.4, "es_price": 7360.25, "nq_price": 26600.5,
  "gex_45d": -13.1e9, "gex_0dte": -2.4e9, "zero_gamma_45d": 765.0, "call_wall_45d": 770, "put_wall_45d": 740 }
```

A tick updates the hero cards, the structural level ruler (spot marker and any wall or flip fields), and the LIVE badge. The charts and tables keep showing the last full snapshot.

## Custom mapping

If a vendor's payload has a different shape, define a mapper **before** the feed starts:

```js
window.SPYGammaFeedMapper = (json) => ({ tick: { spy_price: json.last, vix: json.vix_last } });
```

## In-dashboard banners

`LiveHub` (`#liveToastHost`) shows temporary high-contrast toasts for market snapshots, CIO/pod memos, CEO questions, and blotter tickets. Auto-dismiss (~14s, configurable in the Data Source modal) pauses on hover/touch. A 🔔 bell keeps the last 5 alerts. Use **🔔 Test Banner** in the Data Source modal or bell popover to preview on phone/desktop.

## Full snapshot schema
See any `snapshots/YYYY/MM/YYYY-MM-DD/*.json` (written by `scripts/sync_github_archive.py`). The keys are `summary`, `spy_detail`, `gex_scopes`, `intraday_30m`, `daily_15d`, `volatility_table`, `vol_term_structure`, `assets_table`, `futures_table`, `verified_headlines`, `gex_meta` and `md_rel_path`.

## Alpaca Paper Live Stream (`alpaca_iex`)

1. Save Paper Key ID + Secret in the Data Source modal (stored only in this browser).
2. Select **Alpaca Paper Live Stream**.
3. Adapter connects to `wss://stream.data.alpaca.markets/v2/iex` (WebSockets have no CORS), sends `{"action":"auth","key","secret"}`, then subscribes to SPY trades/quotes.
4. REST against `paper-api.alpaca.markets` also allows browser CORS (`Access-Control-Allow-Origin: *`) for optional account/positions refresh on Tab 6.
5. Server-side sync (no browser keys needed): `python3 scripts/pod_engine.py alpaca-status` writes sanitized state to `pod/paper_account_state.json` (private — `pod/` is gitignored on the public archive).

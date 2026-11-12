#!/usr/bin/env python3
"""Build structured historical archive files from /workspace/spy_gamma_snapshot.json,
commit inside /workspace/horsey (mirroring to persistent store), and push via git/gh when authenticated.
"""
import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

STORE_ROOT = "/cursor/stores/store-83bb580b-d47f-4617-88f0-eaae169aa882/spy-gamma-monitor"
REPO_DIR = "/workspace/horsey"
STORE_REPO_DIR = os.path.join(STORE_ROOT, "horsey")
SNAPSHOT_JSON = "/workspace/spy_gamma_snapshot.json"
REPORT_MD = "/workspace/spy_gamma_report.md"
SUMMARY_MD = "/workspace/spy_gamma_summary.md"

CT = ZoneInfo("America/Chicago")

CSV_COLUMNS = [
    "timestamp_ct",
    "timestamp_utc",
    "session_date",
    "run_tag",
    "spy_price",
    "spy_regular_close",
    "spy_1d_pct",
    "spy_volume",
    "spy_rel_vol_20d",
    "spy_sma_200",
    "spy_sma_200_dist_dollars",
    "spy_sma_200_dist_pct",
    "spy_sma_20",
    "spy_sma_50",
    "gex_spot_ref",
    "net_gex_0dte_dollars",
    "net_gex_45d_dollars",
    "net_gex_opex_dollars",
    "net_gex_all_dollars",
    "call_wall_0dte",
    "put_wall_0dte",
    "call_wall_45d",
    "put_wall_45d",
    "zero_gamma_0dte",
    "zero_gamma_45d",
    "node_735_0dte_net_gex",
    "node_735_0dte_call_oi",
    "node_735_0dte_put_oi",
    "node_735_0dte_call_vol",
    "node_735_0dte_put_vol",
    "node_735_45d_net_gex",
    "vix",
    "vix_1d_pct",
    "vix9d",
    "vix3m",
    "vvix",
    "nvda_1d_pct",
    "sox_1d_pct",
    "smh_1d_pct",
    "bkx_1d_pct",
    "kbwb_1d_pct",
    "es_quote",
    "nq_quote",
]

README_CONTENT = """# SPY Gamma Structure & Regime Historical Archive (`horsey`)

Automated hourly (`8:00 AM – 3:00 PM CT`, weekdays) and post-close archive of SPY dealer gamma exposure (GEX), key call/put walls, gamma flip, `$735` strike node activity, 200-day SMA distance, VIX term structure, AI-complex (`NVDA`, `^SOX`, `SMH`, `SOXX`, `VRT`) & KBW Bank (`^BKX`, `KBWB`) relative strength, and Globex futures (`/ES`, `/NQ`).

Strictly verified public market data, levels, and regime description — zero trade ideas.

## 📊 Live Dashboard: **[https://brahaymin.github.io/horsey/](https://brahaymin.github.io/horsey/)**

Free, zero-login interactive dashboard (GitHub Pages) with a historical snapshot selector. Quick links: [`latest.md`](latest.md) · [`latest.json`](latest.json) · [`history/timeseries.csv`](history/timeseries.csv) · [`snapshots/`](snapshots/) ([index](snapshots/index.json)). All times Central Time (CT).

**Docs:** [Day-trading methodologies & 50 mistakes](docs/day_trading_methodologies_and_50_mistakes.md) · [Live feed spec (`SPYGammaFeed`)](docs/live_feed_spec.md)

**CEO Briefings / Agent Pod:** [CEO Architecture Manual](docs/agent_pod_architecture.md) · [Dashboard Tab 1 — Briefings](https://brahaymin.github.io/horsey/#briefings) · [Tab 6 — Agent Pod & Blotter](https://brahaymin.github.io/horsey/#pod). The live `pod/` working directory is **private** (gitignored on this public archive) and is used locally by CIO/specialist agents + Alpaca Paper staging (`python3 scripts/pod_engine.py status|alpaca-status|alpaca-stage-ticket`). Paper / architecture demo — not financial advice.

## Repository Layout

- `latest.md` / `latest.json` — Most recent snapshot in MCP-ready Markdown and structured JSON.
- `history/timeseries.jsonl` / `history/timeseries.csv` — Append-only hourly time-series log (one record per run) for historical recall, charting, and MCP ingestion.
- `snapshots/YYYY/MM/YYYY-MM-DD/<HHMM>-ct-<tag>.md` & `.json` — Immutable point-in-time hourly and post-close reports with full `$700–$810` strike-by-strike GEX profiles.
- `index.html` — Static GitHub Pages dashboard v2 (tabs; never overwritten by this script; `js/feed-adapter.js` live-feed hook) (reads `snapshots/index.json` + snapshot JSON).
- `scripts/spy_gamma_monitor.py` & `scripts/sync_github_archive.py` — Data pipeline and GitHub archive builder.
- `wash_sale_tracker_v2.py` & `workflows/wash_sale_tracker.yml` — Wash-sale tracking utility (preserved).
"""


# --- shared repo lock (git/vault hygiene) -------------------------------------------------
# Self-contained (no import-path fragility). Reentrant across child processes via env flag:
# a holder exports HORSEY_REPO_LOCK_HELD=<pid> so subprocesses (vault_crypto, pod_engine) skip re-locking.
import contextlib as _ctxlib, fcntl as _fcntl, time as _time
REPO_SYNC_LOCK = "/tmp/horsey_repo_sync.lock"


@_ctxlib.contextmanager
def repo_sync_lock(lock_path=REPO_SYNC_LOCK, timeout_sec=120, label=""):
    if os.environ.get("HORSEY_REPO_LOCK_HELD"):
        yield True
        return
    fd = open(lock_path, "a+")
    t0 = _time.time(); warned = False
    while True:
        try:
            _fcntl.flock(fd.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if _time.time() - t0 > timeout_sec:
                fd.close()
                raise TimeoutError(f"repo_sync_lock timeout after {timeout_sec}s ({label})")
            if not warned:
                print(f"repo_sync_lock: waiting for {lock_path} ({label})", file=sys.stderr); warned = True
            _time.sleep(1)
    os.environ["HORSEY_REPO_LOCK_HELD"] = str(os.getpid())
    try:
        yield True
    finally:
        os.environ.pop("HORSEY_REPO_LOCK_HELD", None)
        try:
            _fcntl.flock(fd.fileno(), _fcntl.LOCK_UN)
        finally:
            fd.close()
# -------------------------------------------------------------------------------------------


def rnd(v, n=4):
    return round(v, n) if isinstance(v, (int, float)) else None


def safe_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def ensure_repo():
    tar_backup = os.path.join(STORE_ROOT, "horsey_repo_backup.tar.gz")
    if not os.path.exists(os.path.join(REPO_DIR, ".git")):
        if os.path.exists(tar_backup):
            os.makedirs(REPO_DIR, exist_ok=True)
            subprocess.run(["tar", "-xzf", tar_backup, "-C", "/workspace"], check=True)
        else:
            subprocess.run(["git", "clone", "https://github.com/brahaymin/horsey.git", REPO_DIR], check=True)
    subprocess.run(["git", "config", "user.name", "SPY Gamma Structure Monitor"], cwd=REPO_DIR, check=True)
    subprocess.run(["git", "config", "user.email", "brahaymin@users.noreply.github.com"], cwd=REPO_DIR, check=True)


ET = ZoneInfo("America/New_York")
INTRADAY_JSON = "/workspace/spy_gamma_data/charts_intraday_SPY.json"
DAILY_JSON = "/workspace/spy_gamma_data/yf_SPY_history.json"
VOL_SYMS = [("^VIX9D", "9-Day"), ("^VIX", "30-Day"), ("^VIX3M", "3-Month"), ("^VVIX", "Vol-of-VIX")]
ASSET_SYMS = ["CEG", "KBWB", "^BKX", "MSFT", "SPY", "QQQ", "XLK", "NVDA", "AVGO", "TSM", "ANET", "SMH", "SOXX", "^SOX", "VRT"]
FUT_SYMS = ["ES=F", "NQ=F", "RTY=F"]


def _ts_ct(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s))
    except ValueError:
        return s
    if d.tzinfo is None:
        d = d.replace(tzinfo=ET)
    return d.astimezone(CT).strftime("%Y-%m-%d %H:%M CT")


def build_intraday(session):
    if not os.path.exists(INTRADAY_JSON):
        return []
    data = json.load(open(INTRADAY_JSON)).get("data", [])
    buckets = {}
    for b in data:
        d = datetime.fromisoformat(b["datetime"]).replace(tzinfo=ET).astimezone(CT)
        if d.strftime("%Y-%m-%d") != session:
            continue
        # bar labelled 09:31 ET covers 09:30-09:31; bucket on bar start
        start = d.replace(second=0) - timedelta(minutes=1)
        slot = start.replace(minute=(start.minute // 30) * 30)
        k = slot.strftime("%H:%M")
        cur = buckets.setdefault(k, {"slot_ct": k, "close": None, "stock_vol": 0, "call_vol": 0, "put_vol": 0})
        cur["close"] = b["price"]["close"]
        v = b.get("volume", {})
        cur["stock_vol"] += v.get("stock_volume") or 0
        cur["call_vol"] += v.get("calls_volume") or 0
        cur["put_vol"] += v.get("puts_volume") or 0
    out = []
    for k in sorted(buckets):
        c = buckets[k]
        out.append({"slot_ct": k, "close": c["close"], "stock_vol_m": round(c["stock_vol"] / 1e6, 3),
                    "call_vol_k": round(c["call_vol"] / 1e3, 1), "put_vol_k": round(c["put_vol"] / 1e3, 1)})
    return out


def build_daily(session):
    if not os.path.exists(DAILY_JSON):
        return []
    rows = json.load(open(DAILY_JSON)).get("data", [])
    pts = []
    for r in rows:
        dt = datetime.fromisoformat(r["Date"].replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d")
        if dt <= session and r.get("Close") is not None:
            pts.append((dt, round(r["Close"], 4), r.get("Volume") or 0))
    out = []
    for i in range(max(0, len(pts) - 15), len(pts)):
        w200 = [p[1] for p in pts[max(0, i - 199): i + 1]]
        w20 = [p[1] for p in pts[max(0, i - 19): i + 1]]
        out.append({"date": pts[i][0], "close": pts[i][1], "volume_m": round(pts[i][2] / 1e6, 3),
                    "sma20": round(sum(w20) / len(w20), 4) if len(w20) == 20 else None,
                    "sma200": round(sum(w200) / len(w200), 4) if len(w200) == 200 else None})
    return out


def build_enrichment(snap, spy, assets, futs, cboe_cur):
    sma = spy.get("sma", {})
    session = spy.get("session") or snap.get("requested_completed_session")
    spy_detail = {
        "close": spy.get("close"), "regular_close": spy.get("regular_close", spy.get("close")),
        "live": bool(spy.get("live")), "quote_session": spy.get("quote_session"), "current_price": cboe_cur, "open": spy.get("open"), "high": spy.get("high"),
        "low": spy.get("low"), "previous_close": spy.get("previous_close"), "change_dollars": spy.get("change_dollars"),
        "d1_pct": rnd(safe_get(spy, "returns_pct", "1")), "d5_pct": rnd(safe_get(spy, "returns_pct", "5")),
        "d20_pct": rnd(safe_get(spy, "returns_pct", "20")), "volume": spy.get("volume"),
        "vol_avg_20d": spy.get("volume_average_prior_20"), "rvol_20d": rnd(spy.get("volume_vs_prior_20")),
        "sma20": safe_get(sma, "20", "value"), "sma50": safe_get(sma, "50", "value"), "sma200": safe_get(sma, "200", "value"),
        "sma200_dist_dollars": rnd(safe_get(sma, "200", "distance_dollars")),
        "sma200_dist_pct": rnd(safe_get(sma, "200", "distance_pct")),
        "quote_note": safe_get(spy, "cboe_quote", "note"),
    }
    vol_table = []
    for sym, label in VOL_SYMS:
        a = assets.get(sym, {})
        vol_table.append({"symbol": sym, "label": label, "close": a.get("close"),
                          "d1_pct": rnd(safe_get(a, "returns_pct", "1")), "sma20": safe_get(a, "sma", "20", "value")})
    assets_table = []
    for sym in ASSET_SYMS:
        a = spy if sym == "SPY" else assets.get(sym, {})
        if not a:
            continue
        assets_table.append({
            "symbol": sym, "close": a.get("close"),
            "d1_pct": rnd(safe_get(a, "returns_pct", "1")), "d5_pct": rnd(safe_get(a, "returns_pct", "5")),
            "d20_pct": rnd(safe_get(a, "returns_pct", "20")),
            "vs_spy_1d_pp": 0.0 if sym == "SPY" else rnd(safe_get(a, "relative_to_spy_pp", "1")),
            "rvol_20d": rnd(a.get("volume_vs_prior_20")) if a.get("volume") else None,
            "sma200": safe_get(a, "sma", "200", "value"), "sma200_dist_pct": rnd(safe_get(a, "sma", "200", "distance_pct")),
        })
    fut_table = []
    for sym in FUT_SYMS:
        f = futs.get(sym, {})
        if not f:
            continue
        on = f.get("overnight", {}) or {}
        d = f.get("daily", {}) or {}
        fut_table.append({
            "symbol": sym, "quote": safe_get(f, "quote", "price"),
            "timestamp_ct": _ts_ct(safe_get(f, "quote", "timestamp")),
            "vs_ref_dollars": safe_get(f, "quote", "displayed_change"),
            "vs_ref_pct": safe_get(f, "quote", "displayed_change_pct"),
            "rth_close": d.get("close"),
            "overnight_vs_close_pct": rnd(on.get("change_vs_completed_daily_close_pct")),
            "overnight_range": [on.get("low"), on.get("high")],
            "prior_range": [d.get("low"), d.get("high")],
        })
    vt = safe_get(snap, "volatility", "term_structure", default={}) or {}
    return {
        "session_date": session,
        "spy_detail": spy_detail,
        "intraday_30m": build_intraday(session),
        "daily_15d": build_daily(session),
        "volatility_table": vol_table,
        "vol_term_structure": {k: vt.get(k) for k in ("vix_over_vix3m", "vix9d_minus_vix", "vix_minus_vix3m", "regime", "note")},
        "assets_table": assets_table,
        "futures_table": fut_table,
        "gex_meta": {k: snap["options_gex"].get(k) for k in ("chain_timestamp", "gex_spot", "next_session_expiry", "monthly_opex_expiry", "units", "dealer_assumption", "formula")},
    }


def write_index(ts_ct):
    root = os.path.join(REPO_DIR, "snapshots")
    items = []
    for dp, _, fns in os.walk(root):
        for fn in fns:
            if not fn.endswith(".json") or fn == "index.json":
                continue
            p = os.path.join(dp, fn)
            try:
                j = json.load(open(p))
            except Exception:
                continue
            rel = os.path.relpath(p, REPO_DIR)
            sm = j.get("summary", {})
            d = datetime.fromisoformat(j["timestamp_utc"]).astimezone(CT)
            items.append({
                "id": rel[len("snapshots/"):-5].replace("/", "_"),
                "timestamp_utc": j["timestamp_utc"],
                "timestamp_ct": j.get("timestamp_ct"),
                "display_ct": d.strftime("%b %-d, %Y · %-I:%M %p CT"),
                "run_tag": j.get("run_tag"),
                "json_path": rel,
                "md_path": rel[:-5] + ".md",
                "spy_price": sm.get("spy_price"),
                "spy_1d_pct": sm.get("spy_1d_pct"),
                "net_gex_45d": sm.get("net_gex_45d_dollars"),
                "vix": sm.get("vix"),
            })
    items.sort(key=lambda r: r["timestamp_utc"], reverse=True)
    with open(os.path.join(root, "index.json"), "w") as f:
        json.dump({"updated_at_ct": ts_ct, "count": len(items), "snapshots": items}, f, indent=2)



# ----------------------------------------------------------------------------------------------
# Daily Gamma Tracker (Nov 12 2026): history/daily_tracker.jsonl + .csv, upserted by date.
# history/ is gitignored and NEVER staged; it reaches the dashboard only via the encrypted vault.
# The pre-open run writes the row; the post-close run finalizes close + data-only notes.
TRACKER_COLUMNS = ["date", "close", "premarket_gap_pct", "net_gex_45d", "prev_net_gex_45d", "flip_45d", "hvl_45d",
                   "call_wall_45d", "put_wall_45d", "pc_vol", "pc_oi", "poc", "vah", "val", "vix",
                   "futures_bias_class", "regime", "notes"]
_TRACKER_ROUND = {"close": 2, "premarket_gap_pct": 3, "net_gex_45d": 0, "prev_net_gex_45d": 0, "flip_45d": 2, "hvl_45d": 2,
                  "call_wall_45d": 2, "put_wall_45d": 2, "pc_vol": 3, "pc_oi": 3, "poc": 2, "vah": 2, "val": 2, "vix": 2}


def _tracker_round(row):
    out = dict(row)
    for k, n in _TRACKER_ROUND.items():
        v = out.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = round(float(v), n) if n else round(float(v))
    return out


def tracker_day_notes(ts_rows, day):
    """Data-only notes: sign changes of 0DTE / 45D net GEX across that day's archived runs."""
    rows = sorted((r for r in ts_rows if str(r.get("timestamp_ct") or "")[:10] == day), key=lambda r: r.get("timestamp_utc") or "")
    notes = []
    for key, label in (("net_gex_0dte_dollars", "0DTE"), ("net_gex_45d_dollars", "45D")):
        prev = None
        for r in rows:
            v = r.get(key)
            if not isinstance(v, (int, float)) or v == 0:
                continue
            if prev is not None and (v > 0) != (prev > 0):
                notes.append(f"{label} flipped {'long' if v > 0 else 'short'} {str(r.get('timestamp_ct'))[11:16]} CT")
            prev = v
    return "; ".join(notes) if notes else ("no net GEX sign change across archived runs" if rows else None)


def _atomic_write(path, text):
    tmp_dir = os.path.join(REPO_DIR, ".git") if os.path.isdir(os.path.join(REPO_DIR, ".git")) else os.path.dirname(path)
    tmp = os.path.join(tmp_dir, ".tmp_" + os.path.basename(path) + f".{os.getpid()}")
    with open(tmp, "w", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def load_daily_tracker(hist_dir):
    rows = {}
    p = os.path.join(hist_dir, "daily_tracker.jsonl")
    if os.path.exists(p):
        with open(p) as fh:
            lines = fh.read().splitlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("date"):
                rows[r["date"]] = r
    return rows


def write_daily_tracker(hist_dir, rows):
    import io
    os.makedirs(hist_dir, exist_ok=True)
    ordered = [rows[k] for k in sorted(rows)]
    _atomic_write(os.path.join(hist_dir, "daily_tracker.jsonl"), "".join(json.dumps(r) + "\n" for r in ordered))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=TRACKER_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in ordered:
        w.writerow({k: r.get(k) for k in TRACKER_COLUMNS})
    _atomic_write(os.path.join(hist_dir, "daily_tracker.csv"), buf.getvalue())


def upsert_daily_tracker(snap, run_tag, ts_rows, hist_dir, stamp_ct):
    """Returns the upserted row (or None when this run does not touch the tracker)."""
    g = snap.get("gamma_tracker") or {}
    new = g.get("tracker_row") if isinstance(g, dict) else None
    day = (new or {}).get("date") or g.get("tracker_date")
    if not day or run_tag not in ("pre-open", "post-close"):
        return None
    if g.get("is_nyse_session") is False:
        return None
    rows = load_daily_tracker(hist_dir)
    cur = dict(rows.get(day) or {c: None for c in TRACKER_COLUMNS})
    cur["date"] = day
    if run_tag == "pre-open":
        if not new:
            return None
        for k in TRACKER_COLUMNS:
            if k not in ("date", "close", "notes"):
                cur[k] = new.get(k)
        cur["pre_open_written_ct"] = stamp_ct
        # Confirm the prior session's close with the completed (final) close now that it is settled.
        spy = snap.get("spy") or {}
        ps = spy.get("completed_session") or spy.get("session")
        pc = spy.get("regular_close", spy.get("close"))
        if ps and ps != day and ps in rows and isinstance(pc, (int, float)):
            rows[ps] = _tracker_round(dict(rows[ps], close=pc, close_confirmed_ct=stamp_ct))
    else:
        spy = snap.get("spy") or {}
        if spy.get("session") == day and spy.get("status") == "ok":
            cur["close"] = spy.get("regular_close", spy.get("close"))
        cur["notes"] = tracker_day_notes(ts_rows, day)
        cur["finalized_ct"] = stamp_ct
    rows[day] = _tracker_round(cur)
    write_daily_tracker(hist_dir, rows)
    return rows[day]


CHARTS_PYTHON = "/workspace/gamma_charts_venv/bin/python"  # matplotlib lives here, not in the monitor venv


def run_morning_charts(day, run_tag):
    """Fail-safe hook: render the two morning charts (pre-open, or later if today's are missing).
    Any failure only prints; the archive/vault/publish flow continues unchanged."""
    charts_py = os.path.join(REPO_DIR, "scripts", "gamma_charts.py")
    if not os.path.exists(charts_py) or run_tag == "post-close":
        return None
    out_dir = os.path.join(REPO_DIR, "pod", "gamma_charts")
    have = all(os.path.exists(os.path.join(out_dir, f"{day}_{k}.png")) for k in ("outlook", "range"))
    if run_tag != "pre-open" and have:
        return None
    try:
        py = CHARTS_PYTHON if os.path.exists(CHARTS_PYTHON) else sys.executable
        r = subprocess.run([py, charts_py, "--date", day], cwd=REPO_DIR, capture_output=True, text=True, timeout=180)
        print("gamma charts rc", r.returncode, (r.stdout or r.stderr or "").strip()[-300:])
        return r.returncode
    except Exception as e:
        print("gamma charts skipped:", e)
        return None


def _main_unlocked():
    # Pull CEO decisions committed from the web app before writing anything (avoids push conflicts)
    try:
        if subprocess.run(["gh", "auth", "status"], capture_output=True, text=True).returncode == 0:
            r = subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], cwd=REPO_DIR, capture_output=True, text=True, timeout=120)
            print("git pull:", (r.stdout or r.stderr).strip()[:200])
    except Exception as e:
        print("git pull skipped:", e)
    ensure_repo()

    # Clean legacy unfinished June 2026 gamma files if still present (leaving wash_sale_tracker untouched)
    legacy_files = [
        ".github/workflows/daily_gamma.yml",
        "workflows/daily_gamma.yml",
        "gamma_gex.py",
        "spy_gamma_chart_2026-06-25.png",
        "spy_historical.csv",
    ]
    for rel in legacy_files:
        p = os.path.join(REPO_DIR, rel)
        if os.path.exists(p):
            subprocess.run(["git", "rm", "-f", rel], cwd=REPO_DIR, check=True)

    with open(SNAPSHOT_JSON) as f:
        snap = json.load(f)

    gen_utc_str = snap["generated_at_utc"]
    dt_utc = datetime.fromisoformat(gen_utc_str)
    dt_ct = dt_utc.astimezone(CT)
    ts_ct = dt_ct.strftime("%Y-%m-%dT%H:%M:%S%z")
    date_str = dt_ct.strftime("%Y-%m-%d")
    yyyy = dt_ct.strftime("%Y")
    mm = dt_ct.strftime("%m")
    hhmm = dt_ct.strftime("%H%M")

    hr = dt_ct.hour
    if hr < 8 or (hr == 8 and dt_ct.minute < 30):
        run_tag = "pre-open"
    elif hr >= 15:
        run_tag = "post-close"
    else:
        run_tag = "rth-hourly"

    os.makedirs(os.path.join(REPO_DIR, "history"), exist_ok=True)
    os.makedirs(os.path.join(REPO_DIR, "scripts"), exist_ok=True)
    snap_dir = os.path.join(REPO_DIR, "snapshots", yyyy, mm, date_str)
    os.makedirs(snap_dir, exist_ok=True)

    for src, dst_name in [
        ("/workspace/spy_gamma_monitor.py", "spy_gamma_monitor.py"),
        ("/workspace/sync_github_archive.py", "sync_github_archive.py"),
    ]:
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(REPO_DIR, "scripts", dst_name))

    spy = snap["spy"]
    og = snap["options_gex"]
    scopes = og["scopes"]
    s_next = scopes["next_session"]
    s_45d = scopes["upcoming_45d"]
    s_opex = scopes["monthly_opex"]
    s_all = scopes["upcoming_all"]
    assets = snap.get("assets", {})
    futs = snap.get("futures", {})

    def top_wall(arr):
        return arr[0]["strike"] if arr else None

    # Live intraday overlay (spy_gamma_monitor sets live=True + regular_close during RTH/pre-market)
    spy_live = spy.get("close") if spy.get("live") else None
    spy_close = spy.get("regular_close", spy.get("close"))  # prior COMPLETED regular-session close
    cboe_cur = spy_live or safe_get(spy, "cboe_quote", "data", "current_price") or og.get("gex_spot") or spy_close
    sma200 = safe_get(spy, "sma", "200", "value")
    sma200_d = safe_get(spy, "sma", "200", "distance_dollars")
    sma200_p = safe_get(spy, "sma", "200", "distance_pct")

    flat_row = {
        "timestamp_ct": ts_ct,
        "timestamp_utc": gen_utc_str,
        "session_date": snap.get("requested_completed_session", date_str),
        "run_tag": run_tag,
        "spy_price": cboe_cur,
        "spy_regular_close": spy_close,
        "spy_1d_pct": rnd(safe_get(spy, "returns_pct", "1"), 4),
        "spy_volume": spy.get("volume"),
        "spy_rel_vol_20d": rnd(spy.get("volume_vs_prior_20")),
        "spy_sma_200": sma200,
        "spy_sma_200_dist_dollars": round(sma200_d, 4) if sma200_d is not None else None,
        "spy_sma_200_dist_pct": round(sma200_p, 4) if sma200_p is not None else None,
        "spy_sma_20": safe_get(spy, "sma", "20", "value"),
        "spy_sma_50": safe_get(spy, "sma", "50", "value"),
        "gex_spot_ref": og.get("gex_spot"),
        "net_gex_0dte_dollars": round(s_next["totals"]["net_gex"], 2),
        "net_gex_45d_dollars": round(s_45d["totals"]["net_gex"], 2),
        "net_gex_opex_dollars": round(s_opex["totals"]["net_gex"], 2),
        "net_gex_all_dollars": round(s_all["totals"]["net_gex"], 2),
        "call_wall_0dte": top_wall(s_next.get("call_walls_by_gex", [])),
        "put_wall_0dte": top_wall(s_next.get("put_walls_by_gex", [])),
        "call_wall_45d": top_wall(s_45d.get("call_walls_by_gex", [])),
        "put_wall_45d": top_wall(s_45d.get("put_walls_by_gex", [])),
        "zero_gamma_0dte": safe_get(s_next, "zero_gamma_repriced_model", "zero_gamma_level"),
        "zero_gamma_45d": safe_get(s_45d, "zero_gamma_repriced_model", "zero_gamma_level"),
        "node_735_0dte_net_gex": rnd(safe_get(s_next, "node_735", "net_gex"), 2),
        "node_735_0dte_call_oi": safe_get(s_next, "node_735", "call_oi"),
        "node_735_0dte_put_oi": safe_get(s_next, "node_735", "put_oi"),
        "node_735_0dte_call_vol": safe_get(s_next, "node_735", "call_volume"),
        "node_735_0dte_put_vol": safe_get(s_next, "node_735", "put_volume"),
        "node_735_45d_net_gex": rnd(safe_get(s_45d, "node_735", "net_gex"), 2),
        "vix": safe_get(assets, "^VIX", "close"),
        "vix_1d_pct": rnd(safe_get(assets, "^VIX", "returns_pct", "1"), 4),
        "vix9d": safe_get(assets, "^VIX9D", "close"),
        "vix3m": safe_get(assets, "^VIX3M", "close"),
        "vvix": safe_get(assets, "^VVIX", "close"),
        "nvda_1d_pct": rnd(safe_get(assets, "NVDA", "returns_pct", "1"), 4),
        "sox_1d_pct": rnd(safe_get(assets, "^SOX", "returns_pct", "1"), 4),
        "smh_1d_pct": rnd(safe_get(assets, "SMH", "returns_pct", "1"), 4),
        "bkx_1d_pct": rnd(safe_get(assets, "^BKX", "returns_pct", "1"), 4),
        "kbwb_1d_pct": rnd(safe_get(assets, "KBWB", "returns_pct", "1"), 4),
        "live_quote_session": snap.get("live_quote_session"),
        "es_quote": safe_get(futs, "ES=F", "quote", "price"),
        "nq_quote": safe_get(futs, "NQ=F", "quote", "price"),
    }

    compact_scopes = {}
    for sk, sv in scopes.items():
        compact_scopes[sk] = {
            "expirations": sv.get("expirations"),
            "totals": sv.get("totals"),
            "call_walls_by_gex": sv.get("call_walls_by_gex", [])[:5],
            "put_walls_by_gex": sv.get("put_walls_by_gex", [])[:5],
            "node_735": sv.get("node_735"),
            "zero_gamma_repriced_model": sv.get("zero_gamma_repriced_model"),
            "strikes_700_810": [
                {
                    "strike": r["strike"],
                    "net_gex": round(r["net_gex"], 2),
                    "call_gex": round(r["call_gex"], 2),
                    "put_gex": round(r["put_gex"], 2),
                    "call_oi": r["call_oi"],
                    "put_oi": r["put_oi"],
                    "call_vol": r["call_volume"],
                    "put_vol": r["put_volume"],
                }
                for r in sv.get("strike_profile", [])
                if 700 <= r["strike"] <= 810
            ],
        }

    compact_json = {
        "schema_version": "1.0",
        "timestamp_ct": ts_ct,
        "timestamp_utc": gen_utc_str,
        "run_tag": run_tag,
        "summary": flat_row,
        "gex_scopes": compact_scopes,
        "verified_headlines": snap.get("verified_headlines", []),
        "gamma_tracker": snap.get("gamma_tracker"),
        "intraday_feed": snap.get("intraday_feed"),  # RTH watcher reads 5m/tod RVOL, GEX-by-spot, ES overnight in SPY terms
    }
    compact_json.update(build_enrichment(snap, spy, assets, futs, cboe_cur))
    compact_json["md_rel_path"] = f"snapshots/{yyyy}/{mm}/{date_str}/{hhmm}-ct-{run_tag}.md"
    compact_json["json_rel_path"] = f"snapshots/{yyyy}/{mm}/{date_str}/{hhmm}-ct-{run_tag}.json"

    # Prefer monitor --summary output; fall back to legacy report path; if both exist use the newer mtime.
    if os.path.exists(SUMMARY_MD) and os.path.exists(REPORT_MD):
        md_source = SUMMARY_MD if os.path.getmtime(SUMMARY_MD) >= os.path.getmtime(REPORT_MD) else REPORT_MD
    elif os.path.exists(SUMMARY_MD):
        md_source = SUMMARY_MD
    else:
        md_source = REPORT_MD
    base_slug = f"{hhmm}-ct-{run_tag}"
    md_dest = os.path.join(snap_dir, f"{base_slug}.md")
    json_dest = os.path.join(snap_dir, f"{base_slug}.json")

    shutil.copyfile(md_source, md_dest)
    shutil.copyfile(md_source, os.path.join(REPO_DIR, "latest.md"))

    with open(json_dest, "w") as f:
        json.dump(compact_json, f, indent=2)
    with open(os.path.join(REPO_DIR, "latest.json"), "w") as f:
        json.dump(compact_json, f, indent=2)

    jsonl_path = os.path.join(REPO_DIR, "history", "timeseries.jsonl")
    existing = []
    if os.path.exists(jsonl_path):
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    obj = json.loads(line)
                    if obj.get("timestamp_utc") != gen_utc_str:
                        existing.append(obj)
    existing.append(flat_row)
    existing.sort(key=lambda r: r["timestamp_utc"])

    with open(jsonl_path, "w") as f:
        for r in existing:
            f.write(json.dumps(r) + "\n")

    csv_path = os.path.join(REPO_DIR, "history", "timeseries.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in existing:
            w.writerow({k: r.get(k) for k in CSV_COLUMNS})

    # Daily Gamma Tracker upsert + morning charts (both fail-safe; local/gitignored outputs only)
    try:
        trow = upsert_daily_tracker(snap, run_tag, existing, os.path.join(REPO_DIR, "history"), ts_ct)
        print("daily tracker:", "upserted " + trow["date"] if trow else "unchanged")
    except Exception as e:
        print("daily tracker skipped:", e)
    run_morning_charts(safe_get(snap, "gamma_tracker", "tracker_date") or date_str, run_tag)

    with open(os.path.join(REPO_DIR, "README.md"), "w") as f:
        f.write(README_CONTENT)

    write_index(ts_ct)

    # Refresh CEO Briefings/memos index + bridge specialist ledger (never fatal)
    pod_engine = os.path.join(REPO_DIR, "scripts", "pod_engine.py")
    if os.path.exists(pod_engine):
        for sub in ("bridge-ledger", "build-memos-index"):
            try:
                subprocess.run([sys.executable, pod_engine, sub], cwd=REPO_DIR, check=False, timeout=60)
            except Exception as e:
                print("pod_engine", sub, "failed:", e)
    # Vault mode: rebuild encrypted state when password/meta present (plaintext stays local)
    vault_py = os.path.join(REPO_DIR, "scripts", "vault_crypto.py")
    vault_meta = os.path.join(REPO_DIR, "vault", "vault_meta.json")
    env_vault = os.path.join(REPO_DIR, ".env.vault")
    vault_active = os.path.exists(vault_py) and (
        os.environ.get("HORSEY_VAULT_PASSWORD") or os.path.exists(env_vault) or os.path.exists(vault_meta)
    )
    if vault_active:
        try:
            subprocess.run([sys.executable, vault_py, "build-state"], cwd=REPO_DIR, check=False, timeout=180)
            print("vault state rebuilt")
        except Exception as e:
            print("vault build-state failed:", e)
    # Explicit allowlist (Nov 10 2026): stage ONLY what this script publishes. No `git add -A`, no -f.
    ok_to_publish, staged_now = stage_allowlist(REPO_DIR)
    if not ok_to_publish:
        print("NO_COMMIT_NO_PUSH: allowlist refusal (see REFUSED above)")
    elif staged_now:
        msg = f"SPY gamma snapshot {dt_ct.strftime('%Y-%m-%d %H:%M CT')} ({run_tag}: SPY ${(flat_row['spy_price'] or spy_close or 0):.2f}, 45D GEX {s_45d['totals']['net_gex']/1e9:.2f}B, VIX {flat_row['vix']})"
        subprocess.run(["git", "commit", "-m", msg], cwd=REPO_DIR, check=True)
        print("COMMITTED:", msg, "| paths:", len(staged_now))
    else:
        print("NO_CHANGES")

    # Persist scripts + git repo into durable agent store
    os.makedirs(STORE_ROOT, exist_ok=True)
    for fn in ["spy_gamma_monitor.py", "sync_github_archive.py", "spy_gamma_snapshot.json", "spy_gamma_report.md"]:
        sp = os.path.join("/workspace", fn)
        if os.path.exists(sp):
            shutil.copyfile(sp, os.path.join(STORE_ROOT, fn))
    tar_backup = os.path.join(STORE_ROOT, "horsey_repo_backup.tar.gz")
    subprocess.run(["tar", "-czf", tar_backup, "-C", "/workspace", "horsey"], check=True)

    # Push via gh/git if gh CLI is authenticated
    gh_check = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if not ok_to_publish:
        print("PUSH_SKIPPED: allowlist refusal")
    elif gh_check.returncode == 0:
        subprocess.run(["gh", "auth", "setup-git"], check=False)
        push = subprocess.run(["git", "push", "-u", "origin", "main"], cwd=REPO_DIR, capture_output=True, text=True)
        print("PUSH_RC:", push.returncode, push.stdout.strip(), push.stderr.strip())
    else:
        print("GH_AUTH_PENDING: committed locally & mirrored to persistent store; will push automatically once gh is authenticated.")



# ----------------------------------------------------------------------------------------------
# Publish allowlist: exactly the tracked outputs this script writes. Derived from its own
# commit history plus the code: encrypted vault artifacts, the gate index.html, and the two
# script mirrors copied in from /workspace. Plaintext (pod/, snapshots/, history/, docs/,
# latest.*, README.md) is gitignored and never staged.
PUBLISH_ALLOWLIST_FILES = (
    "index.html",
    "vault/state.enc.json",
    "vault/manifest.json",
    "vault/app.enc.json",
    "vault/vault_meta.json",
    "scripts/spy_gamma_monitor.py",
    "scripts/sync_github_archive.py",
)
PUBLISH_ALLOWLIST_DIRS = ("vault/snapshots/",)


def in_publish_allowlist(path):
    return path in PUBLISH_ALLOWLIST_FILES or any(path.startswith(d) for d in PUBLISH_ALLOWLIST_DIRS)


def stage_allowlist(repo_dir):
    """Stage allowlisted paths only (`git add -- <paths>`, never -A, never -f), then verify the
    whole index. If anything outside the allowlist is staged, unstage everything, print
    REFUSED and return (False, staged). Returns (True, staged_allowlisted_paths) otherwise."""
    tracked = set((subprocess.run(["git", "ls-files", "--", *PUBLISH_ALLOWLIST_FILES, *PUBLISH_ALLOWLIST_DIRS],
                                  cwd=repo_dir, capture_output=True, text=True).stdout or "").split())
    paths = [p for p in PUBLISH_ALLOWLIST_FILES if os.path.exists(os.path.join(repo_dir, p)) or p in tracked]
    paths += [d.rstrip("/") for d in PUBLISH_ALLOWLIST_DIRS
              if os.path.isdir(os.path.join(repo_dir, d)) or any(t.startswith(d) for t in tracked)]
    if paths:
        r = subprocess.run(["git", "add", "--", *paths], cwd=repo_dir, capture_output=True, text=True)
        if r.returncode != 0:
            print("WARNING: git add (allowlist) rc", r.returncode, (r.stderr or "").strip()[:200])
    staged = [l for l in (subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo_dir,
                                         capture_output=True, text=True).stdout or "").splitlines() if l.strip()]
    outside = [p for p in staged if not in_publish_allowlist(p)]
    if outside:
        subprocess.run(["git", "reset", "-q", "--", *staged], cwd=repo_dir, check=False)
        print("REFUSED: paths outside publish allowlist were staged; unstaged all, skipping commit+push:", outside[:20])
        return False, staged
    return True, staged


def main():
    # Whole pull → write → vault build → commit → push sequence is serialized with the
    # Alpaca RTH tick and manual deploys via /tmp/horsey_repo_sync.lock
    with repo_sync_lock(label="sync_github_archive", timeout_sec=600):
        return _main_unlocked()

if __name__ == "__main__":
    main()

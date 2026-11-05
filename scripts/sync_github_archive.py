#!/usr/bin/env python3
"""Build structured historical archive files from /workspace/spy_gamma_snapshot.json,
commit inside /workspace/horsey (mirroring to persistent store), and push via git/gh when authenticated.
"""
import csv
import json
import os
import shutil
import subprocess
from datetime import datetime
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

## Repository Layout

- `latest.md` / `latest.json` — Most recent snapshot in MCP-ready Markdown and structured JSON.
- `history/timeseries.jsonl` / `history/timeseries.csv` — Append-only hourly time-series log (one record per run) for historical recall, charting, and MCP ingestion.
- `snapshots/YYYY/MM/YYYY-MM-DD/<HHMM>-ct-<tag>.md` & `.json` — Immutable point-in-time hourly and post-close reports with full `$700–$810` strike-by-strike GEX profiles.
- `scripts/spy_gamma_monitor.py` & `scripts/sync_github_archive.py` — Data pipeline and GitHub archive builder.
- `wash_sale_tracker_v2.py` & `workflows/wash_sale_tracker.yml` — Wash-sale tracking utility (preserved).
"""


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


def main():
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

    spy_close = spy.get("close")
    cboe_cur = safe_get(spy, "cboe_quote", "current_price") or og.get("gex_spot") or spy_close
    sma200 = safe_get(spy, "sma", "200", "value")
    sma200_d = safe_get(spy, "sma", "200", "close_distance_dollars")
    sma200_p = safe_get(spy, "sma", "200", "close_distance_pct")

    flat_row = {
        "timestamp_ct": ts_ct,
        "timestamp_utc": gen_utc_str,
        "session_date": snap.get("requested_completed_session", date_str),
        "run_tag": run_tag,
        "spy_price": cboe_cur,
        "spy_regular_close": spy_close,
        "spy_1d_pct": round(safe_get(spy, "returns_pct", "1d", default=0.0), 4),
        "spy_volume": spy.get("volume"),
        "spy_rel_vol_20d": round(safe_get(spy, "volume_context", "relative_to_prior_20_mean", default=0.0), 4),
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
        "zero_gamma_0dte": safe_get(s_next, "zero_gamma_repriced_model", "zero_gamma_spot"),
        "zero_gamma_45d": safe_get(s_45d, "zero_gamma_repriced_model", "zero_gamma_spot"),
        "node_735_0dte_net_gex": round(safe_get(s_next, "node_735", "net_gex", default=0.0), 2),
        "node_735_0dte_call_oi": safe_get(s_next, "node_735", "call_oi"),
        "node_735_0dte_put_oi": safe_get(s_next, "node_735", "put_oi"),
        "node_735_0dte_call_vol": safe_get(s_next, "node_735", "call_volume"),
        "node_735_0dte_put_vol": safe_get(s_next, "node_735", "put_volume"),
        "node_735_45d_net_gex": round(safe_get(s_45d, "node_735", "net_gex", default=0.0), 2),
        "vix": safe_get(assets, "^VIX", "close"),
        "vix_1d_pct": round(safe_get(assets, "^VIX", "returns_pct", "1d", default=0.0), 4),
        "vix9d": safe_get(assets, "^VIX9D", "close"),
        "vix3m": safe_get(assets, "^VIX3M", "close"),
        "vvix": safe_get(assets, "^VVIX", "close"),
        "nvda_1d_pct": round(safe_get(assets, "NVDA", "returns_pct", "1d", default=0.0), 4),
        "sox_1d_pct": round(safe_get(assets, "^SOX", "returns_pct", "1d", default=0.0), 4),
        "smh_1d_pct": round(safe_get(assets, "SMH", "returns_pct", "1d", default=0.0), 4),
        "bkx_1d_pct": round(safe_get(assets, "^BKX", "returns_pct", "1d", default=0.0), 4),
        "kbwb_1d_pct": round(safe_get(assets, "KBWB", "returns_pct", "1d", default=0.0), 4),
        "es_quote": safe_get(futs, "ES=F", "new_globex_session", "quote") or safe_get(futs, "ES=F", "daily_close"),
        "nq_quote": safe_get(futs, "NQ=F", "new_globex_session", "quote") or safe_get(futs, "NQ=F", "daily_close"),
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
    }

    md_source = REPORT_MD if os.path.exists(REPORT_MD) else SUMMARY_MD
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

    with open(os.path.join(REPO_DIR, "README.md"), "w") as f:
        f.write(README_CONTENT)

    subprocess.run(["git", "add", "-A"], cwd=REPO_DIR, check=True)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_DIR, capture_output=True, text=True, check=True)
    if status.stdout.strip():
        msg = f"SPY gamma snapshot {dt_ct.strftime('%Y-%m-%d %H:%M CT')} ({run_tag}: SPY ${spy_close:.2f}, 45D GEX {s_45d['totals']['net_gex']/1e9:.2f}B, VIX {flat_row['vix']})"
        subprocess.run(["git", "commit", "-m", msg], cwd=REPO_DIR, check=True)
        print("COMMITTED:", msg)
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
    if gh_check.returncode == 0:
        subprocess.run(["gh", "auth", "setup-git"], check=False)
        push = subprocess.run(["git", "push", "-u", "origin", "main"], cwd=REPO_DIR, capture_output=True, text=True)
        print("PUSH_RC:", push.returncode, push.stdout.strip(), push.stderr.strip())
    else:
        print("GH_AUTH_PENDING: committed locally & mirrored to persistent store; will push automatically once gh is authenticated.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Playwright: Daily Gamma Tracker table + morning charts render AFTER vault unlock, with no
snake_case, path, UUID, OCC or CLI leaks in the card's visible text, and 0 console errors.

Local mode (default): builds a throwaway encrypted site in a temp dir from SYNTHETIC tracker rows,
a synthetic gamma_tracker block and fixture chart images (rendered by gamma_charts.py from fixture
data), with a throwaway password. The live repo, its vault and its password are never touched.
Live mode: --live https://brahaymin.github.io/horsey/  (password read via vault_crypto.load_password,
never printed).
Run: /tmp/pwvenv/bin/python scripts/test_dashboard_gamma_tracker_pw.py [--live URL]
"""
import http.server, json, os, re, shutil, socket, subprocess, sys, tempfile, threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
VENV_PY = "/workspace/spy_gamma_venv/bin/python"
CHARTS_PY = "/workspace/gamma_charts_venv/bin/python"
SNAKE_RE = re.compile(r"\b[a-z]+_[a-z0-9_]+\b")
PATH_RE = re.compile(r"(/workspace|/home/|/tmp/|\bpod/|\bhistory/|\.jsonl?\b|\.png\b|\.csv\b)")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
OCC_RE = re.compile(r"\b[A-Z]{1,6}\d{6}[CP]\d{8}\b")
CLI_RE = re.compile(r"(python3?\s|--[a-z]{3,}|\bgit\s|scripts/)")
COLS = ["date", "close", "premarket_gap_pct", "net_gex_45d", "prev_net_gex_45d", "flip_45d", "hvl_45d", "call_wall_45d", "put_wall_45d",
        "pc_vol", "pc_oi", "poc", "vah", "val", "vix", "futures_bias_class", "regime", "notes"]
PW = "synthetic-test-only-pass"


def synth_rows():
    out = []
    for i, d in enumerate(["2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"]):
        r = dict(zip(COLS, [d, 750 + i, -0.5 + i * 0.1, -2e9 + i * 3e8, -2.1e9 + i * 3e8, 755 + i, None if i % 2 else 757 + i, 770, 745, 0.9, 1.8,
                             752 + i, 754 + i, 750 + i, 18.2, ["flat_or_green", "dipped_recovering", "dipped_not_recovering"][i % 3],
                             ["short", "neutral", "long"][i % 3], "0DTE flipped long 10:21 CT" if i % 2 else None]))
        r["pre_open_written_ct"] = "2026-11-12T08:23:00-0600"
        out.append(r)
    return out


def synth_gamma_tracker():
    return {"tracker_date": "2026-11-12", "trading_days_left_ytd": 34,
            "gamma_split": {"0dte": {"dominant_side": "call", "call_share": 0.55, "put_share": 0.45}, "45d": {"dominant_side": "put", "call_share": 0.49, "put_share": 0.51}},
            "pc_ratios": {"45d": {"put_call_volume": 0.88, "put_call_oi": 2.13}}, "hvl_model": {"0dte": {"level": 764.62}, "45d": {"level": None}},
            "zero_gamma_repriced": {"45d": 761.32}, "volume_profile": {"poc": 759.25, "vah": 761.5, "val": 758.75},
            "overnight_futures": {"ES=F": {"classification": "dipped_not_recovering", "ref": 7643.0, "gap_pct": -0.72, "low": 7572.0,
                                           "low_time_ct": "2026-11-12T07:17:00-06:00", "pct_retraced": 0.23}},
            "tracker_row": {"date": "2026-11-12"}}


def build_local(tmp: Path):
    """Temp HORSEY_ROOT with plaintext sources, then encrypt it with vault_crypto (throwaway password)."""
    src = tmp / "src"; src.mkdir()
    shutil.copy2(ROOT / "app_unlocked.html", src / "app_unlocked.html")
    shutil.copy2(ROOT / "vault_gate.html", src / "vault_gate.html")
    for name in ("js", "snapshots"):
        if (ROOT / name).exists():
            shutil.copytree(ROOT / name, src / name, symlinks=False)
    (src / "history").mkdir()
    for fn in ("timeseries.jsonl", "timeseries.csv"):
        if (ROOT / "history" / fn).exists():
            shutil.copy2(ROOT / "history" / fn, src / "history" / fn)
    (src / "history" / "daily_tracker.jsonl").write_text("".join(json.dumps(r) + "\n" for r in synth_rows()))
    latest = json.loads((ROOT / "latest.json").read_text()); latest["gamma_tracker"] = synth_gamma_tracker()
    (src / "latest.json").write_text(json.dumps(latest))
    if (ROOT / "latest.md").exists():
        shutil.copy2(ROOT / "latest.md", src / "latest.md")
    pod = src / "pod"
    for d in ("ledger", "risk", "signals", "reviews", "track_record", "ceo_decisions"):
        (pod / d).mkdir(parents=True, exist_ok=True)
    minimal = {"roster.json": {"agents": []}, "scorecard.json": {"agents": {}}, "memos_index.json": {"memos": []},
               "ceo_questions.json": {"questions": []}, "ceo_decisions/latest_decisions.json": {},
               "signals/latest_pod_state.json": {"agents": {}, "cio_synthesis": {"summary": "Synthetic"}},
               "risk/limits.json": {"limits": {}, "kill_switch": {"breached": False}}, "risk/portfolio_snapshot.json": {},
               "reviews/reviews_index.json": {"reviews": []}, "track_record/weekly_summary.json": {}}
    for rel, obj in minimal.items():
        (pod / rel).write_text(json.dumps(obj))
    (pod / "ledger" / "paper_blotter.jsonl").write_text("")
    # fixture charts: rendered by the real chart script from fixture data
    fx, data = tmp / "fx", tmp / "fxdata"
    r = subprocess.run([VENV_PY, "-W", "ignore", "-c", "import sys;from pathlib import Path;import test_gamma_tracker as TG;TG.write_chart_fixture(Path(sys.argv[1]),Path(sys.argv[2]))",
                        str(fx), str(data)], cwd=str(HERE), capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise SystemExit("fixture failed: " + r.stderr[-400:])
    r = subprocess.run([CHARTS_PY, str(HERE / "gamma_charts.py"), "--date", "2026-11-12", "--root", str(fx), "--data-dir", str(data),
                        "--out-dir", str(pod / "gamma_charts")], capture_output=True, text=True, timeout=180)
    if r.returncode:
        raise SystemExit("chart fixture failed: " + r.stderr[-400:])
    env = dict(os.environ, HORSEY_ROOT=str(src), HORSEY_VAULT_PASSWORD=PW)
    for cmd in ("build",):
        r = subprocess.run([VENV_PY, str(HERE / "vault_crypto.py"), cmd], env=env, capture_output=True, text=True, timeout=600)
        if r.returncode:
            raise SystemExit("vault build failed: " + (r.stderr or r.stdout)[-400:])
    site = tmp / "site"; site.mkdir()
    shutil.copy2(src / "vault_gate.html", site / "index.html"); shutil.copytree(src / "vault", site / "vault")
    return site


def check(page, url, pw, expect_rows=None):
    errors = []
    page.on("console", lambda m: errors.append("console: " + m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
    page.goto(url, wait_until="networkidle")
    page.fill("#vaultPass", pw); page.press("#vaultPass", "Enter")
    page.wait_for_selector("#gtTbl tr td", timeout=60000)
    page.wait_for_function("document.querySelectorAll('#gtTbl tr').length>1 && document.querySelector('#gtTbl').innerText.indexOf('Loading')<0", timeout=60000)
    page.wait_for_selector("#gtCharts figure img", timeout=60000)
    page.wait_for_timeout(1500)
    card = page.inner_text("#gtCard")
    rows = page.eval_on_selector_all("#gtTbl tr", "rs=>rs.length") - 1
    imgs = page.eval_on_selector_all("#gtCharts img", "is=>is.map(i=>[i.complete,i.naturalWidth])")
    heads = page.inner_text("#gtTbl tr:first-child")
    checks = {
        "tracker table has rows": rows >= 1 and (expect_rows is None or rows == expect_rows),
        "headers plain English": all(h.upper() in heads.upper() for h in ("Date", "Pre-mkt gap", "Net GEX 45D", "Prior day GEX", "P/C vol", "P/C OI", "POC", "Futures", "Regime", "Notes")),
        "two charts decoded": len(imgs) == 2 and all(c and w > 300 for c, w in imgs),
        "chart titles shown": "Outlook – Gamma Regime Rotation" in card and "Daily Range Sketch" in card,
        "no snake_case": not SNAKE_RE.search(card), "no paths/file names": not PATH_RE.search(card),
        "no UUID": not UUID_RE.search(card), "no OCC": not OCC_RE.search(card), "no CLI": not CLI_RE.search(card),
        "0 console errors": not errors,
    }
    return checks, {"rows": rows, "imgs": imgs, "errors": errors[:5], "leaks": SNAKE_RE.findall(card)[:5] + PATH_RE.findall(card)[:5]}


def main():
    from playwright.sync_api import sync_playwright
    live = sys.argv[sys.argv.index("--live") + 1] if "--live" in sys.argv else None
    tmp = Path(tempfile.mkdtemp(prefix="pw_gtracker_"))
    try:
        if live:
            sys.path.insert(0, str(HERE))
            r = subprocess.run([VENV_PY, "-c", "import sys;sys.path.insert(0,'scripts');import vault_crypto as v;sys.stdout.write(v.load_password() or '')"],
                               cwd=str(ROOT), capture_output=True, text=True)
            pw, url, expect, httpd = r.stdout, live, None, None
            if not pw:
                raise SystemExit("no vault password configured")
        else:
            site = build_local(tmp)
            s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
            handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(site), **k)
            handler.log_message = lambda *a: None
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            pw, url, expect = PW, f"http://127.0.0.1:{port}/index.html#briefings", 10
        with sync_playwright() as p:
            b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 1100})
            checks, info = check(page, url, pw, expect)
            if "--shot" in sys.argv:
                page.locator("#gtCard").screenshot(path=sys.argv[sys.argv.index("--shot") + 1])
            b.close()
        if httpd:
            httpd.shutdown()
        print(json.dumps({"checks": checks, "info": info}, indent=1))
        ok = all(checks.values()); print("PLAYWRIGHT", "PASS" if ok else "FAIL", f"{sum(checks.values())}/{len(checks)}")
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

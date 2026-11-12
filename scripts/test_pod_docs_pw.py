#!/usr/bin/env python3
"""Playwright: the "Pod Docs" reader (Tab 6) renders every encrypted doc after vault unlock, with
plain-English titles, no snake_case / path / UUID / OCC / CLI leaks, no PII-pattern hits in the
visible text, 0 console errors, and the docs NOT reachable as plaintext on the site.

Local mode (default): throwaway encrypted site in a temp dir built from the local Pod Docs library
(docs/pod_docs, gitignored) with a throwaway password; the live vault is never touched.
Live mode: --live https://brahaymin.github.io/horsey/  (password via vault_crypto.load_password, never printed).
Run: /tmp/pwvenv/bin/python scripts/test_pod_docs_pw.py [--live URL]
"""
import http.server, json, os, re, shutil, socket, subprocess, sys, tempfile, threading, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
VENV_PY = "/workspace/spy_gamma_venv/bin/python"
SNAKE_RE = re.compile(r"\b[a-z]+_[a-z0-9_]+\b")
PATH_RE = re.compile(r"(/workspace|/home/|/tmp/|\bpod/|\bdocs/|\bhistory/|\bscripts/|\b[\w-]+\.(md|jsonl?|py|csv|txt|png|html)\b)")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
OCC_RE = re.compile(r"\b[A-Z]{1,6}\d{6}[CP]\d{8}\b")
CLI_RE = re.compile(r"(python3?\s|\s--[a-z]{3,}|\bgit\s(add|push|commit|pull)|\bsudo\b)")
PII_RE = re.compile(r"(•{2,}\s?\d|\*{3,}\d|robinhood|income sleeve|student[- ]debt|student loan|spouse|mortgage|@[a-z0-9-]+\.[a-z]{2,}|APCA_|ghp_|HORSEY_VAULT)", re.I)
TITLES = ["Joint Level-Selection Framework (v1.3)", "CEO Pre-Market Template", "Pod Architecture", "Pod Roster", "Daily Runbook", "Changelog"]
PW = "synthetic-test-only-pass"


def build_local(tmp: Path):
    src = tmp / "src"; src.mkdir()
    for fn in ("app_unlocked.html", "vault_gate.html"):
        shutil.copy2(ROOT / fn, src / fn)
    for name in ("js", "snapshots"):
        if (ROOT / name).exists():
            shutil.copytree(ROOT / name, src / name)
    (src / "history").mkdir()
    for fn in ("timeseries.jsonl", "timeseries.csv"):
        if (ROOT / "history" / fn).exists():
            shutil.copy2(ROOT / "history" / fn, src / "history" / fn)
    shutil.copytree(ROOT / "docs" / "pod_docs", src / "docs" / "pod_docs")
    shutil.copy2(ROOT / "latest.json", src / "latest.json")
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
    env = dict(os.environ, HORSEY_ROOT=str(src), HORSEY_VAULT_PASSWORD=PW, HORSEY_REPO_LOCK_HELD="1")
    r = subprocess.run([VENV_PY, str(HERE / "vault_crypto.py"), "build"], env=env, capture_output=True, text=True, timeout=600)
    if r.returncode:
        raise SystemExit("vault build failed: " + (r.stderr or r.stdout)[-400:])
    site = tmp / "site"; site.mkdir()
    shutil.copy2(src / "vault_gate.html", site / "index.html"); shutil.copytree(src / "vault", site / "vault")
    return site


def plaintext_reachable(base):
    hits = []
    for rel in ("docs/pod_docs/index.json", "docs/pod_docs/runbook.md", "docs/pod_docs/framework.md"):
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/" + rel, timeout=20) as r:
                if r.status == 200:
                    hits.append(rel)
        except (urllib.error.HTTPError, urllib.error.URLError):
            pass
    return hits


def check(page, url, pw):
    errors = []
    page.on("console", lambda m: errors.append("console: " + m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
    page.on("response", lambda r: errors.append(f"http {r.status}: " + r.url.split("/")[-1][:60]) if r.status >= 400 else None)
    page.goto(url, wait_until="networkidle")
    page.fill("#vaultPass", pw); page.press("#vaultPass", "Enter")
    page.wait_for_selector("#pdBtns button", timeout=60000)
    btns = page.eval_on_selector_all("#pdBtns button", "bs=>bs.map(b=>b.innerText.trim())")
    per, leaks = {}, []
    for i, t in enumerate(btns):
        page.click(f"#pdBtns button:nth-child({i + 1})")
        page.wait_for_function("(t)=>{const d=document.querySelector('#pdDoc');return d&&d.innerText.length>200&&!d.innerText.startsWith('Loading')}", arg=t, timeout=30000)
        txt = page.inner_text("#pdCard")
        heads = page.eval_on_selector_all("#pdDoc h1,#pdDoc h2", "hs=>hs.length")
        bad = [rx.pattern[:20] for rx in (SNAKE_RE, PATH_RE, UUID_RE, OCC_RE, CLI_RE, PII_RE) if rx.search(txt)]
        if bad:
            leaks.append({t: bad, "sample": [m.group(0) for rx in (SNAKE_RE, PATH_RE, PII_RE) for m in [rx.search(txt)] if m][:3]})
        per[t] = {"chars": len(txt), "headings": heads}
    checks = {
        "six docs listed with plain-English titles": btns == TITLES,
        "every doc renders with headings": all(v["chars"] > 400 and v["headings"] >= 1 for v in per.values()) and len(per) == 6,
        "no snake_case / paths / UUID / OCC / CLI / PII in visible text": not leaks,
        "tab label plain English": "Playbooks & Pod Docs" in page.inner_text("#tabs"),
        "0 console errors": not errors,
    }
    return checks, {"docs": per, "leaks": leaks[:6], "errors": errors[:5]}


def main():
    from playwright.sync_api import sync_playwright
    live = sys.argv[sys.argv.index("--live") + 1] if "--live" in sys.argv else None
    tmp = Path(tempfile.mkdtemp(prefix="pw_poddocs_"))
    httpd = None
    try:
        if live:
            r = subprocess.run([VENV_PY, "-c", "import sys;sys.path.insert(0,'scripts');import vault_crypto as v;sys.stdout.write(v.load_password() or '')"],
                               cwd=str(ROOT), capture_output=True, text=True)
            pw, base = r.stdout, live.split("#")[0]
            if not pw:
                raise SystemExit("no vault password configured")
            url = base.rstrip("/") + "/#play"
        else:
            site = build_local(tmp)
            s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
            handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(site), **k)
            handler.log_message = lambda *a: None
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            pw, base = PW, f"http://127.0.0.1:{port}/"
            url = base + "index.html#play"
        with sync_playwright() as p:
            b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 1100})
            checks, info = check(page, url, pw)
            if "--shot" in sys.argv:
                page.locator("#pdCard").screenshot(path=sys.argv[sys.argv.index("--shot") + 1])
            b.close()
        exposed = plaintext_reachable(base)
        checks["docs not public as plaintext"] = not exposed
        info["exposed"] = exposed
        print(json.dumps({"checks": checks, "info": info}, indent=1))
        ok = all(checks.values()); print("PLAYWRIGHT", "PASS" if ok else "FAIL", f"{sum(checks.values())}/{len(checks)}")
        return 0 if ok else 1
    finally:
        if httpd:
            httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

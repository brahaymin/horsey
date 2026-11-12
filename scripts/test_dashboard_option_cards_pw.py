#!/usr/bin/env python3
"""Playwright check: option ticket cards render contracts + premiums, with no raw OCC,
snake_case or path leaks, and 0 console errors.

Ticket / pod data is SYNTHETIC (written here). The gamma panels need page scaffolding
(js/feed-adapter.js + local snapshot files), which is served read-only from the local
checkout and never written. Requires playwright (python -m pip install playwright).
Run: python3 scripts/test_dashboard_option_cards_pw.py [path/to/app_unlocked.html]
"""
import http.server
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "app_unlocked.html"
OCC_RE = re.compile(r"\b[A-Z]{1,6}\d{6}[CP]\d{8}\b")
SNAKE_RE = re.compile(r"\b[a-z]+_[a-z0-9_]+\b")
PATH_RE = re.compile(r"(/workspace|/home/|/tmp/|\bpod/|\.jsonl?\b)")

FW = [{"id": i, "name": f"check {i}", "pass": True} for i in range(1, 7)]
TICKETS = [
    {"ticket_id": "TKT-SYN-CALL", "status": "conditional_preapproved", "instrument": "option_call", "direction": "long",
     "symbol": "SPY", "occ_symbol": "SPY301108C00760000", "option_expiration": "2030-11-08", "strike": 760.0,
     "quantity": 4, "entry": 2.00, "stop": 1.40, "target1": 3.80, "target2": 5.00, "session_target": "2030-11-08",
     "actual_dollar_risk": 240.0, "risk_pct_effective": 0.24, "firewall": FW, "verdict": "APPROVED_FOR_STAGING",
     "time_stop": "45m if <0.5R progress; no new risk after 14:30 CT; flat 0DTE by 14:45 CT",
     "alpaca_paper_payload": {"order": {"symbol": "SPY301108C00760000", "qty": "4", "type": "limit", "limit_price": "2.00", "side": "buy"}}},
    {"ticket_id": "TKT-SYN-PUT", "status": "filled_paper", "instrument": "option_put", "direction": "long",
     "symbol": "SPY", "option_expiration": "2030-11-12", "strike": 755.5,
     "quantity": 1, "entry": 3.10, "stop": 2.05, "target1": 6.25, "target2": 8.35, "session_target": "2030-11-08",
     "risk_pct_effective": 0.105, "firewall": FW, "time_stop": "45m if <0.5R progress; no new risk after 14:30 CT"},
    {"ticket_id": "TKT-SYN-STK", "status": "conditional_preapproved", "instrument": "stock", "direction": "short",
     "symbol": "SPY", "quantity": 125, "entry": 749.5, "stop": 751.0, "target1": 745.0, "target2": 741.0,
     "actual_dollar_risk": 187.5, "risk_pct_effective": 0.375, "firewall": FW, "session_target": "2030-11-08",
     "time_stop": "45m if <0.5R progress; no new risk after 14:30 CT"},
    {"ticket_id": "TKT-SYN-VOID", "status": "voided_paper", "instrument": "option_call", "direction": "long",
     "symbol": "SPY", "occ_symbol": "SPY301108C00762000", "option_expiration": "2030-11-08", "strike": 762.0,
     "quantity": 2, "entry": 2.13, "stop": 1.18, "target1": 7.1, "target2": 7.1, "session_target": "2030-11-08",
     "risk_pct_effective": 0.19, "firewall": FW, "time_stop": "45m if <0.5R progress",
     "void_reason": "Stale premium: spread 6.1% of mid is above 5.0%; ticket voided (no chase)",
     "void_reason_plain": "Voided: quote stale or spread too wide"},
    {"ticket_id": "TKT-SYN-SKIP", "status": "skipped_paper", "instrument": "option_put", "direction": "long",
     "symbol": "SPY", "occ_symbol": "SPY301108P00755000", "option_expiration": "2030-11-08", "strike": 755.0,
     "quantity": 1, "entry": 1.50, "stop": 1.00, "target1": 3.0, "target2": 4.0, "session_target": "2030-11-08",
     "risk_pct_effective": 0.05, "firewall": FW, "time_stop": "30m if <0.5R progress",
     "skip_reason": "Size x0.5 (45D net GEX < -$5B) rounds 1 to 0; ticket skipped",
     "skip_reason_plain": "Skipped: reduced size rounds to zero contracts"},
]


def build_site(tmp: Path):
    shutil.copy2(APP, tmp / "index.html")
    for name in ("js", "snapshots", "history", "latest.json", "latest.md"):
        src = ROOT / name
        if src.exists():
            (tmp / name).symlink_to(src)
    pod = tmp / "pod"
    for d in ("ledger", "risk", "signals", "reviews", "track_record", "ceo_decisions"):
        (pod / d).mkdir(parents=True, exist_ok=True)
    (pod / "ledger" / "paper_blotter.jsonl").write_text("".join(json.dumps(t) + "\n" for t in TICKETS))
    files = {
        "roster.json": {"agents": []}, "scorecard.json": {"agents": {}},
        "signals/latest_pod_state.json": {"agents": {}, "cio_synthesis": {"summary": "Synthetic test state"}, "disclaimer": "Paper demo"},
        "paper_account_state.json": {"status": "connected", "equity": 100000.0, "cash": 100000.0, "positions": [], "orders": []},
        "paper_execution_events.json": {"status": "market_closed", "events": [], "armed_tickets": [
            {k: t.get(k) for k in ("ticket_id", "status", "symbol", "quantity", "entry", "stop", "target1", "target2", "session_target")}
            for t in TICKETS if t["status"] == "conditional_preapproved"]},
        "risk/limits.json": {"equity_reference": 50000.0, "alpaca_paper_equity": 100000.0,
                             "limits": {"max_risk_per_ticket_pct": 0.5, "max_total_open_risk_pct": 1.5},
                             "current_usage": {}, "kill_switch": {"breached": False, "status": "ARMED_CLEAR", "breached_limits": []}},
        "risk/portfolio_snapshot.json": {"equity_sizing_base": 50000.0, "alpaca_paper_equity": 100000.0,
                                         "delta_exposure": {"open_only": {}, "including_staged": {}},
                                         "capital_at_risk": {}, "theme_concentration": {}, "specialist_correlation": {}},
        "memos_index.json": {"memos": []}, "ceo_questions.json": {"questions": []},
        "ceo_decisions/latest_decisions.json": {}, "reviews/reviews_index.json": {"reviews": []},
        "track_record/weekly_summary.json": {},
        "alpaca_last_dry_run.json": {"status": "none"},
    }
    for rel, obj in files.items():
        (pod / rel).write_text(json.dumps(obj))


def main() -> int:
    from playwright.sync_api import sync_playwright
    tmp = Path(tempfile.mkdtemp(prefix="pw_optcards_"))
    try:
        build_site(tmp)
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(tmp), **k)
        handler.log_message = lambda *a: None
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        errors, results = [], {}
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page(viewport={"width": 1500, "height": 1000})
            page.on("console", lambda m: errors.append("console: " + m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
            page.goto(f"http://127.0.0.1:{port}/index.html#pod", wait_until="networkidle")
            page.wait_for_selector("#podBlot tr[data-ticket='TKT-SYN-CALL']", timeout=20000)
            page.wait_for_timeout(1500)
            rows = {tid: page.inner_text(f"#podBlot tr[data-ticket='{tid}']") for tid in ("TKT-SYN-CALL", "TKT-SYN-PUT", "TKT-SYN-STK", "TKT-SYN-VOID", "TKT-SYN-SKIP")}
            blot = page.inner_text("#podBlot")
            prev = page.inner_text("#alpacaPreview") if page.query_selector("#alpacaPreview") else ""
            body = page.inner_text("body")
            httpd.shutdown(); b.close()
        call, put, stk = rows["TKT-SYN-CALL"], rows["TKT-SYN-PUT"], rows["TKT-SYN-STK"]
        checks = {
            "call contract label": "SPY Nov 8 $760 Call (0DTE)" in call,
            "call contracts qty": "Buy 4 contracts" in call,
            "call premium entry/stop": "$2.00 / $1.40 premium" in call and "$200 / $140 per contract" in call,
            "call premium targets": "$3.80 / $5.00 premium" in call and "$380 / $500 per contract" in call,
            "call max loss": "Max loss $240" in call,
            "put built from expiry+strike, 4DTE": "SPY Nov 12 $755.5 Put (4DTE)" in put,
            "put single contract": "Buy 1 contract" in put and "Buy 1 contracts" not in put,
            "put max loss": "Max loss $105" in put,
            "stock row unchanged (shares)": "× 125" in stk and "749.5 / 751" in stk and "contract" not in stk.lower(),
            "preview shows contract + premium": "SPY Nov 8 $760 Call (0DTE)" in prev and "Buy 4 contracts @ $2.00 premium" in prev and "TKT-SYN" not in prev,
            "no raw OCC in visible page": not OCC_RE.search(body),
            "no snake_case in ticket table/preview": not SNAKE_RE.search(blot + "\n" + prev),
            "no paths in ticket table/preview": not PATH_RE.search(blot + "\n" + prev),
            "voided shown in plain words": "Voided: quote stale or spread too wide" in rows["TKT-SYN-VOID"]
                                            and "%" not in rows["TKT-SYN-VOID"].split("Voided")[-1] and "voided_paper" not in blot,
            "skipped shown in plain words": "Skipped: reduced size rounds to zero contracts" in rows["TKT-SYN-SKIP"],
            "0 console errors": not errors,
        }
        results = {"checks": checks, "errors": errors[:10], "rows": rows, "preview": prev}
        print(json.dumps(results, indent=1))
        ok = all(checks.values())
        print("PLAYWRIGHT", "PASS" if ok else "FAIL", f"{sum(checks.values())}/{len(checks)}")
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

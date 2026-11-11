#!/usr/bin/env python3
"""pod_engine.py — Shared state bus, Pre-Trade Risk Firewall & Bracket Order Sizer,
and Self-Training Quant Auditor for the horsey multi-agent pod.

Paper / architecture demo — NOT live financial advice. Zero paid dependencies.

Subcommands:
  status              Executive CIO brief (Central Time)
  size-ticket         Deterministic 6-point 50-Mistakes firewall + bracket sizer
  log-signal          Validate & record a specialist signal
  grade               Grade elapsed signals vs history/timeseries.jsonl, update pod/scorecard.json
  build-memos-index   Index CIO/specialist memos for the web app
  bridge-ledger       Bridge specialist ledgers into signals_log
  post-questions      Publish CEO question set
  alpaca-status       Alpaca Paper account/positions/orders → pod/paper_account_state.json
  alpaca-sync         Alias of alpaca-status (refresh sanitized paper state)
  alpaca-find-option  Resolve OCC option symbol (offline OCC math; live quote if keys set)
  alpaca-stage-ticket Build/validate Alpaca Paper order from blotter ticket (--dry-run / --submit-paper)
  alpaca-rth-tick     RTH trigger check + OCO management for conditional_preapproved paper tickets
"""
import argparse, json, math, os, sys, uuid, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover
    CT = timezone(timedelta(hours=-6))

ROOT = os.environ.get("HORSEY_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
P = lambda *a: os.path.join(ROOT, *a)
LATEST, ROSTER, SCORE = P("latest.json"), P("pod", "roster.json"), P("pod", "scorecard.json")
STATE, SIGLOG, BLOTTER = P("pod", "signals", "latest_pod_state.json"), P("pod", "ledger", "signals_log.jsonl"), P("pod", "ledger", "paper_blotter.jsonl")
PAPER_STATE, PAPER_ORDERS = P("pod", "paper_account_state.json"), P("pod", "ledger", "alpaca_paper_orders.jsonl")
PAPER_EVENTS = P("pod", "paper_execution_events.json")
TS = P("history", "timeseries.jsonl")
REGIMES = ("short_gamma_backwardation", "positive_gamma_contango", "below_200sma")
PTJ_ID = "05895e3e-1c82-4cec-9ff3-07aa73b5c0db"
DISCLAIMER = "Paper / Architecture Demo — Not Live Financial Advice"


def now_ct():
    return datetime.now(CT)


def iso_ct(dt=None):
    return (dt or now_ct()).astimezone(CT).strftime("%Y-%m-%dT%H:%M:%S%z")


def parse_ts(s):
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if len(s) >= 5 and (s[-5] in "+-") and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=CT)


def rj(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def wj(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def rjl(path):
    out = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return out


def ajl(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def market():
    d = rj(LATEST, {}) or {}
    s = d.get("summary", {})
    vts = d.get("vol_term_structure", {})
    return {
        "ts": s.get("timestamp_ct"), "spy": s.get("spy_regular_close") or s.get("spy_price"),
        "sma200": s.get("spy_sma_200"), "gex45": s.get("net_gex_45d_dollars"), "gex0": s.get("net_gex_0dte_dollars"),
        "vix": s.get("vix"), "vix9d": s.get("vix9d"), "vix3m": s.get("vix3m"),
        "call_wall_0dte": s.get("call_wall_0dte"), "put_wall_0dte": s.get("put_wall_0dte"),
        "call_wall_45d": s.get("call_wall_45d"), "put_wall_45d": s.get("put_wall_45d"),
        "sox": s.get("sox_1d_pct"), "bkx": s.get("bkx_1d_pct"), "vol_regime": vts.get("regime"),
    }


def classify_regime(m):
    """Return list of active regime tags from a market dict."""
    tags = []
    back = m.get("vix") and m.get("vix3m") and m["vix"] > m["vix3m"]
    if (m.get("gex45") or 0) < 0 and back:
        tags.append("short_gamma_backwardation")
    if (m.get("gex45") or 0) > 0 and not back:
        tags.append("positive_gamma_contango")
    if m.get("spy") and m.get("sma200") and m["spy"] < m["sma200"]:
        tags.append("below_200sma")
    return tags


# ---------------------------------------------------------------- sizer
def size_ticket(a, m=None):
    m = m or market()
    errors, checks = [], []
    instr = a.instrument
    mult = 100 if instr.startswith("option") else 1
    direction = "long" if instr.startswith("option") else a.direction  # options = long premium
    if instr.startswith("option") and a.direction == "short":
        errors.append("Short option premium (naked) is not supported by this sizer; size long premium only.")
    min_rr = a.min_rr if a.min_rr is not None else (5.0 if a.strategy_agent in (PTJ_ID, "ptj", "PTJ Macro & Regime Strategist") else 3.0)

    # 1 invalidation
    entry, stop = a.entry, a.stop
    inval_ok = stop is not None and entry is not None and ((direction == "long" and stop < entry) or (direction == "short" and stop > entry))
    if stop is None:
        errors.append("REJECTED: --stop (hard invalidation) is mandatory (Mistake #1: no predefined stop).")
    elif not inval_ok:
        errors.append(f"REJECTED: stop {stop} is on the wrong side of entry {entry} for a {direction} position.")
    checks.append({"id": 1, "name": "Hard invalidation present & correct side", "pass": bool(inval_ok)})

    r_unit = abs(entry - stop) if inval_ok else None
    sgn = 1 if direction == "long" else -1
    t1 = a.target1 if a.target1 is not None else (entry + sgn * 3 * r_unit if r_unit else None)
    t2 = a.target2 if a.target2 is not None else (entry + sgn * 5 * r_unit if r_unit else None)
    best = None
    if r_unit:
        rewards = [sgn * (t - entry) / r_unit for t in (t1, t2) if t is not None]
        best = max(rewards) if rewards else None
    asym_ok = best is not None and best >= min_rr - 1e-9
    checks.append({"id": 2, "name": f"Asymmetry >= {min_rr:g}R", "pass": asym_ok, "value": round(best, 2) if best is not None else None})

    cap = 100.0 if a.override_cap else 1.0
    req = a.risk_pct
    eff = min(req, cap)
    checks.append({"id": 3, "name": f"Risk % <= cap ({cap:g}%)", "pass": req <= cap, "value": req, "applied": eff})

    vix = m.get("vix")
    throttle = []
    if a.drawdown_mode:
        throttle.append("drawdown_mode")
    if vix is not None and vix > 25:
        throttle.append(f"VIX {vix} > 25")
    if throttle:
        eff = eff / 2
    checks.append({"id": 4, "name": "Vol-regime / drawdown size throttle applied", "pass": True,
                   "value": ("halved: " + ", ".join(throttle)) if throttle else "not required"})

    ref = a.underlying if a.underlying is not None else (entry if instr == "stock" and a.symbol.upper() == "SPY" else m.get("spy"))
    lo, hi = m.get("put_wall_0dte"), m.get("call_wall_0dte")
    nml = False
    if ref is not None and lo and hi and hi > lo:
        lo, hi = min(lo, hi), max(lo, hi)
        w = hi - lo
        nml = (lo + 0.3 * w) < ref < (hi - 0.3 * w)
    checks.append({"id": 5, "name": "Not mid-range no-man's-land vs 0DTE walls", "pass": not nml,
                   "value": {"ref": ref, "put_wall": lo, "call_wall": hi}})

    ts_ok = bool(a.time_stop) or instr == "stock"
    checks.append({"id": 6, "name": "Time-stop defined (mandatory for options)", "pass": ts_ok, "value": a.time_stop})

    dollar_risk = a.equity * eff / 100.0
    qty = int(math.floor(dollar_risk / (r_unit * mult))) if r_unit else 0
    if r_unit and qty < 1:
        errors.append("Position rounds to 0 units at this risk budget — widen budget or tighten structure.")
    allpass = all(c["pass"] for c in checks) and not errors
    t = {
        "ticket_id": "TKT-" + uuid.uuid4().hex[:8].upper(), "created_ct": iso_ct(), "tag": "PAPER_TICKET", "disclaimer": DISCLAIMER,
        "symbol": a.symbol.upper(), "strategy_agent": a.strategy_agent, "instrument": instr, "direction": direction,
        "equity": a.equity, "risk_pct_requested": req, "risk_pct_effective": round(eff, 4), "dollar_risk_budget": round(dollar_risk, 2),
        "entry": entry, "stop": stop, "risk_per_unit": round(r_unit, 4) if r_unit else None, "multiplier": mult, "quantity": qty,
        "actual_dollar_risk": round(qty * r_unit * mult, 2) if r_unit else 0, "position_notional": round(qty * entry * mult, 2),
        "target1": round(t1, 4) if t1 is not None else None, "target2": round(t2, 4) if t2 is not None else None,
        "best_rr": round(best, 2) if best is not None else None, "min_rr": min_rr,
        "oco_bracket": {"stop_order": stop, "take_profit_1": round(t1, 4) if t1 else None, "take_profit_2": round(t2, 4) if t2 else None,
                        "scale_plan": "50% at T1, trail stop to entry, 50% at T2"},
        "time_stop": a.time_stop, "vix_at_ticket": vix, "regime_tags": classify_regime(m),
        "firewall": checks, "errors": errors, "verdict": "APPROVED_FOR_STAGING" if allpass else "BLOCKED",
    }
    return t


def ticket_md(t):
    L = [f"### Order Ticket {t['ticket_id']} — **{t['verdict']}**  _({DISCLAIMER})_",
         f"- {t['direction'].upper()} {t['quantity']} × {t['symbol']} ({t['instrument']}) @ {t['entry']}  | hard stop {t['stop']}",
         f"- Risk: {t['risk_pct_effective']}% of ${t['equity']:,.0f} = ${t['dollar_risk_budget']:,.2f} budget (actual ${t['actual_dollar_risk']:,.2f})",
         f"- Targets: T1 {t['target1']} · T2 {t['target2']} · best {t['best_rr']}R (min {t['min_rr']}R)  | time stop: {t['time_stop'] or '—'}",
         "- Firewall:"]
    L += [f"  {'✅' if c['pass'] else '❌'} {c['id']}. {c['name']}" + (f" — {c.get('value')}" if c.get("value") is not None else "") for c in t["firewall"]]
    L += [f"- ⚠️ {e}" for e in t["errors"]]
    return "\n".join(L)


# ---------------------------------------------------------------- signals
def log_signal(a):
    roster = rj(ROSTER, {}) or {}
    ids = {x["id"]: x for x in roster.get("agents", []) if x.get("id")}
    if ids and a.agent_id not in ids:
        sys.exit(f"ERROR: unknown agent_id {a.agent_id}")
    if not 0.0 <= a.probability <= 1.0:
        sys.exit("ERROR: --probability must be in [0,1]")
    if a.direction in ("long", "short"):
        if a.entry is None or a.stop is None or a.target is None:
            sys.exit("ERROR: directional signals need --entry, --stop and --target")
        sg = 1 if a.direction == "long" else -1
        if sg * (a.entry - a.stop) <= 0 or sg * (a.target - a.entry) <= 0:
            sys.exit("ERROR: stop/target on wrong side of entry")
    m = market()
    sig = {"signal_id": "SIG-" + uuid.uuid4().hex[:8].upper(), "logged_ct": iso_ct(), "agent_id": a.agent_id,
           "agent_name": ids.get(a.agent_id, {}).get("name"), "symbol": a.symbol.upper(), "direction": a.direction,
           "entry": a.entry, "stop": a.stop, "target": a.target, "probability": a.probability, "horizon_hours": a.horizon_hours,
           "regime_tags": classify_regime(m), "stance": a.stance, "note": a.note, "tag": a.tag, "graded": False}
    if a.as_of:
        sig["logged_ct"] = iso_ct(parse_ts(a.as_of))
    ajl(SIGLOG, sig)
    st = rj(STATE, {}) or {}
    st.setdefault("agents", {})
    cur = st["agents"].get(a.agent_id, {})
    cur.update({"last_signal_id": sig["signal_id"], "last_signal_ct": sig["logged_ct"], "stance": a.stance or cur.get("stance"),
                "last_note": a.note})
    st["agents"][a.agent_id] = cur
    st["updated_ct"] = iso_ct()
    wj(STATE, st)
    return sig


# ---------------------------------------------------------------- grading
def load_path():
    rows = []
    for r in rjl(TS):
        try:
            rows.append((parse_ts(r["timestamp_ct"]), float(r.get("spy_price") or r.get("spy_regular_close"))))
        except (KeyError, TypeError, ValueError):
            pass
    rows.sort()
    return rows


def grade_one(sig, path, now):
    t0 = parse_ts(sig["logged_ct"])
    t1 = t0 + timedelta(hours=float(sig.get("horizon_hours") or 24))
    if now < t1:
        return None
    seg = [p for (t, p) in path if t0 < t <= t1]
    if not seg or sig.get("direction") not in ("long", "short") or sig.get("symbol") != "SPY":
        return {"graded": True, "grade_status": "ungradable_no_data" if not seg else "ungradable_non_directional"}
    sg = 1 if sig["direction"] == "long" else -1
    e, s, tg = sig["entry"], sig["stop"], sig["target"]
    r = abs(e - s)
    outcome, realized = None, None
    for p in seg:
        if sg * (p - s) <= 0:
            outcome, realized = "hit_stop", -1.0
            break
        if sg * (p - tg) >= 0:
            outcome, realized = "hit_target", sg * (tg - e) / r
            break
    if outcome is None:
        outcome, realized = "time_exit", sg * (seg[-1] - e) / r
    win = 1.0 if realized > 0 else 0.0
    return {"graded": True, "grade_status": outcome, "hit_target": outcome == "hit_target", "hit_stop": outcome == "hit_stop",
            "realized_r": round(realized, 3), "brier_error": round((sig["probability"] - win) ** 2, 4), "graded_ct": iso_ct(now)}


def update_score(sc, sig, g, alpha=0.2):
    a = sc.setdefault("agents", {}).setdefault(sig["agent_id"], {"name": sig.get("agent_name"), "signals_logged": 0, "graded_count": 0,
        "win_rate": None, "avg_realized_r": None, "brier_score": None, "regime_weights": {k: 1.0 for k in REGIMES}, "current_effective_weight": None})
    n = a["graded_count"] + 1
    win = 1.0 if g["realized_r"] > 0 else 0.0
    a["win_rate"] = round(((a["win_rate"] or 0) * (n - 1) + win) / n, 4)
    a["avg_realized_r"] = round(((a["avg_realized_r"] or 0) * (n - 1) + g["realized_r"]) / n, 4)
    a["brier_score"] = round(((a["brier_score"] or 0) * (n - 1) + g["brier_error"]) / n, 4)
    a["graded_count"] = n
    # skill in [0.25, 2.0]: rewards positive R and calibration (brier < 0.25 baseline)
    skill = max(0.25, min(2.0, 1.0 + 0.5 * max(-1.0, min(2.0, g["realized_r"])) + (0.25 - g["brier_error"])))
    for tag in sig.get("regime_tags") or []:
        if tag in a["regime_weights"]:
            a["regime_weights"][tag] = round((1 - alpha) * a["regime_weights"][tag] + alpha * skill, 4)


def recompute_effective(sc, m=None):
    tags = classify_regime(m or market())
    raw = {}
    for aid, a in sc.get("agents", {}).items():
        if a.get("role") in ("cio", "data", "engine"):
            continue
        ws = [a["regime_weights"][t] for t in tags if t in a.get("regime_weights", {})] or [1.0]
        raw[aid] = sum(ws) / len(ws)
    tot = sum(raw.values()) or 1
    for aid, v in raw.items():
        sc["agents"][aid]["current_effective_weight"] = round(v / tot, 4)
    sc["active_regime_tags"] = tags
    sc["updated_ct"] = iso_ct()


def grade(now=None):
    now = now or now_ct()
    sigs, path = rjl(SIGLOG), load_path()
    sc = rj(SCORE, {"agents": {}}) or {"agents": {}}
    for a in sc.get("agents", {}).values():
        a["signals_logged"] = 0
    for s in sigs:
        if s["agent_id"] in sc.get("agents", {}):
            sc["agents"][s["agent_id"]]["signals_logged"] += 1
    done = []
    for s in sigs:
        if s.get("graded"):
            continue
        g = grade_one(s, path, now)
        if g is None:
            continue
        s.update(g)
        if "realized_r" in g:
            update_score(sc, s, g)
        done.append(s)
    recompute_effective(sc)
    wj(SCORE, sc)
    tmp = SIGLOG + ".tmp"
    with open(tmp, "w") as f:
        for s in sigs:
            f.write(json.dumps(s) + "\n")
    os.replace(tmp, SIGLOG)
    return done, sc


# ---------------------------------------------------------------- status
def fmtB(v):
    return "—" if v is None else f"{'-' if v < 0 else ''}${abs(v)/1e9:.2f}B"


def status():
    m, roster, sc, st = market(), rj(ROSTER, {}) or {}, rj(SCORE, {}) or {}, rj(STATE, {}) or {}
    L = [f"=== EXECUTIVE CIO BRIEF — {now_ct().strftime('%a %b %d, %Y %I:%M %p')} CT ===", f"({DISCLAIMER})", ceo_decisions_block(), "",
         f"Data as of {m['ts']} | SPY ${m['spy']} | 200-SMA ${m['sma200']:.2f} | 45D Net GEX {fmtB(m['gex45'])} | "
         f"VIX {m['vix']} (VIX9D {m['vix9d']} / VIX3M {m['vix3m']}) | ^SOX {m['sox']}% vs ^BKX {m['bkx']}%",
         f"0DTE walls: put {m['put_wall_0dte']} / call {m['call_wall_0dte']} | 45D walls: put {m['put_wall_45d']} / call {m['call_wall_45d']}",
         f"Active regime tags: {', '.join(classify_regime(m)) or 'none'}",
         f"Risk throttle: {'HALVED (VIX > 25) → 0.25%–0.50% per idea' if (m['vix'] or 0) > 25 else 'normal 0.50%–1.00%'}", "", "Specialists:"]
    names = {a["id"]: a["name"] for a in roster.get("agents", []) if a.get("id")}
    for aid, s in (st.get("agents") or {}).items():
        w = (sc.get("agents", {}).get(aid) or {}).get("current_effective_weight")
        L.append(f" • {names.get(aid, aid)} [w={w}] — {s.get('stance')}")
        if s.get("headline"):
            L.append(f"     {s['headline']}")
    if st.get("cio_synthesis"):
        L += ["", "CIO synthesis: " + st["cio_synthesis"].get("summary", "")]
    bl = rjl(BLOTTER)
    L += ["", f"Paper blotter: {len(bl)} ticket(s); signals logged: {len(rjl(SIGLOG))}"]
    return "\n".join(L)



# ---------------------------------------------------------------- memos index + specialist bridge
MEMO_SOURCES = [
    ("pod/cio_briefings", "_cio_briefing.md", "cio", "CIO Executive Briefing", "CIO & Risk Gatekeeper"),
    ("pod/ptj_macro", "_thesis.md", "ptj", "PTJ Macro & Regime", "PTJ Macro & Regime Strategist"),
    ("pod/intraday_tape_gamma", "_thesis.md", "intraday", "Intraday Tape & Gamma", "Intraday Tape & Gamma Tactician"),
    ("pod/buffett_value", "_thesis.md", "buffett", "Buffett Value Compounder", "Buffett Value Compounder"),
    ("pod/income_sleeve", "_income_sleeve.md", "income_sleeve", "Income Sleeve (Robinhood)", "Buffett Value Compounder"),
]
ROLE_ORDER = ["cio", "ptj", "intraday", "buffett", "income_sleeve", "gamma_data"]


def _memo_meta(rel, role, label, agent):
    import re
    full = P(rel)
    base = os.path.basename(rel)
    m = re.match(r"(\d{4}-\d{2}-\d{2})[_-](\d{2})(\d{2})-ct-?([a-z0-9-]*?)(?:_thesis|_cio_briefing|_income_sleeve)?\.md$", base)
    if role == "gamma_data":
        parts = rel.split("/")  # snapshots/YYYY/MM/DD/HHMM-ct-tag.md
        m2 = re.match(r"(\d{2})(\d{2})-ct-?(.*)\.md$", parts[-1])
        date = parts[3] if re.match(r"\d{4}-\d{2}-\d{2}$", parts[3]) else f"{parts[1]}-{parts[2]}-{parts[3]}"
        hh, mm, tag = (m2.group(1), m2.group(2), m2.group(3)) if m2 else ("00", "00", "")
    elif m:
        date, hh, mm, tag = m.group(1), m.group(2), m.group(3), m.group(4)
    else:
        dt = datetime.fromtimestamp(os.path.getmtime(full), CT)
        date, hh, mm, tag = dt.strftime("%Y-%m-%d"), dt.strftime("%H"), dt.strftime("%M"), ""
    title, summary = base, ""
    with open(full, encoding="utf-8", errors="replace") as f:
        lines = [l.rstrip("\n") for l in f]
    for l in lines:
        if l.startswith("# "):
            title = l[2:].strip(); break
    for l in lines:
        t = l.strip()
        if t and not t.startswith(("#", "|", "---", "**From", "**Agent", "**To", "**Source", "**Written", "Date:", "```", ">")):
            summary = re.sub(r"[*`>_]", "", t).strip()[:220]; break
    h12 = int(hh) % 12 or 12
    disp = f"{datetime.strptime(date, '%Y-%m-%d').strftime('%a %b %d, %Y')} · {h12}:{mm} {'AM' if int(hh) < 12 else 'PM'} CT" + (f" · {tag}" if tag else "")
    run = f"{date}_{hh}{mm}" + (f"-{tag}" if tag else "")
    return {"id": f"{role}:{run}", "role": role, "label": label, "agent_name": agent, "title": title, "display_ct": disp,
            "session_date": date, "run_id": run, "sort_key": f"{date}T{hh}{mm}", "path": rel, "bytes": os.path.getsize(full), "summary_line": summary}


def _memo_guard():
    """memo_publishable from vault_crypto (single source of truth). If it can't be loaded,
    fail closed: nothing under pod/income_sleeve/ is publishable."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_vault_crypto_guard", os.path.join(os.path.dirname(os.path.abspath(__file__)), "vault_crypto.py"))
        vc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vc)
        approved = vc.load_approved_memos(ROOT)
        return lambda rel: vc.memo_publishable(rel, approved=approved)
    except Exception as e:
        print(f"memo guard unavailable, failing closed: {e}", file=sys.stderr)
        return lambda rel: not str(rel or "").replace(os.sep, "/").startswith("pod/income_sleeve/")


def build_memos_index():
    import glob
    items = []
    publishable = _memo_guard()
    for d, suf, role, label, agent in MEMO_SOURCES:
        for f in glob.glob(P(d, "*" + suf)):
            rel = os.path.relpath(f, ROOT).replace(os.sep, "/")
            if not publishable(rel):
                continue  # unapproved (e.g. Income Sleeve memo without CEO approval)
            items.append(_memo_meta(rel, role, label, agent))
    for f in glob.glob(P("snapshots", "*", "*", "*", "*.md")):
        items.append(_memo_meta(os.path.relpath(f, ROOT).replace(os.sep, "/"), "gamma_data", "SPY Gamma Raw Data Report", "SPY Gamma Structure Monitor"))
    items.sort(key=lambda x: (x["sort_key"], -ROLE_ORDER.index(x["role"])), reverse=True)
    by_role = {r: [i["id"] for i in items if i["role"] == r] for r in ROLE_ORDER}
    runs = {}
    for i in items:
        runs.setdefault(i["run_id"], {"run_id": i["run_id"], "display_ct": i["display_ct"], "session_date": i["session_date"], "memos": {}})
        runs[i["run_id"]]["memos"].setdefault(i["role"], i["id"])
    idx = {"schema_version": 1, "generated_ct": iso_ct(), "count": len(items),
           "roles": [{"role": r, "label": next((s[3] for s in MEMO_SOURCES if s[2] == r), "SPY Gamma Raw Data Report"), "count": len(by_role[r])} for r in ROLE_ORDER],
           "latest_cio_briefing": "pod/cio_briefings/latest.md" if os.path.exists(P("pod", "cio_briefings", "latest.md")) else None,
           "by_role": by_role, "by_run": sorted(runs.values(), key=lambda r: r["run_id"], reverse=True), "memos": items}
    # Idempotent: if nothing but generated_ct would change, keep the file (and mtime) untouched
    ip = P("pod", "memos_index.json")
    if os.path.exists(ip):
        try:
            with open(ip) as f:
                old = json.load(f)
            strip = lambda d: {k: v for k, v in d.items() if k != "generated_ct"}
            if json.loads(json.dumps(strip(idx))) == strip(old):
                return old
        except Exception:
            pass
    wj(ip, idx)
    return idx


def bridge_specialist_ledger():
    """Sync entries specialists appended to pod/ledger/signals.jsonl into signals_log.jsonl (non-directional, deduped)."""
    src = rjl(P("pod", "ledger", "signals.jsonl"))
    if not src:
        return 0
    have = {s.get("bridge_key") for s in rjl(SIGLOG)}
    st = rj(STATE, {}) or {}
    added = 0
    for e in src:
        try:
            key = f"{e.get('agent_id')}|{e.get('ts_ct')}"
            if not e.get("agent_id") or key in have:
                continue
            mm = e.get("memo_md") or ""
            rel = os.path.relpath(mm, ROOT) if mm.startswith("/") else mm
            sig = {"signal_id": "SIG-BR-" + uuid.uuid5(uuid.NAMESPACE_URL, key).hex[:8].upper(), "logged_ct": e.get("ts_ct") or iso_ct(),
                   "agent_id": e["agent_id"], "agent_name": e.get("agent"), "symbol": "SPY", "direction": "neutral",
                   "entry": None, "stop": None, "target": None, "probability": 0.5, "horizon_hours": 24,
                   "regime_tags": [t for t in (e.get("regime_tags") or []) if t in REGIMES] or classify_regime(market()),
                   "stance": e.get("primary_bias"), "note": f"bridged from pod/ledger/signals.jsonl; setups {e.get('setup_ids')}",
                   "memo_md": rel, "tag": "BRIDGED_SPECIALIST", "bridge_key": key, "graded": False}
            ajl(SIGLOG, sig); have.add(key); added += 1
            a = st.setdefault("agents", {}).setdefault(e["agent_id"], {})
            a.update({"primary_bias": e.get("primary_bias"), "memo_md": rel, "last_signal_ct": sig["logged_ct"]})
        except Exception as ex:  # never crash on a malformed specialist line
            print(f"bridge: skipped entry ({ex})", file=sys.stderr)
    if added:
        st["updated_ct"] = iso_ct(); wj(STATE, st)
    return added


def _housekeeping():
    try:
        bridge_specialist_ledger()
    except Exception as ex:
        print(f"bridge error: {ex}", file=sys.stderr)
    try:
        build_memos_index()
    except Exception as ex:
        print(f"memos index error: {ex}", file=sys.stderr)



# ---------------------------------------------------------------- CEO questions / decisions
QFILE, DFILE = P("pod", "ceo_questions.json"), P("pod", "ceo_decisions", "latest_decisions.json")


def ceo_decisions_block():
    q, d = rj(QFILE, {}) or {}, rj(DFILE, {}) or {}
    L = ["--- CEO DECISIONS & STANDING DIRECTIVES ---"]
    if not d or d.get("status") in (None, "pending_ceo_input") or not d.get("responses"):
        n = len(q.get("questions", [])) if q.get("status") == "awaiting_ceo" else 0
        L.append(f"Status: PENDING CEO INPUT ({n} open question(s) in pod/ceo_questions.json) — use desk defaults until answered.")
    else:
        labels = {qq["id"]: (qq["title"], {o["value"]: o["label"] for o in qq.get("options", [])}) for qq in q.get("questions", [])}
        L.append(f"Status: {d.get('status')} · submitted {d.get('submitted_ct')} · set {d.get('prompt_set_id')}")
        for k, v in (d.get("responses") or {}).items():
            t, opts = labels.get(k, (k, {}))
            vals = v if isinstance(v, list) else [v]
            L.append(f" • {t}: " + "; ".join(opts.get(x, str(x)) for x in vals))
        if d.get("risk_pct") is not None:
            L.append(f" • Active CEO risk cap per shot: {d['risk_pct']}%")
    if d.get("ceo_note"):
        L.append(f" • CEO note: {d['ceo_note']}")
    return "\n".join(L)


def post_questions(path):
    """CIO helper: validate a questions JSON file and publish it to pod/ceo_questions.json; resets decisions to pending."""
    q = rj(path)
    if not q or not isinstance(q.get("questions"), list) or not q.get("prompt_set_id"):
        sys.exit("ERROR: questions file needs prompt_set_id and questions[]")
    for qq in q["questions"]:
        if qq.get("type") not in ("single", "multi", "text") or not qq.get("id"):
            sys.exit(f"ERROR: bad question {qq.get('id')}")
    q.setdefault("created_ct", iso_ct()); q["status"] = "awaiting_ceo"
    wj(QFILE, q)
    wj(DFILE, {"prompt_set_id": q["prompt_set_id"], "status": "pending_ceo_input", "submitted_ct": None, "responses": {}, "ceo_note": (rj(DFILE, {}) or {}).get("ceo_note", "")})
    return q



# ---------------------------------------------------------------- Alpaca Paper Trading (HARD paper-only rail)
# Hosts are intentionally constants — never accept api.alpaca.markets (live capital).
ALPACA_PAPER_HOST = "https://paper-api.alpaca.markets"
ALPACA_DATA_HOST = "https://data.alpaca.markets"
ALPACA_LIVE_HOST_BLOCKLIST = ("https://api.alpaca.markets", "api.alpaca.markets")
ALPACA_ENV_FILE = P(".env.alpaca")


def _load_dotenv_alpaca(path=None):
    """Parse KEY=VALUE from gitignored .env.alpaca into a dict (no os.environ mutation required)."""
    path = path or ALPACA_ENV_FILE
    out = {}
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def alpaca_creds():
    """Return (key_id, secret) from env or .env.alpaca. Never logs secrets."""
    envf = _load_dotenv_alpaca()
    key = os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_API_KEY") or envf.get("APCA_API_KEY_ID") or envf.get("ALPACA_API_KEY")
    sec = os.environ.get("APCA_API_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY") or envf.get("APCA_API_SECRET_KEY") or envf.get("ALPACA_SECRET_KEY")
    return (key or "").strip() or None, (sec or "").strip() or None


def assert_paper_host(url):
    u = (url or "").strip().lower()
    for bad in ALPACA_LIVE_HOST_BLOCKLIST:
        if bad in u and "paper-api" not in u:
            raise RuntimeError(f"REFUSED: live Alpaca host blocked ({bad}). Paper-only rail requires {ALPACA_PAPER_HOST}")
    if u.startswith("http") and "paper-api.alpaca.markets" not in u and "data.alpaca.markets" not in u:
        raise RuntimeError(f"REFUSED: non-paper Alpaca host: {url}")
    return url


def alpaca_request(method, host, path, body=None, params=None, timeout=20):
    """Authenticated HTTPS call. host must be paper-api or data. Never hits live api.alpaca.markets."""
    assert_paper_host(host)
    key, sec = alpaca_creds()
    if not key or not sec:
        raise RuntimeError("Alpaca Paper keys not configured (set APCA_API_KEY_ID|ALPACA_API_KEY and APCA_API_SECRET_KEY|ALPACA_SECRET_KEY, or .env.alpaca)")
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = host.rstrip("/") + path + q
    assert_paper_host(url)
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header("APCA-API-KEY-ID", key)
    req.add_header("APCA-API-SECRET-KEY", sec)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Alpaca HTTP {e.code} {path}: {err[:400]}") from e


def occ_option_symbol(underlying, expiration, opt_type, strike):
    """Build OCC symbol offline, e.g. SPY + 2026-11-06 + put + 732 → SPY261106P00732000."""
    u = underlying.upper().strip()
    exp = expiration.replace("-", "")
    if len(exp) == 8:
        yymmdd = exp[2:]
    elif len(exp) == 6:
        yymmdd = exp
    else:
        raise ValueError(f"bad expiration {expiration}")
    cp = "P" if str(opt_type).lower().startswith("p") else "C"
    strike_i = int(round(float(strike) * 1000))
    return f"{u}{yymmdd}{cp}{strike_i:08d}"


_OCC_RE = __import__("re").compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


def parse_occ_symbol(occ):
    """'SPY261112C00750000' -> {underlying, expiry 'YYYY-MM-DD', right, strike} or None."""
    m = _OCC_RE.match(str(occ or "").strip().upper())
    if not m:
        return None
    u, yy, mm, dd, cp, k = m.groups()
    return {"underlying": u, "expiry": f"20{yy}-{mm}-{dd}", "right": "put" if cp == "P" else "call",
            "strike": int(k) / 1000.0, "occ_symbol": m.group(0)}


def option_right(ticket):
    r = str(ticket.get("right") or ticket.get("option_type") or "").lower()
    if r in ("put", "p", "call", "c"):
        return "put" if r.startswith("p") else "call"
    instr = str(ticket.get("instrument") or "").lower()
    if instr in ("option_put", "option_call"):
        return "put" if instr.endswith("put") else "call"
    return None


def option_contract_spec(ticket, occ_symbol=None, expiration=None, strike=None):
    """Resolve an option ticket's contract from EXPLICIT fields only (fail closed).

    Accepted: ticket occ_symbol (or explicit occ_symbol arg), or expiry
    (option_expiration | expiry, or explicit arg) + strike (ticket strike, or explicit arg).
    Any given fields must agree with each other. session_target is NOT an expiry, and
    alpaca_paper_payload symbols from older dry-runs are ignored. Returns (spec, None)
    or (None, error)."""
    und = (ticket.get("symbol") or "SPY").upper()
    right = option_right(ticket)
    occ = occ_symbol or ticket.get("occ_symbol")
    exp = expiration or ticket.get("option_expiration") or ticket.get("expiry")
    k = strike if strike is not None else ticket.get("strike")
    if exp is not None:
        exp = str(exp)
        if not __import__("re").match(r"^\d{4}-\d{2}-\d{2}$", exp):
            return None, f"bad expiry {exp!r} (want YYYY-MM-DD)"
    try:
        k = float(k) if k is not None else None
    except Exception:
        return None, f"bad strike {k!r}"
    if occ:
        po = parse_occ_symbol(occ)
        if not po:
            return None, f"bad OCC symbol {occ!r}"
        if po["underlying"] != und:
            return None, f"OCC underlying {po['underlying']} != ticket symbol {und}"
        if right and po["right"] != right:
            return None, f"OCC right {po['right']} != ticket right {right}"
        if exp and po["expiry"] != exp:
            return None, f"OCC expiry {po['expiry']} != ticket expiry {exp}"
        if k is not None and abs(po["strike"] - k) > 1e-6:
            return None, f"OCC strike {po['strike']} != ticket strike {k}"
        return po, None
    if not right:
        return None, "missing option right (instrument option_put/option_call or right)"
    missing = [n for n, v in (("occ_symbol or expiry", exp), ("strike", k)) if v is None]
    if missing:
        return None, "missing " + " and ".join(missing)
    sym = occ_option_symbol(und, exp, right, k)
    return parse_occ_symbol(sym), None


def _awaiting_keys_state():
    return {
        "schema_version": 1,
        "status": "awaiting_keys",
        "broker": "Alpaca Paper Trading (paper-api.alpaca.markets)",
        "mode": "paper_only",
        "host_locked": ALPACA_PAPER_HOST,
        "data_host": ALPACA_DATA_HOST,
        "live_host_blocked": list(ALPACA_LIVE_HOST_BLOCKLIST),
        "updated_ct": iso_ct(),
        "disclaimer": DISCLAIMER,
        "account": None,
        "positions": [],
        "orders": [],
        "note": "Set APCA_API_KEY_ID|ALPACA_API_KEY + APCA_API_SECRET_KEY|ALPACA_SECRET_KEY (env) or gitignored .env.alpaca, then run: python3 scripts/pod_engine.py alpaca-status",
        "cors_browser": {
            "paper_api": "Access-Control-Allow-Origin: * (browser REST OK with APCA headers)",
            "iex_ws": "wss://stream.data.alpaca.markets/v2/iex (WebSocket — no CORS; auth via JSON after open)",
        },
    }


def _sanitize_account(acct):
    if not acct:
        return None
    keep = ("id", "account_number", "status", "currency", "cash", "buying_power", "regt_buying_power",
            "daytrading_buying_power", "equity", "last_equity", "long_market_value", "short_market_value",
            "portfolio_value", "pattern_day_trader", "trading_blocked", "transfers_blocked", "account_blocked",
            "created_at", "multiplier", "initial_margin", "maintenance_margin", "sma", "daytrade_count")
    out = {k: acct.get(k) for k in keep if k in acct}
    # Mask account_number — keep last 4 only
    an = out.get("account_number")
    if isinstance(an, str) and len(an) > 4:
        out["account_number_masked"] = ("*" * (len(an) - 4)) + an[-4:]
        del out["account_number"]
    # Never persist raw id as a secret vector; keep a short fingerprint only
    aid = out.get("id")
    if isinstance(aid, str) and len(aid) > 8:
        out["account_id_fingerprint"] = aid[:4] + "…" + aid[-4:]
        del out["id"]
    return out


def _sanitize_position(p):
    keep = ("symbol", "asset_class", "qty", "qty_available", "avg_entry_price", "market_value",
            "cost_basis", "unrealized_pl", "unrealized_plpc", "current_price", "lastday_price",
            "change_today", "side")
    return {k: p.get(k) for k in keep if k in p}


def _sanitize_order(o):
    keep = ("id", "client_order_id", "created_at", "updated_at", "submitted_at", "filled_at",
            "expired_at", "canceled_at", "asset_class", "symbol", "qty", "filled_qty",
            "filled_avg_price", "order_class", "order_type", "type", "side", "time_in_force",
            "limit_price", "stop_price", "status", "extended_hours")
    return {k: o.get(k) for k in keep if k in o}


def alpaca_status(offline=False):
    """Fetch paper account state when keys exist; otherwise write awaiting_keys. Never uses live host."""
    key, sec = alpaca_creds()
    if not key or not sec or offline:
        st = _awaiting_keys_state()
        if offline:
            st["note"] = "offline mode — skipped network; keys " + ("present" if key and sec else "missing")
        wj(PAPER_STATE, st)
        return st
    # Live paper fetch (only when keys configured)
    acct = alpaca_request("GET", ALPACA_PAPER_HOST, "/v2/account")
    positions = alpaca_request("GET", ALPACA_PAPER_HOST, "/v2/positions") or []
    orders = alpaca_request("GET", ALPACA_PAPER_HOST, "/v2/orders", params={"status": "all", "limit": 20, "direction": "desc"}) or []
    st = {
        "schema_version": 1,
        "status": "connected",
        "broker": "Alpaca Paper Trading (paper-api.alpaca.markets)",
        "mode": "paper_only",
        "host_locked": ALPACA_PAPER_HOST,
        "data_host": ALPACA_DATA_HOST,
        "live_host_blocked": list(ALPACA_LIVE_HOST_BLOCKLIST),
        "updated_ct": iso_ct(),
        "disclaimer": DISCLAIMER,
        "account": _sanitize_account(acct),
        "positions": [_sanitize_position(p) for p in positions],
        "orders": [_sanitize_order(o) for o in orders],
        "equity": float(acct.get("equity") or 0) if acct else None,
        "buying_power": float(acct.get("buying_power") or 0) if acct else None,
        "cash": float(acct.get("cash") or 0) if acct else None,
    }
    wj(PAPER_STATE, st)
    return st


def alpaca_find_option(underlying, expiration, opt_type, strike, premium=None):
    """Resolve OCC symbol. Uses offline OCC math always; optionally enriches from data API if keys exist."""
    sym = occ_option_symbol(underlying, expiration, opt_type, strike)
    out = {
        "underlying": underlying.upper(),
        "expiration": expiration,
        "type": "put" if str(opt_type).lower().startswith("p") else "call",
        "strike": float(strike),
        "occ_symbol": sym,
        "source": "occ_offline",
        "host_locked": ALPACA_PAPER_HOST,
        "updated_ct": iso_ct(),
    }
    if premium is not None:
        out["target_premium"] = float(premium)
    key, sec = alpaca_creds()
    if key and sec:
        try:
            # Prefer contracts endpoint on paper host
            contracts = alpaca_request(
                "GET", ALPACA_PAPER_HOST, "/v2/options/contracts",
                params={
                    "underlying_symbols": underlying.upper(),
                    "expiration_date": expiration,
                    "type": out["type"],
                    "strike_price_gte": str(float(strike) - 0.01),
                    "strike_price_lte": str(float(strike) + 0.01),
                    "limit": 10,
                    "status": "active",
                },
            )
            # API may return {"option_contracts":[...]} or a list
            rows = contracts.get("option_contracts") if isinstance(contracts, dict) else contracts
            if rows:
                match = next((c for c in rows if abs(float(c.get("strike_price", 0)) - float(strike)) < 1e-6), rows[0])
                out["contract"] = {
                    "id": match.get("id"),
                    "symbol": match.get("symbol"),
                    "name": match.get("name"),
                    "status": match.get("status"),
                    "tradable": match.get("tradable"),
                    "expiration_date": match.get("expiration_date"),
                    "strike_price": match.get("strike_price"),
                    "type": match.get("type"),
                    "open_interest": match.get("open_interest"),
                }
                if match.get("symbol"):
                    out["occ_symbol"] = match["symbol"]
                    out["source"] = "alpaca_paper_contracts"
        except Exception as e:
            out["lookup_warning"] = str(e)[:240]
    return out


def _blotter_ticket(ticket_id):
    rows = rjl(BLOTTER)
    for t in rows:
        if t.get("ticket_id") == ticket_id:
            return t, rows
    return None, rows


def _firewall_ok(ticket):
    fw = ticket.get("firewall") or []
    if not fw:
        return False, "ticket missing firewall[]"
    if not all(c.get("pass") for c in fw):
        bad = [c for c in fw if not c.get("pass")]
        return False, f"firewall failed: {bad}"
    if ticket.get("verdict") not in ("APPROVED_FOR_STAGING", "APPROVED", None) and ticket.get("status") not in (
        "conditional_preapproved", "staged", "approved"
    ):
        # allow conditional_preapproved even if verdict field differs
        if ticket.get("status") != "conditional_preapproved":
            return False, f"verdict/status not approved: {ticket.get('verdict')}/{ticket.get('status')}"
    return True, "6/6"


def build_alpaca_paper_order(ticket, occ_symbol=None, expiration=None, strike=None):
    """Translate blotter ticket → Alpaca Paper /v2/orders payload. Hard-locked to paper host."""
    ok, why = _firewall_ok(ticket)
    if not ok:
        raise RuntimeError(f"Pre-Trade Risk Firewall blocked staging: {why}")
    instr = ticket.get("instrument") or "stock"
    qty = int(ticket.get("quantity") or 0)
    if qty < 1:
        raise RuntimeError("quantity < 1")
    entry = ticket.get("entry")
    stop = ticket.get("stop") or (ticket.get("oco_bracket") or {}).get("stop_order")
    t1 = ticket.get("target1") or (ticket.get("oco_bracket") or {}).get("take_profit_1")
    side = "buy" if (ticket.get("direction") or "long") == "long" else "sell"

    if instr.startswith("option"):
        # Fail closed (Nov 11 2026): the contract must come from explicit ticket fields
        # (occ_symbol, or option_expiration/expiry + strike). Never a default strike/expiry.
        spec, err = option_contract_spec(ticket, occ_symbol=occ_symbol, expiration=expiration, strike=strike)
        if err:
            raise RuntimeError(f"Option contract unresolved (fail closed): {err}")
        if entry is None or stop is None:
            raise RuntimeError("Option ticket missing entry/stop premium (fail closed)")
        occ = spec["occ_symbol"]
        # Alpaca options: simple limit (brackets not universally supported on options)
        order = {
            "symbol": occ,
            "qty": str(qty),
            "side": side,
            "type": "limit",
            "time_in_force": "day",
            "limit_price": f"{float(entry):.2f}",
            "extended_hours": False,
            "client_order_id": f"horsey-{ticket.get('ticket_id','ticket')}"[:48],
        }
        management = {
            "style": "manual_oco_after_fill",
            "note": "Alpaca paper options: submit long premium limit; manage stop/T1/T2 as child orders after fill (OCO).",
            "stop_price": float(stop) if stop is not None else None,
            "take_profit_1": float(t1) if t1 is not None else None,
            "take_profit_2": float(ticket.get("target2")) if ticket.get("target2") is not None else None,
            "scale_plan": (ticket.get("oco_bracket") or {}).get("scale_plan"),
            "time_stop": ticket.get("time_stop"),
            "underlying_trigger": ticket.get("underlying_trigger"),
            "hard_invalidation_underlying": ticket.get("hard_invalidation_underlying"),
        }
        asset_class = "us_option"
    else:
        # Equity bracket
        sym = (ticket.get("symbol") or "SPY").upper()
        if side == "buy":
            order = {
                "symbol": sym,
                "qty": str(qty),
                "side": "buy",
                "type": "limit",
                "time_in_force": "day",
                "limit_price": f"{float(entry):.2f}",
                "order_class": "bracket",
                "take_profit": {"limit_price": f"{float(t1):.2f}"},
                "stop_loss": {"stop_price": f"{float(stop):.2f}"},
                "client_order_id": f"horsey-{ticket.get('ticket_id','ticket')}"[:48],
            }
        else:
            # short stock bracket
            order = {
                "symbol": sym,
                "qty": str(qty),
                "side": "sell",
                "type": "limit",
                "time_in_force": "day",
                "limit_price": f"{float(entry):.2f}",
                "order_class": "bracket",
                "take_profit": {"limit_price": f"{float(t1):.2f}"},
                "stop_loss": {"stop_price": f"{float(stop):.2f}"},
                "client_order_id": f"horsey-{ticket.get('ticket_id','ticket')}"[:48],
            }
        management = {
            "style": "native_bracket",
            "stop_price": float(stop) if stop is not None else None,
            "take_profit_1": float(t1) if t1 is not None else None,
            "take_profit_2": float(ticket.get("target2")) if ticket.get("target2") is not None else None,
            "time_stop": ticket.get("time_stop"),
        }
        asset_class = "us_equity"
        occ = sym

    payload = {
        "host": ALPACA_PAPER_HOST,
        "endpoint": "/v2/orders",
        "method": "POST",
        "mode": "paper_only",
        "live_host_blocked": True,
        "ticket_id": ticket.get("ticket_id"),
        "setup_id": ticket.get("setup_id"),
        "asset_class": asset_class,
        "occ_symbol": occ if asset_class == "us_option" else None,
        "firewall_verified": why,
        "risk": {
            "equity_ref": ticket.get("equity"),
            "risk_pct_effective": ticket.get("risk_pct_effective"),
            "dollar_risk_budget": ticket.get("dollar_risk_budget"),
            "actual_dollar_risk": ticket.get("actual_dollar_risk"),
            "quantity": qty,
        },
        "order": order,
        "management_rules": management,
        "disclaimer": DISCLAIMER,
        "built_ct": iso_ct(),
    }
    return payload


def alpaca_stage_ticket(ticket_id, dry_run=True, submit_paper=False, expiration=None, strike=None, occ_symbol=None):
    ticket, rows = _blotter_ticket(ticket_id)
    if not ticket:
        raise SystemExit(f"ERROR: ticket {ticket_id} not found in {BLOTTER}")
    payload = build_alpaca_paper_order(ticket, occ_symbol=occ_symbol, expiration=expiration, strike=strike)
    # Persist dry-run artifact next to ticket (non-secret)
    ticket["alpaca_paper_payload"] = payload
    ticket["alpaca_dry_run_ct"] = iso_ct()
    ticket["alpaca_host_lock"] = ALPACA_PAPER_HOST

    if submit_paper and not dry_run:
        # Transmit ONLY to paper host after re-checking lock + firewall
        assert_paper_host(payload["host"])
        if payload["host"] != ALPACA_PAPER_HOST:
            raise RuntimeError("host lock mismatch")
        resp = alpaca_request("POST", ALPACA_PAPER_HOST, "/v2/orders", body=payload["order"])
        ticket["alpaca_order_id"] = resp.get("id") if isinstance(resp, dict) else None
        ticket["alpaca_status"] = resp.get("status") if isinstance(resp, dict) else "submitted"
        ticket["alpaca_submitted_ct"] = iso_ct()
        ticket["status"] = "submitted_paper"
        ajl(PAPER_ORDERS, {"ticket_id": ticket_id, "submitted_ct": iso_ct(), "order_id": ticket.get("alpaca_order_id"),
                           "status": ticket.get("alpaca_status"), "symbol": payload["order"].get("symbol")})
        try:
            alpaca_status()
        except Exception as e:
            ticket["alpaca_sync_warning"] = str(e)[:200]
        result = {"mode": "submitted_paper", "payload": payload, "response": _sanitize_order(resp) if isinstance(resp, dict) else resp}
    else:
        ticket["alpaca_status"] = "dry_run_validated"
        result = {"mode": "dry_run", "payload": payload, "note": "Validated locally. Not transmitted. Re-run with --submit-paper after keys + human confirm."}

    # Rewrite blotter with updated ticket
    with open(BLOTTER, "w") as f:
        for t in rows:
            if t.get("ticket_id") == ticket_id:
                f.write(json.dumps(ticket) + "\n")
            else:
                f.write(json.dumps(t) + "\n")
    # Also stash latest dry-run under pod/
    wj(P("pod", "alpaca_last_dry_run.json"), result)
    return result





def _gov():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_pod_governance", P("scripts", "_pod_governance.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


CEO_DIR_REL = ("pod", "ceo_decisions")


def _ceo_pull_safely(timeout=10):
    """Best-effort git pull (under repo lock); never fatal."""
    import subprocess
    try:
        with repo_sync_lock(label="ceo-push pull", timeout_sec=30):
            r = subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], cwd=ROOT,
                               capture_output=True, text=True, timeout=timeout)
            return {"pulled": r.returncode == 0, "detail": (r.stdout or r.stderr or "").strip()[:160]}
    except Exception as e:
        return {"pulled": False, "detail": str(e)[:160]}


def check_ceo_push(pull=True):
    out = {}
    if pull:
        out["pull"] = _ceo_pull_safely()
    latest = rj(P(*CEO_DIR_REL, "latest_decisions.json"), {}) or {}
    last = rj(P(*CEO_DIR_REL, ".last_processed.json"), {}) or {}
    lsub = latest.get("submitted_ct"); psub = last.get("last_submitted_ct")
    def _ts(x):
        try:
            return datetime.strptime(x, "%Y-%m-%dT%H:%M:%S%z")
        except Exception:
            return None
    unp = bool(lsub) and (not psub or (_ts(lsub) and _ts(psub) and _ts(lsub) > _ts(psub)) or (lsub != psub and not (_ts(lsub) and _ts(psub))))
    out.update({"unprocessed_ceo_push": bool(unp), "last_processed_submitted_ct": psub, "latest_submitted_ct": lsub,
                "ceo_push_summary": ({k: latest.get(k) for k in ("submitted_ct", "prompt_set_id", "risk_pct", "ceo_note")} if unp else {})})
    return out


def mark_ceo_push_processed(submitted_ct=None, handled_by="cli_mark_ceo_push_processed"):
    latest = rj(P(*CEO_DIR_REL, "latest_decisions.json"), {}) or {}
    sub = submitted_ct or latest.get("submitted_ct")
    if not sub:
        raise SystemExit("no submitted_ct available")
    rec = {"last_submitted_ct": sub, "prompt_set_id": latest.get("prompt_set_id") if sub == latest.get("submitted_ct") else None,
           "processed_ct": iso_ct(), "handled_by": handled_by}
    wj(P(*CEO_DIR_REL, ".last_processed.json"), rec)
    return rec


def maybe_rebuild_vault_state():
    """If CEO vault is active, refresh vault/state.enc.json (never fatal)."""
    try:
        import subprocess
        vault_py = P("scripts", "vault_crypto.py")
        env_vault = P(".env.vault")
        meta = P("vault", "vault_meta.json")
        if not os.path.exists(vault_py):
            return
        if not (os.environ.get("HORSEY_VAULT_PASSWORD") or os.path.exists(env_vault) or os.path.exists(meta)):
            return
        # Only rebuild if password actually resolvable (vault_crypto skips cleanly otherwise)
        subprocess.run([sys.executable, vault_py, "build-state"], cwd=ROOT, check=False, timeout=120,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"vault build-state skipped: {e}", file=sys.stderr)


# ---------------------------------------------------------------- Robinhood Income Sleeve (read-only)
INCOME_SLEEVE_DIR = P("pod", "income_sleeve")
INCOME_SLEEVE_HOLDINGS = P("pod", "income_sleeve", "holdings_snapshot.json")

_INCOME_SLEEVE_INVARIANTS = {
    "mode": "monitoring_and_recommendation_only",
    "automated_trading": False,
}


def _income_sleeve_default():
    return {
        "updated_at": iso_ct(),
        "updated_at_ct": iso_ct(),
        "status": "awaiting_holdings_snapshot",
        "data_source_plan": (
            "Manual CSV / holdings snapshot now; read-only Robinhood MCP connector "
            "[plugin 68492778] once Agentic/read access is connected"
        ),
        "mode": "monitoring_and_recommendation_only",
        "automated_trading": False,
        "purpose": (
            "Covered-call ETFs & dividend vehicles funded by trading proceeds to generate "
            "income and pay down student debt"
        ),
        "positions": [],
        "options_overlay": [],
        "earmarked_trading_proceeds_usd": 0.0,
        "missing_inputs_needed": [
            "Current Robinhood share/ETF tickers, share counts, cost basis, and trailing distribution yields",
            "Any active short covered-call legs (strike, expiration, premium collected)",
            "Optional target monthly student-debt payment amount for cash-flow coverage tracking",
        ],
    }


def _enforce_income_sleeve_invariants(doc: dict) -> dict:
    doc = dict(doc or {})
    doc["mode"] = "monitoring_and_recommendation_only"
    doc["automated_trading"] = False
    if "updated_at_ct" not in doc and "updated_at" in doc:
        doc["updated_at_ct"] = doc["updated_at"]
    return doc


def load_income_sleeve_holdings() -> dict:
    doc = rj(INCOME_SLEEVE_HOLDINGS, None)
    if not isinstance(doc, dict):
        doc = _income_sleeve_default()
        os.makedirs(INCOME_SLEEVE_DIR, exist_ok=True)
        wj(INCOME_SLEEVE_HOLDINGS, _enforce_income_sleeve_invariants(doc))
    return _enforce_income_sleeve_invariants(doc)


def income_sleeve_status(import_csv: str | None = None) -> dict:
    """Read-only status summary; optionally import a CSV first (still recommendation-only)."""
    if import_csv:
        import_income_sleeve_csv(import_csv)
    doc = load_income_sleeve_holdings()
    # Non-mutating glance at paper earmarks if present
    paper = rj(PAPER_STATE, {}) or {}
    earmarked = float(doc.get("earmarked_trading_proceeds_usd") or 0.0)
    summary = {
        "status": doc.get("status"),
        "mode": doc.get("mode"),
        "automated_trading": bool(doc.get("automated_trading")),
        "positions_count": len(doc.get("positions") or []),
        "options_overlay_count": len(doc.get("options_overlay") or []),
        "earmarked_trading_proceeds_usd": earmarked,
        "missing_inputs_needed": doc.get("missing_inputs_needed") or [],
        "updated_at": doc.get("updated_at") or doc.get("updated_at_ct"),
        "purpose": doc.get("purpose"),
        "paper_account_status": paper.get("status"),
        "data_source_plan": doc.get("data_source_plan"),
    }
    return summary


def _find_csv_col(cols, candidates):
    lower = {c.lower().strip(): c for c in cols}
    for name in candidates:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _parse_occ_desc(text: str):
    """Parse OCC or Robinhood-style option description into overlay fields."""
    import re
    if not text:
        return None
    s = str(text).strip()
    m = re.match(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$", s.replace(" ", "").upper())
    if m:
        from datetime import datetime as _dt
        und, ymd, cp, strike = m.group(1), m.group(2), m.group(3), int(m.group(4)) / 1000.0
        try:
            exp = _dt.strptime(ymd, "%y%m%d").date().isoformat()
        except Exception:
            exp = ymd
        return {
            "underlying": und,
            "expiration": exp,
            "option_type": "call" if cp == "C" else "put",
            "strike": strike,
            "occ_symbol": f"{und}{ymd}{cp}{m.group(4)}",
            "side": "short",
            "quantity": None,
            "premium_collected": None,
        }
    # e.g. "SPY 11/06/2026 Call $733.00" or "JEPI 01/16/2026 Call 55.0"
    m2 = re.search(
        r"([A-Z]{1,6}).*?(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}).*?\b(Call|Put)\b.*?\$?(\d+(?:\.\d+)?)",
        s, re.I,
    )
    if m2:
        from datetime import datetime as _dt
        und = m2.group(1).upper()
        raw_exp = m2.group(2).replace("-", "/")
        for fmt in ("%m/%d/%Y", "%m/%d/%y"):
            try:
                exp = _dt.strptime(raw_exp, fmt).date().isoformat()
                break
            except Exception:
                exp = raw_exp
        return {
            "underlying": und,
            "expiration": exp,
            "option_type": m2.group(3).lower(),
            "strike": float(m2.group(4)),
            "occ_symbol": None,
            "side": "short",
            "quantity": None,
            "premium_collected": None,
        }
    return None


def import_income_sleeve_csv(csv_path: str) -> dict:
    """Parse Robinhood activity/holdings CSV read-only; never stages orders."""
    import csv as _csv
    import re
    p = csv_path if hasattr(csv_path, "exists") else __import__("pathlib").Path(csv_path)
    if not p.exists():
        raise SystemExit(f"income-sleeve CSV not found: {csv_path}")
    with p.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = _csv.Sniffer().sniff(sample, delimiters=",\t")
        except Exception:
            dialect = _csv.excel
        reader = _csv.DictReader(f, dialect=dialect)
        rows = list(reader)
        cols = reader.fieldnames or []

    sym_col = _find_csv_col(cols, ["Symbol", "Ticker", "Instrument", "symbol", "chain_symbol"])
    qty_col = _find_csv_col(cols, ["Shares", "Quantity", "Qty", "Share Count", "processed_quantity"])
    cost_col = _find_csv_col(cols, ["Avg Cost", "Average Cost", "Cost Basis", "Average Price", "Price", "cost_basis"])
    yield_col = _find_csv_col(cols, ["Yield", "Trailing Yield", "Distribution Yield", "Div Yield"])
    action_col = _find_csv_col(cols, ["Trans Code", "Side", "Action", "action", "direction"])
    amount_col = _find_csv_col(cols, ["Amount", "amount", "Notional"])
    exp_col = _find_csv_col(cols, ["Expiration", "expiration", "expiration_date", "exp_date"])
    strike_col = _find_csv_col(cols, ["Strike", "strike", "strike_price"])
    opt_col = _find_csv_col(cols, ["Option Type", "option_type", "type", "put_call"])

    positions = {}
    overlay = []
    for row in rows:
        raw_sym = (row.get(sym_col) or "").strip() if sym_col else ""
        if not raw_sym:
            continue
        action = (row.get(action_col) or "").strip().upper() if action_col else ""
        qty_raw = (row.get(qty_col) or "").replace(",", "").strip() if qty_col else ""
        try:
            qty = float(qty_raw) if qty_raw else 0.0
        except Exception:
            qty = 0.0
        # Option row?
        occ = _parse_occ_desc(raw_sym)
        looks_option = bool(occ) or bool(opt_col and (row.get(opt_col) or "").strip()) or bool(strike_col and (row.get(strike_col) or "").strip())
        if looks_option:
            leg = occ or {
                "underlying": re.sub(r"[^A-Z].*", "", raw_sym.upper()) or raw_sym.upper(),
                "expiration": (row.get(exp_col) or "").strip() if exp_col else None,
                "option_type": ((row.get(opt_col) or "call").strip().lower()) if opt_col else "call",
                "strike": float(str(row.get(strike_col)).replace("$", "").replace(",", "") or 0) if strike_col and row.get(strike_col) else None,
                "occ_symbol": raw_sym if re.match(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$", raw_sym.replace(" ", "").upper()) else None,
                "side": "short",
                "quantity": None,
                "premium_collected": None,
            }
            # Short opens: STO / SELL (covered call)
            if action in ("STO", "SELL", "SSC") or not action:
                leg["side"] = "short"
            elif action in ("BTC", "BUY", "BTO"):
                leg["side"] = "long"
            if qty:
                leg["quantity"] = abs(qty)
            if amount_col and row.get(amount_col):
                try:
                    leg["premium_collected"] = abs(float(str(row.get(amount_col)).replace("$", "").replace(",", "")))
                except Exception:
                    pass
            # Only retain short call overlays for the income sleeve watch
            if (leg.get("option_type") or "call").startswith("c") and leg.get("side") == "short":
                overlay.append(leg)
            continue

        # Equity / ETF holdings
        ticker = raw_sym.upper().split()[0]
        if ticker in ("USD", "CASH"):
            continue
        cost = None
        if cost_col and row.get(cost_col):
            try:
                cost = float(str(row.get(cost_col)).replace("$", "").replace(",", ""))
            except Exception:
                cost = None
        yld = None
        if yield_col and row.get(yield_col):
            try:
                yld = float(str(row.get(yield_col)).replace("%", "").replace(",", ""))
            except Exception:
                yld = None
        # Activity CSVs: net BUY/SELL into a running qty; holdings CSVs: absolute shares
        signed = qty
        if action in ("SELL", "STC", "STO"):
            signed = -abs(qty)
        elif action in ("BUY", "BTO", "BTC"):
            signed = abs(qty)
        elif not action:
            signed = abs(qty)
        pos = positions.setdefault(ticker, {"ticker": ticker, "shares": 0.0, "avg_cost": cost, "trailing_yield_pct": yld})
        if action:
            pos["shares"] = float(pos.get("shares") or 0.0) + signed
        else:
            pos["shares"] = float(signed or pos.get("shares") or 0.0)
        if cost is not None:
            pos["avg_cost"] = cost
        if yld is not None:
            pos["trailing_yield_pct"] = yld

    pos_list = [p for p in positions.values() if abs(float(p.get("shares") or 0.0)) > 1e-9]
    for p in pos_list:
        p["shares"] = round(float(p["shares"]), 6)

    doc = load_income_sleeve_holdings()
    doc["positions"] = pos_list
    doc["options_overlay"] = overlay
    doc["updated_at"] = iso_ct()
    doc["updated_at_ct"] = doc["updated_at"]
    doc["status"] = "holdings_loaded" if pos_list or overlay else "awaiting_holdings_snapshot"
    if pos_list or overlay:
        doc["missing_inputs_needed"] = [
            x for x in (doc.get("missing_inputs_needed") or [])
            if "tickers" not in x.lower() and "covered-call" not in x.lower()
        ]
        if not any("student-debt" in x.lower() for x in doc["missing_inputs_needed"]):
            doc["missing_inputs_needed"].append(
                "Optional target monthly student-debt payment amount for cash-flow coverage tracking"
            )
    doc["data_source_plan"] = f"Imported read-only CSV ({p.name}); monitoring & recommendation only"
    doc = _enforce_income_sleeve_invariants(doc)
    os.makedirs(INCOME_SLEEVE_DIR, exist_ok=True)
    wj(INCOME_SLEEVE_HOLDINGS, doc)
    return doc



def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("status")
    t = sp.add_parser("size-ticket")
    t.add_argument("--symbol", required=True); t.add_argument("--strategy-agent", default="cio")
    t.add_argument("--direction", choices=["long", "short"], default="long")
    t.add_argument("--instrument", choices=["stock", "option_call", "option_put"], default="stock")
    t.add_argument("--equity", type=float, required=True); t.add_argument("--risk-pct", type=float, default=0.5)
    t.add_argument("--override-cap", action="store_true"); t.add_argument("--drawdown-mode", action="store_true")
    t.add_argument("--entry", type=float, required=True); t.add_argument("--stop", type=float)
    t.add_argument("--target1", type=float); t.add_argument("--target2", type=float)
    t.add_argument("--min-rr", type=float); t.add_argument("--time-stop"); t.add_argument("--underlying", type=float)
    t.add_argument("--stage-paper", action="store_true"); t.add_argument("--json", action="store_true")
    s = sp.add_parser("log-signal")
    s.add_argument("--agent-id", required=True); s.add_argument("--symbol", default="SPY")
    s.add_argument("--direction", choices=["long", "short", "neutral"], default="neutral")
    s.add_argument("--entry", type=float); s.add_argument("--stop", type=float); s.add_argument("--target", type=float)
    s.add_argument("--probability", type=float, default=0.5); s.add_argument("--horizon-hours", type=float, default=24)
    s.add_argument("--stance"); s.add_argument("--note"); s.add_argument("--tag", default="PAPER"); s.add_argument("--as-of")
    g = sp.add_parser("grade"); g.add_argument("--now")
    sp.add_parser("build-memos-index")
    sp.add_parser("index-memos")  # alias of build-memos-index
    sp.add_parser("bridge-ledger")
    iss = sp.add_parser("income-sleeve-status", help="Read-only Robinhood Income Sleeve status summary")
    iss.add_argument("--import-csv", dest="import_csv", help="Optional read-only holdings/activity CSV to ingest first")
    isc = sp.add_parser("income-sleeve-import-csv", help="Import Robinhood holdings/activity CSV (read-only; never trades)")
    isc.add_argument("csv_path")
    isc2 = sp.add_parser("import-income-sleeve-csv", help="Alias of income-sleeve-import-csv")
    isc2.add_argument("csv_path")
    pq = sp.add_parser("post-questions"); pq.add_argument("file")
    # Alpaca Paper (hard-locked to paper-api.alpaca.markets)
    asp = sp.add_parser("alpaca-status", help="Refresh sanitized Alpaca Paper account state")
    asp.add_argument("--offline", action="store_true", help="Skip network; write awaiting_keys / offline note")
    sp.add_parser("alpaca-sync", help="Alias of alpaca-status")
    afo = sp.add_parser("alpaca-find-option")
    afo.add_argument("--underlying", default="SPY")
    afo.add_argument("--expiration", required=True)
    afo.add_argument("--type", dest="opt_type", choices=["put", "call"], required=True)
    afo.add_argument("--strike", type=float, required=True)
    afo.add_argument("--premium", type=float)
    astg = sp.add_parser("alpaca-stage-ticket")
    astg.add_argument("--ticket-id", required=True)
    astg.add_argument("--dry-run", action="store_true", default=True)
    astg.add_argument("--submit-paper", action="store_true", help="Transmit to paper-api.alpaca.markets ONLY")
    astg.add_argument("--expiration")
    astg.add_argument("--strike", type=float)
    astg.add_argument("--occ-symbol")

    art = sp.add_parser("alpaca-rth-tick", help="RTH auto-exec tick for conditional_preapproved Alpaca Paper tickets")
    art.add_argument("--force-sim", action="store_true",
                     help="Dry evaluate triggers; NEVER submit paper/live orders unless real RTH conditions already hold (still never transmits when force-sim)")
    art.add_argument("--push-if-changed", action="store_true",
                     help="Rebuild vault/state.enc.json and git push when execution state changes")
    art.add_argument("--ensure-watcher", action="store_true",
                     help="Spawn alpaca_rth_watcher.py detached if RTH window and lock free")

    ct = sp.add_parser("close-ticket")
    ct.add_argument("--ticket-id", required=True)
    ct.add_argument("--exit-price", type=float, required=True)
    ct.add_argument("--opened-ct"); ct.add_argument("--closed-ct")
    ct.add_argument("--exit-reason", default="manual_close",
                    choices=["target1_target2","stop_hit","invalidation_hit","time_stop","eod_0dte_flat","manual_close"])
    ct.add_argument("--primary-driver", choices=["delta","gamma","vega","timing_execution","thesis_wrong","pending_fill"])
    ct.add_argument("--lesson"); ct.add_argument("--apply-learning", action="store_true")
    cr = sp.add_parser("complete-review")
    cr.add_argument("--ticket-id", required=True)
    cr.add_argument("--primary-driver", required=True, choices=["delta","gamma","vega","timing_execution","thesis_wrong","pending_fill"])
    cr.add_argument("--lesson", required=True)
    cr.add_argument("--weight-delta", type=float)
    cr.add_argument("--regime-tag", choices=["short_gamma_backwardation","positive_gamma_contango","below_200sma"])
    sp.add_parser("portfolio-risk")
    sp.add_parser("check-limits")
    ccp = sp.add_parser("check-ceo-push", help="Is there an unprocessed CEO decision push? (read-only)")
    ccp.add_argument("--no-pull", action="store_true"); ccp.add_argument("--pull", action="store_true")
    mcp = sp.add_parser("mark-ceo-push-processed"); mcp.add_argument("--submitted-ct")
    sp.add_parser("track-record")
    sp.add_parser("build-reviews-index")
    rd = sp.add_parser("record-decision")
    rd.add_argument("--kill-switch-action", choices=["reset_clear","raise_daily_1_5","raise_weekly_3_0","raise_open_1_5"])
    rd.add_argument("--note", default="")
    rd.add_argument("file", nargs="?")
    t.add_argument("--auto-reduce", action="store_true", default=True)
    t.add_argument("--no-auto-reduce", action="store_true")
    t.add_argument("--strict-block", action="store_true")

    a = ap.parse_args(argv)
    if a.cmd in ("status", "log-signal", "grade"):
        _housekeeping()
    if a.cmd in ("build-memos-index", "index-memos"):
        idx = build_memos_index()
        print(f"memos_index.json: {idx['count']} memo(s) — " + ", ".join(f"{r['role']}={r['count']}" for r in idx["roles"]))
        maybe_rebuild_vault_state()
    elif a.cmd == "income-sleeve-status":
        summary = income_sleeve_status(getattr(a, "import_csv", None))
        print(json.dumps(summary, indent=2))
        maybe_rebuild_vault_state()
    elif a.cmd in ("income-sleeve-import-csv", "import-income-sleeve-csv"):
        doc = import_income_sleeve_csv(a.csv_path)
        print(json.dumps({
            "ok": True,
            "status": doc.get("status"),
            "positions_count": len(doc.get("positions") or []),
            "options_overlay_count": len(doc.get("options_overlay") or []),
            "earmarked_trading_proceeds_usd": doc.get("earmarked_trading_proceeds_usd"),
            "automated_trading": doc.get("automated_trading"),
            "mode": doc.get("mode"),
            "updated_at": doc.get("updated_at"),
        }, indent=2))
        maybe_rebuild_vault_state()
    elif a.cmd == "post-questions":
        q = post_questions(a.file); print(f"posted {len(q['questions'])} question(s) as {q['prompt_set_id']}")
        maybe_rebuild_vault_state()
    elif a.cmd == "bridge-ledger":
        print(f"bridged {bridge_specialist_ledger()} specialist entr(ies)")
        maybe_rebuild_vault_state()
    elif a.cmd == "status":
        print(status())
    elif a.cmd == "size-ticket":
        if getattr(a, "no_auto_reduce", False):
            a.auto_reduce = False
        tk = size_ticket(a)
        if a.stage_paper and tk["verdict"] == "APPROVED_FOR_STAGING":
            ajl(BLOTTER, tk); tk["staged"] = True
        if a.json:
            print(json.dumps(tk, indent=2))
        else:
            print(ticket_md(tk))
            print()
            print(json.dumps(tk, indent=2))
        return 0 if tk["verdict"] == "APPROVED_FOR_STAGING" else 2
    elif a.cmd == "log-signal":
        print(json.dumps(log_signal(a), indent=2)); maybe_rebuild_vault_state()
    elif a.cmd == "grade":
        done, sc = grade(parse_ts(a.now) if a.now else None)
        print(f"Graded {len(done)} signal(s). Active regime: {sc.get('active_regime_tags')}")
        for d in done:
            print(f" • {d['signal_id']} {d.get('agent_name')}: {d['grade_status']} R={d.get('realized_r')} brier={d.get('brier_error')}")
        print(json.dumps({k: {x: v.get(x) for x in ('graded_count', 'win_rate', 'avg_realized_r', 'brier_score', 'current_effective_weight')}
                          for k, v in sc.get("agents", {}).items()}, indent=2))
        maybe_rebuild_vault_state()
    elif a.cmd in ("alpaca-status", "alpaca-sync"):
        offline = bool(getattr(a, "offline", False))
        st = alpaca_status(offline=offline)
        print(json.dumps(st, indent=2))
        print()
        print("→ wrote %s · status=%s · mode=%s" % (PAPER_STATE, st.get("status"), st.get("mode")))
    elif a.cmd == "alpaca-find-option":
        print(json.dumps(alpaca_find_option(a.underlying, a.expiration, a.opt_type, a.strike, a.premium), indent=2))
    elif a.cmd == "alpaca-stage-ticket":
        # Default dry-run; --submit-paper opts into paper transmission
        dry = not a.submit_paper
        if a.submit_paper:
            print("WARNING: submitting to PAPER host only:", ALPACA_PAPER_HOST, file=sys.stderr)
        result = alpaca_stage_ticket(
            a.ticket_id, dry_run=dry, submit_paper=a.submit_paper,
            expiration=a.expiration, strike=a.strike, occ_symbol=a.occ_symbol,
        )
        print(json.dumps(result, indent=2)); maybe_rebuild_vault_state()
        return 0
    elif a.cmd == "alpaca-rth-tick":
        import importlib.util
        _spec = importlib.util.spec_from_file_location("_alpaca_rth", P("scripts", "_alpaca_rth.py"))
        rth = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(rth)
        pe = sys.modules[__name__]
        result = rth.alpaca_rth_tick(
            pe,
            force_sim=bool(getattr(a, "force_sim", False)),
            push_if_changed=bool(getattr(a, "push_if_changed", False)),
            ensure_watcher_flag=bool(getattr(a, "ensure_watcher", False)),
        )
        try:
            _stamp = "/tmp/horsey_ceo_pull.stamp"; _due = (not os.path.exists(_stamp)) or (_time.time() - os.path.getmtime(_stamp) > 120)
            if _due:
                open(_stamp, "w").close()
            cp = check_ceo_push(pull=_due)  # throttled pull (≤1 per 2 min); never marks .last_processed.json
            if isinstance(result, dict):
                result["unprocessed_ceo_push"] = cp["unprocessed_ceo_push"]
                result["ceo_push_summary"] = cp["ceo_push_summary"]
        except Exception as e:
            if isinstance(result, dict):
                result["unprocessed_ceo_push"] = False; result["ceo_push_error"] = str(e)[:160]
        print(json.dumps(result, indent=2))
        return 0
    elif a.cmd == "check-ceo-push":
        print(json.dumps(check_ceo_push(pull=not a.no_pull), indent=2))
        return 0
    elif a.cmd == "mark-ceo-push-processed":
        print(json.dumps(mark_ceo_push_processed(a.submitted_ct), indent=2))
        return 0

    elif a.cmd == "close-ticket":
        g = _gov()
        rev = g.close_ticket(a.ticket_id, a.exit_price, a.opened_ct, a.closed_ct, a.exit_reason,
                             a.primary_driver, a.lesson, a.apply_learning)
        print(json.dumps(rev, indent=2)); maybe_rebuild_vault_state(); return 0
    elif a.cmd == "complete-review":
        g = _gov(); rev = g.complete_review(a.ticket_id, a.primary_driver, a.lesson, a.weight_delta, a.regime_tag)
        print(json.dumps(rev, indent=2)); maybe_rebuild_vault_state(); return 0
    elif a.cmd == "portfolio-risk":
        print(json.dumps(_gov().compute_portfolio_snapshot(), indent=2)); maybe_rebuild_vault_state(); return 0
    elif a.cmd == "check-limits":
        print(json.dumps(_gov().check_limits(), indent=2)); maybe_rebuild_vault_state(); return 0
    elif a.cmd == "track-record":
        g = _gov(); g.ensure_armed_queue(); print(json.dumps(g.rebuild_weekly_summary(), indent=2)); maybe_rebuild_vault_state(); return 0
    elif a.cmd == "build-reviews-index":
        print(json.dumps(_gov().build_reviews_index(), indent=2)); maybe_rebuild_vault_state(); return 0
    elif a.cmd == "record-decision":
        g = _gov()
        if getattr(a, "kill_switch_action", None):
            print(json.dumps(g.record_kill_switch_decision(a.kill_switch_action, a.note or ""), indent=2))
        elif getattr(a, "file", None):
            print(json.dumps({"ok": True, "file": a.file}, indent=2))
        else:
            raise SystemExit("record-decision requires --kill-switch-action or file")
        maybe_rebuild_vault_state(); return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

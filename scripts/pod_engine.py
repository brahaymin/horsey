#!/usr/bin/env python3
"""pod_engine.py — Shared state bus, Pre-Trade Risk Firewall & Bracket Order Sizer,
and Self-Training Quant Auditor for the horsey multi-agent pod.

Paper / architecture demo — NOT live financial advice. Zero paid dependencies.

Subcommands:
  status       Executive CIO brief (Central Time)
  size-ticket  Deterministic 6-point 50-Mistakes firewall + bracket sizer
  log-signal   Validate & record a specialist signal
  grade        Grade elapsed signals vs history/timeseries.jsonl, update pod/scorecard.json
"""
import argparse, json, math, os, sys, uuid
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
]
ROLE_ORDER = ["cio", "ptj", "intraday", "buffett", "gamma_data"]


def _memo_meta(rel, role, label, agent):
    import re
    full = P(rel)
    base = os.path.basename(rel)
    m = re.match(r"(\d{4}-\d{2}-\d{2})[_-](\d{2})(\d{2})-ct-?([a-z0-9-]*?)(?:_thesis|_cio_briefing)?\.md$", base)
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


def build_memos_index():
    import glob
    items = []
    for d, suf, role, label, agent in MEMO_SOURCES:
        for f in glob.glob(P(d, "*" + suf)):
            items.append(_memo_meta(os.path.relpath(f, ROOT).replace(os.sep, "/"), role, label, agent))
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
    wj(P("pod", "memos_index.json"), idx)
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
    sp.add_parser("bridge-ledger")
    pq = sp.add_parser("post-questions"); pq.add_argument("file")
    a = ap.parse_args(argv)
    if a.cmd in ("status", "log-signal", "grade"):
        _housekeeping()
    if a.cmd == "build-memos-index":
        idx = build_memos_index()
        print(f"memos_index.json: {idx['count']} memo(s) — " + ", ".join(f"{r['role']}={r['count']}" for r in idx["roles"]))
    elif a.cmd == "post-questions":
        q = post_questions(a.file); print(f"posted {len(q['questions'])} question(s) as {q['prompt_set_id']}")
    elif a.cmd == "bridge-ledger":
        print(f"bridged {bridge_specialist_ledger()} specialist entr(ies)")
    elif a.cmd == "status":
        print(status())
    elif a.cmd == "size-ticket":
        tk = size_ticket(a)
        if a.stage_paper and tk["verdict"] == "APPROVED_FOR_STAGING":
            ajl(BLOTTER, tk); tk["staged"] = True
        print(json.dumps(tk, indent=2) if a.json else ticket_md(tk) + "\n\n" + json.dumps(tk, indent=2))
        return 0 if tk["verdict"] == "APPROVED_FOR_STAGING" else 2
    elif a.cmd == "log-signal":
        print(json.dumps(log_signal(a), indent=2))
    elif a.cmd == "grade":
        done, sc = grade(parse_ts(a.now) if a.now else None)
        print(f"Graded {len(done)} signal(s). Active regime: {sc.get('active_regime_tags')}")
        for d in done:
            print(f" • {d['signal_id']} {d.get('agent_name')}: {d['grade_status']} R={d.get('realized_r')} brier={d.get('brier_error')}")
        print(json.dumps({k: {x: v.get(x) for x in ('graded_count', 'win_rate', 'avg_realized_r', 'brier_score', 'current_effective_weight')}
                          for k, v in sc.get("agents", {}).items()}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
    L = [f"=== EXECUTIVE CIO BRIEF — {now_ct().strftime('%a %b %d, %Y %I:%M %p')} CT ===", f"({DISCLAIMER})",
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
    a = ap.parse_args(argv)
    if a.cmd == "status":
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

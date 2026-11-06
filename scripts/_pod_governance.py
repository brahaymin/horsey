#!/usr/bin/env python3
"""CIO-owned deterministic engines: post-trade reviews, portfolio risk, track record, kill switches.

Owned by CIO 7ad52fe7-3d09-4438-bfa9-552a13aef8b6. Zero new sidebar agents.
Paper / Architecture Demo — Not Live Financial Advice.
"""
from __future__ import annotations

import json
import math
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover
    CT = timezone(timedelta(hours=-6))

ROOT = Path(os.environ.get("HORSEY_ROOT", Path(__file__).resolve().parents[1]))
POD = ROOT / "pod"
REVIEWS = POD / "reviews"
RISK = POD / "risk"
TRACK = POD / "track_record"
BLOTTER = POD / "ledger" / "paper_blotter.jsonl"
SCORE = POD / "scorecard.json"
ROSTER = POD / "roster.json"
STATE = POD / "signals" / "latest_pod_state.json"
PAPER_STATE = POD / "paper_account_state.json"
LATEST = ROOT / "latest.json"
LIMITS = RISK / "limits.json"
PORTFOLIO = RISK / "portfolio_snapshot.json"
REVIEWS_INDEX = REVIEWS / "reviews_index.json"
SCHEMA = REVIEWS / "schema.json"
TEMPLATE = REVIEWS / "TEMPLATE.md"
RULES = TRACK / "rules_registry.json"
TRADES = TRACK / "trades.jsonl"
ARMED_Q = TRACK / "armed_rules_queue.json"
WEEKLY_JSON = TRACK / "weekly_summary.json"
WEEKLY_MD = TRACK / "weekly_summary.md"
CEO_Q = POD / "ceo_questions.json"
CEO_D = POD / "ceo_decisions" / "latest_decisions.json"
CIO_DIR = POD / "cio_briefings"
MEMOS_IDX = POD / "memos_index.json"

CIO_ID = "7ad52fe7-3d09-4438-bfa9-552a13aef8b6"
ITT_ID = "ee33b632-6c42-4d43-bf17-665bc939db17"
PTJ_ID = "05895e3e-1c82-4cec-9ff3-07aa73b5c0db"
BUFFETT_ID = "e44fa805-3796-4f84-8366-30901208f4ab"
DISCLAIMER = "Paper / Architecture Demo — Not Live Financial Advice"

EXIT_MAP = {
    "STOP_EXIT": "stop_hit",
    "TARGET2_CLOSED": "target1_target2",
    "TARGET1_SCALED": "target1_target2",
    "TIME_STOP_EXIT": "time_stop",
    "EOD_0DTE_FLAT": "eod_0dte_flat",
    "INVALIDATED": "invalidation_hit",
}


def now_ct() -> datetime:
    return datetime.now(CT)


def iso_ct(dt: Optional[datetime] = None) -> str:
    return (dt or now_ct()).astimezone(CT).strftime("%Y-%m-%dT%H:%M:%S%z")


def rj(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def wj(path: Path, obj: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def rjl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def ajl(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def rewrite_blotter(mutator) -> List[dict]:
    rows = rjl(BLOTTER)
    new_rows = [mutator(dict(t)) for t in rows]
    with BLOTTER.open("w", encoding="utf-8") as f:
        for t in new_rows:
            if t is None:
                continue
            f.write(json.dumps(t) + "\n")
    return [t for t in new_rows if t is not None]


def blotter_ticket(ticket_id: str) -> Optional[dict]:
    for t in rjl(BLOTTER):
        if t.get("ticket_id") == ticket_id:
            return t
    return None


def parse_ct(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = str(s).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # handle -0600 without colon
        if re.search(r"[+-]\d{4}$", s):
            s = s[:-2] + ":" + s[-2:]
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CT)
        return dt.astimezone(CT)
    except Exception:
        return None


def market_snapshot() -> dict:
    j = rj(LATEST, {}) or {}
    s = j.get("summary") or {}
    spy = s.get("spy_price") or s.get("spy_close") or j.get("spy_price") or 733.0
    vix = s.get("vix") or j.get("vix") or 20.0
    sma = s.get("spy_sma_200") or 730.15
    gex = s.get("net_gex_45d") or j.get("net_gex_45d")
    return {"spy": float(spy), "vix": float(vix), "sma200": float(sma), "net_gex_45d": gex}


def specialist_name(sid: str) -> str:
    rost = rj(ROSTER, {}) or {}
    for a in rost.get("agents") or []:
        if a.get("id") == sid:
            return a.get("name") or sid
    return sid


def theme_for_ticket(t: dict) -> str:
    sym = (t.get("symbol") or "").upper()
    tags = t.get("regime_tags") or []
    instr = t.get("instrument") or ""
    if sym == "SPY" and ("short_gamma_backwardation" in tags or "put" in instr):
        return "spy_short_gamma"
    if sym in ("NVDA", "AMD", "SMH", "SOXX") or "semi" in (t.get("tag") or "").lower():
        return "semis_ai"
    if sym in ("JPM", "BAC", "XLF", "KBE"):
        return "banks"
    if t.get("strategy_agent") == BUFFETT_ID:
        return "single_name_value"
    if sym == "SPY":
        return "spy_short_gamma"
    return "other"


def rule_id_for_ticket(t: dict) -> Optional[str]:
    sid = t.get("strategy_agent")
    tags = t.get("regime_tags") or []
    setup = t.get("setup_id") or ""
    if sid == ITT_ID:
        if "positive_gamma" in "".join(tags):
            return "ITT-POS-GAMMA-FADE"
        return "ITT-ORB-SHORT-GAMMA"
    if sid == PTJ_ID:
        return "PTJ-200SMA-INFLECTION"
    if setup.startswith("ITT"):
        return "ITT-ORB-SHORT-GAMMA"
    return None


# ---------- Upgrade 1: Reviews ----------

def infer_primary_driver(exit_reason: str, realized_r: Optional[float]) -> str:
    if exit_reason in ("time_stop", "eod_0dte_flat"):
        return "timing_execution"
    if exit_reason == "invalidation_hit":
        return "thesis_wrong"
    if exit_reason == "stop_hit":
        return "thesis_wrong" if (realized_r is not None and realized_r < 0) else "delta"
    if exit_reason == "target1_target2":
        return "delta"
    return "pending_fill"


def render_review_md(rev: dict) -> str:
    tpl = TEMPLATE.read_text(encoding="utf-8") if TEMPLATE.exists() else "# {{review_id}}\n"
    attr = rev.get("attribution") or {}
    learn = rev.get("system_learning") or {}
    repl = {
        "review_id": rev.get("review_id"),
        "ticket_id": rev.get("ticket_id"),
        "originating_specialist_name": rev.get("originating_specialist_name"),
        "originating_specialist_id": rev.get("originating_specialist_id"),
        "setup_id": rev.get("setup_id") or "—",
        "symbol": rev.get("symbol"),
        "instrument": rev.get("instrument"),
        "direction": rev.get("direction"),
        "quantity": rev.get("quantity"),
        "thesis_summary": rev.get("thesis_summary") or "—",
        "entry_price": rev.get("entry_price"),
        "exit_price": rev.get("exit_price"),
        "stop_price": rev.get("stop_price"),
        "realized_pnl_dollars": rev.get("realized_pnl_dollars"),
        "realized_r": rev.get("realized_r"),
        "time_in_trade_minutes": rev.get("time_in_trade_minutes"),
        "opened_ct": rev.get("opened_ct"),
        "closed_ct": rev.get("closed_ct"),
        "exit_reason": rev.get("exit_reason"),
        "status": rev.get("status"),
        "primary_driver": attr.get("primary_driver"),
        "secondary_driver": attr.get("secondary_driver") or "—",
        "attribution_notes": attr.get("notes") or "—",
        "lesson": learn.get("lesson") or "—",
        "weight_delta_suggestion": learn.get("weight_delta_suggestion"),
        "regime_tag": learn.get("regime_tag"),
        "applied_to_scorecard": learn.get("applied_to_scorecard"),
        "applied_ct": learn.get("applied_ct") or "—",
    }
    out = tpl
    for k, v in repl.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def build_reviews_index() -> dict:
    reviews = []
    for p in sorted(REVIEWS.glob("*.json"), reverse=True):
        if p.name in ("schema.json", "reviews_index.json"):
            continue
        obj = rj(p)
        if isinstance(obj, dict) and obj.get("review_id"):
            reviews.append(obj)
    reviews.sort(key=lambda r: r.get("closed_ct") or r.get("opened_ct") or "", reverse=True)
    idx = {
        "updated_ct": iso_ct(),
        "count": len(reviews),
        "reviews": reviews[:20],
        "disclaimer": DISCLAIMER,
    }
    wj(REVIEWS_INDEX, idx)
    return idx


def close_ticket(
    ticket_id: str,
    exit_price: float,
    opened_ct: Optional[str] = None,
    closed_ct: Optional[str] = None,
    exit_reason: str = "manual_close",
    primary_driver: Optional[str] = None,
    lesson: Optional[str] = None,
    apply_learning: bool = False,
    skip_portfolio_refresh: bool = False,
) -> dict:
    t = blotter_ticket(ticket_id)
    if not t:
        raise SystemExit(f"ERROR: ticket {ticket_id} not found")
    entry = float(t.get("entry") if t.get("entry") is not None else t.get("alpaca_fill_price") or 0)
    stop = float(t.get("stop") or (t.get("oco_bracket") or {}).get("stop_order") or 0)
    qty = float(t.get("quantity") or 0)
    mult = float(t.get("multiplier") or (100 if str(t.get("instrument", "")).startswith("option") else 1))
    direction = t.get("direction") or "long"
    sgn = 1 if direction == "long" else -1
    pnl = (float(exit_price) - entry) * sgn * qty * mult
    risk_unit = abs(entry - stop) if stop else float(t.get("risk_per_unit") or 0) or None
    realized_r = None
    if risk_unit and risk_unit > 0:
        realized_r = round(((float(exit_price) - entry) * sgn) / risk_unit, 4)

    opened = opened_ct or t.get("alpaca_filled_ct") or t.get("alpaca_submitted_ct") or t.get("created_ct")
    closed = closed_ct or iso_ct()
    ot, ct = parse_ct(opened), parse_ct(closed)
    mins = None
    if ot and ct:
        mins = round((ct - ot).total_seconds() / 60.0, 1)

    sid = t.get("strategy_agent") or ITT_ID
    driver = primary_driver or infer_primary_driver(exit_reason, realized_r)
    status = "stub"
    if lesson and primary_driver:
        status = "completed"
    lesson_line = lesson or (
        f"Mechanical close via {exit_reason}; realized_r={realized_r}. Awaiting CIO/specialist final learning line."
        if status == "stub"
        else lesson
    )
    regime = (t.get("regime_tags") or ["short_gamma_backwardation"])[0]
    if regime not in ("short_gamma_backwardation", "positive_gamma_contango", "below_200sma"):
        regime = "short_gamma_backwardation"
    weight_delta = 0.0
    if realized_r is not None:
        weight_delta = max(-0.10, min(0.10, round(0.04 * max(-2, min(2, realized_r)), 4)))

    review = {
        "review_id": f"REV-{ticket_id}",
        "ticket_id": ticket_id,
        "originating_specialist_id": sid,
        "originating_specialist_name": specialist_name(sid),
        "setup_id": t.get("setup_id"),
        "symbol": t.get("symbol"),
        "instrument": t.get("instrument"),
        "direction": direction,
        "quantity": qty,
        "thesis_summary": (t.get("entry_note") or t.get("tag") or "Staged paper ticket")[:280],
        "entry_price": entry,
        "exit_price": float(exit_price),
        "stop_price": stop,
        "realized_pnl_dollars": round(pnl, 2),
        "realized_r": realized_r,
        "opened_ct": opened,
        "closed_ct": closed,
        "time_in_trade_minutes": mins,
        "exit_reason": exit_reason,
        "attribution": {
            "primary_driver": driver,
            "secondary_driver": "timing_execution" if exit_reason in ("time_stop", "eod_0dte_flat") else None,
            "notes": f"exit={exit_reason}; R={realized_r}",
        },
        "system_learning": {
            "lesson": (lesson_line or "")[:240],
            "weight_delta_suggestion": weight_delta,
            "regime_tag": regime,
            "applied_to_scorecard": False,
            "applied_ct": None,
        },
        "status": status,
        "disclaimer": DISCLAIMER,
    }

    day = (parse_ct(closed) or now_ct()).strftime("%Y-%m-%d")
    base = REVIEWS / f"{day}_{ticket_id}"
    wj(Path(str(base) + ".json"), review)
    Path(str(base) + ".md").write_text(render_review_md(review), encoding="utf-8")

    def _mut(row):
        if row.get("ticket_id") != ticket_id:
            return row
        row = dict(row)
        row["status"] = "closed_paper"
        row["exit_price"] = float(exit_price)
        row["exit_reason"] = exit_reason
        row["closed_ct"] = closed
        row["realized_pnl_dollars"] = round(pnl, 2)
        row["realized_r"] = realized_r
        row["review_id"] = review["review_id"]
        return row

    rewrite_blotter(_mut)
    build_reviews_index()
    log_track_trade(ticket_id, review, t)
    rebuild_weekly_summary()
    ensure_armed_queue()
    if apply_learning and status == "completed":
        complete_review(ticket_id, driver, lesson_line, weight_delta, regime)
    if not skip_portfolio_refresh:
        compute_portfolio_snapshot()
        check_limits()
    return review


def complete_review(
    ticket_id: str,
    primary_driver: str,
    lesson: str,
    weight_delta: Optional[float] = None,
    regime_tag: Optional[str] = None,
) -> dict:
    # find review file
    paths = list(REVIEWS.glob(f"*_{ticket_id}.json"))
    if not paths:
        raise SystemExit(f"ERROR: review for {ticket_id} not found")
    path = sorted(paths)[-1]
    rev = rj(path)
    if weight_delta is None:
        weight_delta = rev.get("system_learning", {}).get("weight_delta_suggestion") or 0.0
    weight_delta = max(-0.10, min(0.10, float(weight_delta)))
    regime = regime_tag or rev.get("system_learning", {}).get("regime_tag") or "short_gamma_backwardation"
    rev["attribution"]["primary_driver"] = primary_driver
    rev["attribution"]["notes"] = (rev.get("attribution") or {}).get("notes") or ""
    rev["system_learning"]["lesson"] = lesson[:240]
    rev["system_learning"]["weight_delta_suggestion"] = weight_delta
    rev["system_learning"]["regime_tag"] = regime
    rev["status"] = "completed"

    sc = rj(SCORE, {"agents": {}, "schema_version": 1}) or {"agents": {}}
    sid = rev["originating_specialist_id"]
    ag = sc.setdefault("agents", {}).setdefault(sid, {
        "name": rev["originating_specialist_name"], "role": "specialist",
        "regime_weights": {
            "short_gamma_backwardation": 1.0,
            "positive_gamma_contango": 1.0,
            "below_200sma": 1.0,
        },
        "current_effective_weight": 0.3333,
    })
    rw = ag.setdefault("regime_weights", {})
    before = float(rw.get(regime, 1.0))
    after = max(0.10, min(0.60, before + weight_delta))
    rw[regime] = round(after, 4)
    # re-normalize effective weights across specialists using mean of regime weights
    for aid, a in sc["agents"].items():
        vals = list((a.get("regime_weights") or {}).values()) or [1.0]
        a["_raw_mean"] = sum(vals) / len(vals)
    total = sum(a.get("_raw_mean", 1.0) for a in sc["agents"].values()) or 1.0
    for a in sc["agents"].values():
        a["current_effective_weight"] = round(a.pop("_raw_mean") / total, 4)
    log = sc.setdefault("attribution_log", [])
    log.append({
        "review_id": rev["review_id"],
        "lesson": lesson[:240],
        "weight_before": before,
        "weight_after": after,
        "regime_tag": regime,
        "ts_ct": iso_ct(),
    })
    sc["attribution_log"] = log[-50:]
    sc["updated_ct"] = iso_ct()
    wj(SCORE, sc)

    rev["system_learning"]["applied_to_scorecard"] = True
    rev["system_learning"]["applied_ct"] = iso_ct()
    wj(path, rev)
    Path(str(path).replace(".json", ".md")).write_text(render_review_md(rev), encoding="utf-8")
    build_reviews_index()
    return rev


# ---------- Upgrade 3: Track record ----------

def ensure_armed_queue() -> dict:
    registry = rj(RULES, {"rules": []}) or {"rules": []}
    armed = []
    for t in rjl(BLOTTER):
        st = t.get("status")
        if st not in ("conditional_preapproved", "staged", "submitted_paper", "filled_paper"):
            continue
        rid = rule_id_for_ticket(t)
        if not rid:
            continue
        # Buffett excluded
        if t.get("strategy_agent") == BUFFETT_ID:
            continue
        armed.append({
            "ticket_id": t.get("ticket_id"),
            "rule_id": rid,
            "specialist_id": t.get("strategy_agent"),
            "specialist_name": specialist_name(t.get("strategy_agent") or ""),
            "status": st,
            "session_target": t.get("session_target"),
            "execution_source": "alpaca_paper" if t.get("alpaca_paper_payload") or st.endswith("paper") else "rule_sim_blotter",
            "symbol": t.get("symbol"),
            "occ_symbol": ((t.get("alpaca_paper_payload") or {}).get("order") or {}).get("symbol"),
            "quantity": t.get("quantity"),
            "entry": t.get("entry"),
            "stop": t.get("stop"),
        })
    # PTJ conditional status from macro folder if no ticket
    ptj_armed = any(a.get("specialist_id") == PTJ_ID for a in armed)
    if not ptj_armed:
        armed.append({
            "ticket_id": None,
            "rule_id": "PTJ-200SMA-INFLECTION",
            "specialist_id": PTJ_ID,
            "specialist_name": specialist_name(PTJ_ID),
            "status": "stand_down_watch",
            "session_target": None,
            "execution_source": "rule_sim_blotter",
            "note": "PTJ flat/defense — 200-SMA $730.15 watch; no ticket staged (CEO Scenario A only)",
            "symbol": "SPY",
        })
    doc = {
        "updated_ct": iso_ct(),
        "armed_count": len([a for a in armed if a.get("ticket_id")]),
        "armed": armed,
        "scope_lock": (registry.get("scope_lock") or ""),
        "closed_trades_to_date": len(rjl(TRADES)),
        "disclaimer": DISCLAIMER,
    }
    wj(ARMED_Q, doc)
    return doc


def log_track_trade(ticket_id: str, review: dict, ticket: Optional[dict] = None) -> Optional[dict]:
    ticket = ticket or blotter_ticket(ticket_id) or {}
    sid = review.get("originating_specialist_id") or ticket.get("strategy_agent")
    if sid == BUFFETT_ID:
        return None  # excluded until tactical history clean
    rid = rule_id_for_ticket(ticket) or rule_id_for_ticket({"strategy_agent": sid, "regime_tags": ticket.get("regime_tags")})
    if rid is None and sid not in (ITT_ID, PTJ_ID):
        return None
    # dedupe
    for row in rjl(TRADES):
        if row.get("ticket_id") == ticket_id:
            return row
    rr = review.get("realized_r")
    outcome = "scratch"
    if rr is not None:
        if rr > 0.15:
            outcome = "win"
        elif rr < -0.15:
            outcome = "loss"
        else:
            outcome = "scratch"
    rec = {
        "trade_id": f"TRD-{uuid.uuid4().hex[:10].upper()}",
        "ticket_id": ticket_id,
        "specialist_id": sid,
        "specialist_name": review.get("originating_specialist_name") or specialist_name(sid or ""),
        "rule_id": rid or "ITT-ORB-SHORT-GAMMA",
        "execution_source": "alpaca_paper" if ticket.get("alpaca_order_id") or ticket.get("status", "").endswith("paper") else "rule_sim_blotter",
        "symbol": review.get("symbol"),
        "instrument": review.get("instrument"),
        "direction": review.get("direction"),
        "qty": review.get("quantity"),
        "trigger_written": (ticket.get("underlying_trigger") or {}).get("or_break") or ticket.get("entry_note") or "",
        "invalidation_written": ticket.get("hard_invalidation_underlying") or str(ticket.get("stop")),
        "time_stop_written": ticket.get("time_stop") or "",
        "opened_ct": review.get("opened_ct"),
        "closed_ct": review.get("closed_ct"),
        "entry_price": review.get("entry_price"),
        "exit_price": review.get("exit_price"),
        "stop_price": review.get("stop_price"),
        "realized_pnl_dollars": review.get("realized_pnl_dollars"),
        "realized_r": review.get("realized_r"),
        "outcome": outcome,
        "exit_reason": review.get("exit_reason"),
    }
    ajl(TRADES, rec)
    return rec


def _metrics(trades: List[dict]) -> dict:
    closed = len(trades)
    wins = sum(1 for t in trades if t.get("outcome") == "win")
    losses = sum(1 for t in trades if t.get("outcome") == "loss")
    scratches = sum(1 for t in trades if t.get("outcome") == "scratch")
    rs = [float(t["realized_r"]) for t in trades if t.get("realized_r") is not None]
    avg_r = round(sum(rs) / len(rs), 4) if rs else None
    win_rate = round(wins / closed, 4) if closed else None
    # max drawdown in R (equity curve of cumulative R)
    max_dd = None
    if rs:
        eq = 0.0
        peak = 0.0
        dd = 0.0
        for r in rs:
            eq += r
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        max_dd = round(dd, 4)
    pos = sum(r for r in rs if r > 0)
    neg = abs(sum(r for r in rs if r < 0))
    if closed == 0:
        pf = None
    elif losses == 0 and wins > 0:
        pf = "∞"
    elif neg == 0:
        pf = None
    else:
        pf = round(pos / neg, 4)
    return {
        "closed_trades": closed,
        "wins": wins,
        "losses": losses,
        "scratches": scratches,
        "win_rate": win_rate,
        "avg_r": avg_r,
        "max_drawdown_r": max_dd,
        "max_drawdown_pct": None,  # notional % optional; R is primary
        "profit_factor": pf,
    }


def rebuild_weekly_summary() -> dict:
    trades = rjl(TRADES)
    now = now_ct()
    # ISO week Mon-Sun in CT
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

    def in_week(t):
        ct = parse_ct(t.get("closed_ct"))
        return ct is not None and ct >= week_start

    week_trades = [t for t in trades if in_week(t)]
    overall = _metrics(trades)
    week = _metrics(week_trades)
    by_sp = {}
    for sid, name in ((ITT_ID, "Intraday Tape & Gamma Tactician"), (PTJ_ID, "PTJ Macro & Regime Strategist")):
        all_s = [t for t in trades if t.get("specialist_id") == sid]
        wk_s = [t for t in week_trades if t.get("specialist_id") == sid]
        by_sp[sid] = {
            "name": name,
            "cumulative": _metrics(all_s),
            "week": _metrics(wk_s),
            "armed_count": len([a for a in (rj(ARMED_Q, {}).get("armed") or []) if a.get("specialist_id") == sid and a.get("ticket_id")]),
        }
    armed = ensure_armed_queue()
    doc = {
        "updated_ct": iso_ct(),
        "week_start_ct": iso_ct(week_start),
        "scope_lock": "Locked to Intraday Tape + PTJ 200-SMA rules until clean paper history established",
        "buffett_excluded_until_tactical_history_clean": True,
        "cumulative": overall,
        "week": week,
        "by_specialist": by_sp,
        "armed_count": armed.get("armed_count", 0),
        "disclaimer": DISCLAIMER,
    }
    wj(WEEKLY_JSON, doc)
    lines = [
        f"# Rule-Based Paper Track Record — week of {week_start.date()}",
        "",
        f"_Updated {doc['updated_ct']} · {doc['scope_lock']}_",
        "",
        "## Cumulative (inception → date)",
        f"- Closed: {overall['closed_trades']} (W {overall['wins']} / L {overall['losses']} / S {overall['scratches']})",
        f"- Win rate: {overall['win_rate'] if overall['win_rate'] is not None else '—'} · Avg R: {overall['avg_r'] if overall['avg_r'] is not None else '—'}",
        f"- Max DD (R): {overall['max_drawdown_r'] if overall['max_drawdown_r'] is not None else '—'} · Profit factor: {overall['profit_factor'] if overall['profit_factor'] is not None else '—'}",
        f"- Armed rules/tickets: {doc['armed_count']}",
        "",
        "## This ISO week",
        f"- Closed: {week['closed_trades']} · Win rate: {week['win_rate'] if week['win_rate'] is not None else '—'} · Avg R: {week['avg_r'] if week['avg_r'] is not None else '—'}",
        "",
        "## By specialist",
    ]
    for sid, block in by_sp.items():
        c = block["cumulative"]
        lines.append(
            f"- **{block['name']}**: closed {c['closed_trades']} · WR {c['win_rate'] if c['win_rate'] is not None else '—'} · "
            f"Avg R {c['avg_r'] if c['avg_r'] is not None else '—'} · armed {block['armed_count']}"
        )
    lines += ["", f"_{DISCLAIMER}_", ""]
    WEEKLY_MD.write_text("\n".join(lines), encoding="utf-8")
    return doc


# ---------- Upgrade 2 + 4: Portfolio risk + limits ----------

def estimate_delta(t: dict, spy: float) -> float:
    """Signed SPY-share-equivalent delta for one ticket/position."""
    qty = float(t.get("quantity") or t.get("qty") or 0)
    instr = str(t.get("instrument") or t.get("asset_class") or "stock")
    direction = t.get("direction") or ("long" if float(t.get("qty") or 1) > 0 else "short")
    sgn = 1.0 if direction == "long" else -1.0
    if instr in ("stock", "us_equity") or (not instr.startswith("option") and "option" not in instr):
        # short stock = negative delta
        return (-qty if direction == "short" else qty)
    # options: qty * 100 * est_delta
    strike = None
    occ = ((t.get("alpaca_paper_payload") or {}).get("order") or {}).get("symbol") or t.get("occ_symbol") or t.get("symbol")
    if isinstance(occ, str) and len(occ) >= 15 and occ[-8:].isdigit():
        strike = int(occ[-8:]) / 1000.0
    if strike is None:
        strike = float((t.get("underlying_trigger") or {}).get("prior_rth_low") or spy)
    # moneyness approx: ATM put ~ -0.50, ITM/OTM linear clamp
    mny = (spy - strike) / max(spy, 1e-9)  # >0 spot above strike
    if "put" in instr or (isinstance(occ, str) and "P" in occ[6:9]):
        # put delta ~ -0.50 at ATM; deeper ITM (spot<<strike) → -0.85; OTM → -0.15
        est = -0.50 - 0.7 * max(-0.5, min(0.5, (strike - spy) / max(spy * 0.05, 1e-6) * 0.05))
        est = max(-0.95, min(-0.05, est if abs(spy - strike) / spy > 0.002 else -0.50))
        # simpler transparent: ATM±0.5% → -0.50; else clamp by moneyness
        if abs(spy - strike) / spy <= 0.005:
            est = -0.50
        elif spy < strike:
            est = max(-0.85, -0.50 - (strike - spy) / spy * 2)
        else:
            est = min(-0.15, -0.50 + (spy - strike) / spy * 2)
    else:
        if abs(spy - strike) / spy <= 0.005:
            est = 0.50
        elif spy > strike:
            est = min(0.85, 0.50 + (spy - strike) / spy * 2)
        else:
            est = max(0.15, 0.50 - (strike - spy) / spy * 2)
    # long premium: put long = negative delta already in est; call long positive
    return sgn * qty * 100.0 * est


def ticket_dollar_risk(t: dict) -> float:
    if t.get("actual_dollar_risk") is not None:
        return float(t["actual_dollar_risk"])
    entry = float(t.get("entry") or 0)
    stop = float(t.get("stop") or 0)
    qty = float(t.get("quantity") or 0)
    mult = float(t.get("multiplier") or (100 if str(t.get("instrument", "")).startswith("option") else 1))
    return abs(entry - stop) * qty * mult


def active_blotter_tickets() -> List[dict]:
    out = []
    for t in rjl(BLOTTER):
        if t.get("status") in (
            "conditional_preapproved", "staged", "submitted_paper", "filled_paper", "approved"
        ):
            out.append(t)
    return out


def compute_portfolio_snapshot() -> dict:
    m = market_snapshot()
    spy = m["spy"]
    limits = rj(LIMITS, {}) or {}
    paper = rj(PAPER_STATE, {}) or {}
    eq_base = float(limits.get("equity_reference") or 50000.0)
    eq_paper = float(paper.get("equity") or limits.get("alpaca_paper_equity") or 100000.0)
    tickets = active_blotter_tickets()
    open_pos = paper.get("positions") or []

    open_tickets = [t for t in tickets if t.get("status") in ("filled_paper", "submitted_paper")]
    staged = [t for t in tickets if t.get("status") in ("conditional_preapproved", "staged")]

    def pack(rows, include_positions=False):
        net = 0.0
        gross = 0.0
        for t in rows:
            d = estimate_delta(t, spy)
            net += d
            gross += abs(d)
        if include_positions:
            for p in open_pos:
                # map position to pseudo ticket
                sym = p.get("symbol") or ""
                qty = abs(float(p.get("qty") or 0))
                side = p.get("side") or "long"
                instr = "option_put" if ("P" in sym[6:9] if len(sym) > 10 else False) else (
                    "option_call" if len(sym) > 10 else "stock"
                )
                d = estimate_delta({"symbol": sym, "occ_symbol": sym, "quantity": qty, "direction": side,
                                   "instrument": instr}, spy)
                net += d
                gross += abs(d)
        return {
            "net_delta_shares": round(net, 2),
            "net_delta_dollars": round(net * spy, 2),
            "gross_delta_shares": round(gross, 2),
            "gross_delta_dollars": round(gross * spy, 2),
        }

    open_delta = pack(open_tickets, include_positions=True)
    all_delta = pack(open_tickets + staged, include_positions=True)

    open_risk = sum(ticket_dollar_risk(t) for t in open_tickets)
    # also from positions unrealized if no matching ticket
    staged_risk = sum(ticket_dollar_risk(t) for t in staged)
    total_risk = open_risk + staged_risk

    def pct(x):
        return round(100.0 * x / eq_base, 4) if eq_base else 0.0

    # themes
    themes: Dict[str, float] = {}
    for t in open_tickets + staged:
        th = theme_for_ticket(t)
        themes[th] = themes.get(th, 0.0) + ticket_dollar_risk(t)
    max_theme_pct = float((limits.get("limits") or {}).get("max_theme_concentration_pct") or 0.75)
    flags = []
    for th, dol in themes.items():
        p = pct(dol)
        if p > max_theme_pct:
            flags.append({"theme": th, "risk_pct": p, "detail": f"{th} {p}% > max_theme {max_theme_pct}%"})
        if total_risk > 0 and dol / total_risk > 0.65 and len(open_tickets + staged) >= 2:
            flags.append({"theme": th, "risk_pct": p, "detail": f"{th} >65% of open risk budget with >=2 tickets"})
    if staged and staged_risk > 0 and themes.get("spy_short_gamma", 0) >= staged_risk * 0.999:
        flags.append({
            "theme": "spy_short_gamma",
            "risk_pct": pct(themes.get("spy_short_gamma", 0)),
            "detail": "INFO: 100% of staged risk concentrated in spy_short_gamma",
            "severity": "info",
        })

    # specialist correlation
    st = rj(STATE, {}) or {}
    agents = st.get("agents") or {}
    bearish = []
    bullish = []
    for aid, a in agents.items():
        if aid not in (ITT_ID, PTJ_ID, BUFFETT_ID):
            continue
        stance = (a.get("stance") or "") + " " + (a.get("headline") or "")
        low = stance.lower()
        name = a.get("name") or specialist_name(aid)
        if any(k in low for k in ("breakdown", "short", "bear", "below", "put", "pre-approved conditional", "or_low")):
            # Buffett hold cash is not directional short
            if "hold cash" in low or "wait" in low and "buffett" in name.lower():
                continue
            if "flat" in low and "stand_down" in low.replace(" ", "_") and "pre-approved" not in low:
                # PTJ flat defense — conditional bearish overlap only if trigger language present
                if "730" in low or "200" in low or "breakdown" in low:
                    bearish.append(name)
                continue
            bearish.append(name)
        if any(k in low for k in ("long", "bull", "reclaim", "call wall fade")) and "hold cash" not in low:
            bullish.append(name)
    # Also: ITT conditional short + PTJ knife-edge above 200-SMA with breakdown watch = CONDITIONAL BEARISH OVERLAP
    itt = agents.get(ITT_ID) or {}
    ptj = agents.get(PTJ_ID) or {}
    itt_s = ((itt.get("stance") or "") + " " + (itt.get("headline") or "")).lower()
    ptj_s = ((ptj.get("stance") or "") + " " + (ptj.get("headline") or "")).lower()
    corr_active = False
    corr_detail = "No multi-specialist directional overlap."
    aligned = []
    direction = None
    if ("conditional" in itt_s or "breakdown" in itt_s or "put" in itt_s) and (
        "200" in ptj_s or "730" in ptj_s or "breakdown" in ptj_s or "knife" in ptj_s
    ):
        corr_active = True
        direction = "short_spy"
        aligned = [itt.get("name") or "Intraday Tape", ptj.get("name") or "PTJ"]
        corr_detail = (
            "CONDITIONAL BEARISH OVERLAP: Intraday Tape Scenario A short-SPY trigger and PTJ 200-SMA "
            f"(${m['sma200']}) regime watch lean the same directional bet — do not double-count as independent risk."
        )
    elif len(set(bearish)) >= 2:
        corr_active = True
        direction = "short"
        aligned = sorted(set(bearish))
        corr_detail = f"Aligned bearish specialists: {', '.join(aligned)}"
    elif len(set(bullish)) >= 2:
        corr_active = True
        direction = "long"
        aligned = sorted(set(bullish))
        corr_detail = f"Aligned bullish specialists: {', '.join(aligned)}"

    snap = {
        "updated_ct": iso_ct(),
        "spy_spot": spy,
        "vix": m["vix"],
        "net_gex_45d": m.get("net_gex_45d"),
        "equity_sizing_base": eq_base,
        "alpaca_paper_equity": eq_paper,
        "open_positions_count": len(open_pos) + len(open_tickets),
        "staged_tickets_count": len(staged),
        "delta_exposure": {
            "open_only": open_delta,
            "including_staged": all_delta,
            "formula_note": (
                "stock: ±qty shares; option: qty*100*est_delta with ATM put≈-0.50 at spot, "
                "moneyness-adjusted clamp — transparent, zero fake VaR"
            ),
        },
        "capital_at_risk": {
            "open_dollar_risk": round(open_risk, 2),
            "open_risk_pct": pct(open_risk),
            "staged_dollar_risk": round(staged_risk, 2),
            "staged_risk_pct": pct(staged_risk),
            "total_open_plus_staged_dollar_risk": round(total_risk, 2),
            "total_open_plus_staged_risk_pct": pct(total_risk),
            "max_total_open_risk_pct_limit": float((limits.get("limits") or {}).get("max_total_open_risk_pct") or 1.0),
            "pct_of_sizing_base_note": f"% of ${eq_base:,.0f} sizing baseline (Alpaca paper equity ${eq_paper:,.0f})",
        },
        "theme_concentration": {
            "by_theme_dollar_risk": {k: round(v, 2) for k, v in themes.items()},
            "by_theme_risk_pct": {k: pct(v) for k, v in themes.items()},
            "concentration_flags": flags,
            "max_theme_risk_pct": max_theme_pct,
        },
        "specialist_correlation": {
            "correlation_warning": {
                "active": corr_active,
                "direction": direction,
                "aligned_specialists": aligned,
                "detail": corr_detail,
            }
        },
        "disclaimer": DISCLAIMER,
    }
    wj(PORTFOLIO, snap)
    # sync usage into limits
    if limits:
        usage = limits.setdefault("current_usage", {})
        usage["open_plus_staged_risk_pct"] = pct(total_risk)
        limits["alpaca_paper_equity"] = eq_paper
        limits["updated_ct"] = iso_ct()
        wj(LIMITS, limits)
    return snap


def realized_pnl_window(start: datetime, end: Optional[datetime] = None) -> float:
    end = end or now_ct()
    total = 0.0
    for t in rjl(BLOTTER):
        if t.get("status") not in ("closed_paper", "closed"):
            continue
        ct = parse_ct(t.get("closed_ct"))
        if ct is None or ct < start or ct > end:
            continue
        if t.get("realized_pnl_dollars") is not None:
            total += float(t["realized_pnl_dollars"])
    for t in rjl(TRADES):
        # avoid double count if blotter already has it
        ct = parse_ct(t.get("closed_ct"))
        if ct is None or ct < start or ct > end:
            continue
        # only add if blotter missing
        tid = t.get("ticket_id")
        bt = blotter_ticket(tid) if tid else None
        if bt and bt.get("realized_pnl_dollars") is not None:
            continue
        if t.get("realized_pnl_dollars") is not None:
            total += float(t["realized_pnl_dollars"])
    return round(total, 2)


def write_kill_switch_memo(breached_limits: List[str], limits_doc: dict) -> str:
    now = now_ct()
    # dedupe: if breach memo already exists for this episode
    existing = limits_doc.get("kill_switch", {}).get("breach_memo_path")
    if existing and (ROOT / existing).exists() and limits_doc.get("kill_switch", {}).get("breached"):
        return existing
    fname = f"{now.strftime('%Y-%m-%d_%H%M')}-ct-risk-limit-breached_cio_briefing.md"
    path = CIO_DIR / fname
    CIO_DIR.mkdir(parents=True, exist_ok=True)
    body = "\n".join([
        "# RISK LIMIT BREACHED — KILL SWITCH ENGAGED",
        "",
        f"_CT {iso_ct(now)} · CIO & Risk Gatekeeper_",
        "",
        "## Breached limits",
        *[f"- `{b}`" for b in breached_limits],
        "",
        "## Standing limits",
        f"- Max risk / ticket: {limits_doc.get('limits', {}).get('max_risk_per_ticket_pct')}%",
        f"- Max total open risk: {limits_doc.get('limits', {}).get('max_total_open_risk_pct')}%",
        f"- Max daily realized loss: {limits_doc.get('limits', {}).get('max_daily_realized_loss_pct')}%",
        f"- Max weekly realized loss: {limits_doc.get('limits', {}).get('max_weekly_realized_loss_pct')}%",
        "",
        "## Current usage",
        f"- Open+staged risk: {limits_doc.get('current_usage', {}).get('open_plus_staged_risk_pct')}%",
        f"- Daily realized loss: {limits_doc.get('current_usage', {}).get('daily_realized_loss_pct')}%",
        f"- Weekly realized loss: {limits_doc.get('current_usage', {}).get('weekly_realized_loss_pct')}%",
        "",
        "## Action",
        "- NEW ticket staging/submission is REFUSED until CEO override/reset via CEO Decisions box.",
        "- Protective exits on already-open paper positions remain armed.",
        "",
        f"_{DISCLAIMER}_",
        "",
    ])
    path.write_text(body, encoding="utf-8")
    return str(path.relative_to(ROOT)).replace("\\", "/")


def inject_kill_switch_ceo_question(breached_limits: List[str]):
    q = rj(CEO_Q, {}) or {}
    # if already has open kill-switch question, keep
    for qq in q.get("questions") or []:
        if qq.get("id") == "kill_switch_override":
            q["status"] = "open"
            wj(CEO_Q, q)
            return q
    q = {
        "prompt_set_id": f"CEO-Q-KILL-{now_ct().strftime('%Y%m%d-%H%M')}",
        "status": "open",
        "session_target": now_ct().strftime("%Y-%m-%d"),
        "created_ct": iso_ct(),
        "title": "Kill Switch Override / Reset",
        "questions": [
            {
                "id": "kill_switch_override",
                "title": "Risk kill switch is ENGAGED — choose action",
                "detail": "Breached: " + ", ".join(breached_limits),
                "options": [
                    {"value": "reset_clear", "label": "Reset kill switch to ARMED_CLEAR (acknowledge breach)"},
                    {"value": "raise_daily_1_5", "label": "Temporarily raise max daily loss to 1.50%"},
                    {"value": "raise_weekly_3_0", "label": "Temporarily raise max weekly loss to 3.00%"},
                    {"value": "raise_open_1_5", "label": "Temporarily raise max total open risk to 1.50%"},
                ],
            }
        ],
        "disclaimer": DISCLAIMER,
    }
    wj(CEO_Q, q)
    return q


def check_limits() -> dict:
    limits = rj(LIMITS, {}) or {}
    lim = limits.setdefault("limits", {})
    usage = limits.setdefault("current_usage", {})
    snap = rj(PORTFOLIO) or compute_portfolio_snapshot()
    eq = float(limits.get("equity_reference") or 50000.0)

    now = now_ct()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    daily_pnl = realized_pnl_window(day_start)
    weekly_pnl = realized_pnl_window(week_start)
    daily_loss_pct = round(100.0 * min(0.0, daily_pnl) * -1 / eq, 4) if daily_pnl < 0 else 0.0
    weekly_loss_pct = round(100.0 * min(0.0, weekly_pnl) * -1 / eq, 4) if weekly_pnl < 0 else 0.0
    usage["daily_realized_pnl_dollars"] = daily_pnl
    usage["weekly_realized_pnl_dollars"] = weekly_pnl
    usage["daily_realized_loss_pct"] = daily_loss_pct
    usage["weekly_realized_loss_pct"] = weekly_loss_pct
    usage["open_plus_staged_risk_pct"] = (snap.get("capital_at_risk") or {}).get("total_open_plus_staged_risk_pct") or 0.0

    # active CEO override?
    override = None
    dec = rj(CEO_D, {}) or {}
    ov = (dec.get("kill_switch_override") or dec.get("responses", {}).get("kill_switch_override")
          or limits.get("kill_switch", {}).get("active_ceo_override"))
    if isinstance(ov, dict) and ov.get("expires_ct"):
        exp = parse_ct(ov["expires_ct"])
        if exp and exp > now:
            override = ov
        else:
            limits.setdefault("kill_switch", {})["active_ceo_override"] = None
    elif isinstance(ov, dict):
        override = ov

    max_daily = float(lim.get("max_daily_realized_loss_pct") or 1.0)
    max_weekly = float(lim.get("max_weekly_realized_loss_pct") or 2.5)
    max_open = float(lim.get("max_total_open_risk_pct") or 1.0)
    max_ticket = float(lim.get("max_risk_per_ticket_pct") or 0.5)
    if override:
        max_daily = float(override.get("max_daily_realized_loss_pct", max_daily))
        max_weekly = float(override.get("max_weekly_realized_loss_pct", max_weekly))
        max_open = float(override.get("max_total_open_risk_pct", max_open))
        max_ticket = float(override.get("max_risk_per_ticket_pct", max_ticket))

    breached = []
    if daily_loss_pct > max_daily + 1e-9:
        breached.append(f"max_daily_realized_loss_pct ({daily_loss_pct}% > {max_daily}%)")
    if weekly_loss_pct > max_weekly + 1e-9:
        breached.append(f"max_weekly_realized_loss_pct ({weekly_loss_pct}% > {max_weekly}%)")
    open_pct = float(usage.get("open_plus_staged_risk_pct") or 0)
    if open_pct > max_open + 1e-9:
        breached.append(f"max_total_open_risk_pct ({open_pct}% > {max_open}%)")

    # per-ticket check on active
    for t in active_blotter_tickets():
        rp = float(t.get("risk_pct_effective") or 0)
        # also dollar / eq
        dol = ticket_dollar_risk(t)
        rp2 = 100.0 * dol / eq if eq else 0
        use = max(rp, rp2)
        if use > max_ticket + 1e-9:
            breached.append(f"max_risk_per_ticket_pct on {t.get('ticket_id')} ({use}% > {max_ticket}%)")

    ks = limits.setdefault("kill_switch", {})
    ack = ks.get("ceo_ack") or {}
    if ack and breached:
        # Suppress limits that have not worsened past CEO acknowledgment snapshot
        filtered = []
        for b in breached:
            if b.startswith("max_daily_realized_loss_pct") and daily_loss_pct <= float(ack.get("daily_realized_loss_pct") or 0) + 1e-9:
                continue
            if b.startswith("max_weekly_realized_loss_pct") and weekly_loss_pct <= float(ack.get("weekly_realized_loss_pct") or 0) + 1e-9:
                continue
            if b.startswith("max_total_open_risk_pct") and open_pct <= float(ack.get("open_plus_staged_risk_pct") or 0) + 1e-9:
                continue
            filtered.append(b)
        breached = filtered
    if breached:
        was = bool(ks.get("breached"))
        ks["breached"] = True
        ks["status"] = "BREACHED"
        ks["breached_limits"] = breached
        ks["ceo_ack"] = None  # new adverse breach supersedes prior ack
        if not was:
            ks["breached_since_ct"] = iso_ct()
        memo = write_kill_switch_memo(breached, limits)
        ks["breach_memo_path"] = memo
        inject_kill_switch_ceo_question(breached)
        # append paper execution event for LiveHub
        try:
            ev_path = POD / "paper_execution_events.json"
            doc = rj(ev_path, {"events": []}) or {"events": []}
            if not any(e.get("kind") == "KILL_SWITCH_BREACHED" and e.get("event_id", "").startswith("EVT-KS-")
                       for e in (doc.get("events") or [])[-5:]):
                doc.setdefault("events", []).append({
                    "event_id": f"EVT-KS-{uuid.uuid4().hex[:8].upper()}",
                    "ts_ct": iso_ct(),
                    "kind": "KILL_SWITCH_BREACHED",
                    "ticket_id": None,
                    "symbol": "PORTFOLIO",
                    "headline": "🚨 HARD KILL SWITCH ENGAGED — RISK LIMIT BREACHED",
                    "detail": "; ".join(breached)[:240],
                    "severity": "neg",
                })
                doc["events"] = doc["events"][-30:]
                wj(ev_path, doc)
        except Exception:
            pass
    else:
        ks["breached"] = False
        ks["status"] = "ARMED_CLEAR"
        ks["breached_limits"] = []
        # keep breached_since / memo for audit until CEO reset clears them explicitly

    ks["active_ceo_override"] = override
    limits["updated_ct"] = iso_ct()
    wj(LIMITS, limits)
    # rebuild memos index if possible
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("pod_engine", ROOT / "scripts" / "pod_engine.py")
        # avoid circular heavy import — call build via subprocess only when breached newly
    except Exception:
        pass
    return limits


def kill_switch_blocks_new_entries() -> Tuple[bool, str]:
    limits = check_limits()
    ks = limits.get("kill_switch") or {}
    if ks.get("breached"):
        return True, "REFUSED_KILL_SWITCH_BREACH: " + "; ".join(ks.get("breached_limits") or [])
    return False, "ARMED_CLEAR"


def apply_portfolio_gate_to_ticket(ticket: dict, auto_reduce: bool = True, strict_block: bool = False) -> dict:
    """Mutate/approve ticket against portfolio limits. May reduce qty or set BLOCKED_* verdict."""
    blocked, why = kill_switch_blocks_new_entries()
    if blocked:
        ticket["verdict"] = "REFUSED_KILL_SWITCH_BREACH"
        ticket.setdefault("errors", []).append(why)
        ticket["portfolio_gate"] = {"pass": False, "reason": why}
        return ticket

    limits = rj(LIMITS, {}) or {}
    lim = limits.get("limits") or {}
    eq = float(limits.get("equity_reference") or 50000.0)
    max_open = float(lim.get("max_total_open_risk_pct") or 1.0)
    max_theme = float(lim.get("max_theme_concentration_pct") or 0.75)
    max_ticket = float(lim.get("max_risk_per_ticket_pct") or 0.5)
    snap = compute_portfolio_snapshot()
    cur = float((snap.get("capital_at_risk") or {}).get("total_open_plus_staged_risk_pct") or 0)
    # exclude this ticket if already in blotter staged
    # remaining room in dollars
    room_pct = max_open - cur
    # if ticket already counted in snap (re-stage), add back its risk
    tid = ticket.get("ticket_id")
    existing = blotter_ticket(tid) if tid else None
    if existing and existing.get("status") in ("conditional_preapproved", "staged", "submitted_paper", "filled_paper"):
        room_pct += 100.0 * ticket_dollar_risk(existing) / eq

    risk_per = float(ticket.get("risk_per_unit") or 0) * float(ticket.get("multiplier") or 1)
    if risk_per <= 0:
        entry = float(ticket.get("entry") or 0)
        stop = float(ticket.get("stop") or 0)
        mult = float(ticket.get("multiplier") or 1)
        risk_per = abs(entry - stop) * mult
    qty = int(ticket.get("quantity") or 0)
    ticket_risk = risk_per * qty
    ticket_pct = 100.0 * ticket_risk / eq if eq else 0

    if ticket_pct > max_ticket + 1e-9:
        ticket["verdict"] = "BLOCKED_PORTFOLIO_LIMIT"
        ticket.setdefault("errors", []).append(
            f"Ticket risk {ticket_pct:.4f}% > max_risk_per_ticket_pct {max_ticket}%"
        )
        ticket["portfolio_gate"] = {"pass": False, "reason": "max_risk_per_ticket"}
        return ticket

    theme = theme_for_ticket(ticket)
    theme_now = float(((snap.get("theme_concentration") or {}).get("by_theme_risk_pct") or {}).get(theme) or 0)
    # room for theme
    proposed_theme = theme_now + ticket_pct
    # if existing same ticket in theme, avoid double count roughly
    if existing:
        proposed_theme = theme_now  # already included

    warnings = []
    if cur + ticket_pct > max_open + 1e-9 or (not existing and proposed_theme > max_theme + 1e-9 and theme_now + ticket_pct > max_theme):
        if strict_block or not auto_reduce:
            ticket["verdict"] = "BLOCKED_PORTFOLIO_LIMIT"
            ticket.setdefault("errors", []).append(
                f"Adding ticket would push open+staged risk to {cur + ticket_pct:.4f}% > {max_open}% "
                f"(or theme {theme} above {max_theme}%)"
            )
            ticket["portfolio_gate"] = {"pass": False, "reason": "max_total_or_theme"}
            return ticket
        # auto-reduce
        room_dollars = max(0.0, room_pct / 100.0 * eq)
        if risk_per <= 0:
            ticket["verdict"] = "BLOCKED_PORTFOLIO_LIMIT"
            ticket.setdefault("errors", []).append("Cannot auto-reduce: risk_per_unit undefined")
            return ticket
        new_qty = int(math.floor(room_dollars / risk_per))
        # also theme room
        theme_room_pct = max_theme - theme_now
        theme_room_dollars = max(0.0, theme_room_pct / 100.0 * eq)
        new_qty = min(new_qty, int(math.floor(theme_room_dollars / risk_per))) if theme_room_dollars >= 0 else new_qty
        if new_qty < 1:
            ticket["verdict"] = "BLOCKED_PORTFOLIO_LIMIT"
            ticket.setdefault("errors", []).append(
                f"Even 1 contract exceeds remaining open-risk room ({room_pct:.4f}% left of {max_open}%)"
            )
            ticket["portfolio_gate"] = {"pass": False, "reason": "no_room"}
            ticket["quantity"] = 0
            ticket["actual_dollar_risk"] = 0
            return ticket
        if new_qty < qty:
            ticket["quantity"] = new_qty
            ticket["actual_dollar_risk"] = round(risk_per * new_qty, 2)
            ticket["size_auto_reduced"] = True
            warnings.append(f"Auto-reduced qty {qty} → {new_qty} to stay <= portfolio limits")
            ticket["portfolio_gate"] = {"pass": True, "size_auto_reduced": True, "warnings": warnings}
        else:
            ticket["portfolio_gate"] = {"pass": True, "warnings": warnings}
    else:
        ticket["portfolio_gate"] = {"pass": True}
    return ticket


def record_kill_switch_decision(action: str, note: str = "") -> dict:
    """CEO override/reset path."""
    limits = rj(LIMITS, {}) or {}
    ks = limits.setdefault("kill_switch", {})
    log = limits.setdefault("override_audit_log", [])
    entry = {"ts_ct": iso_ct(), "action": action, "note": note, "by": "CEO"}
    override = None
    if action == "reset_clear":
        ks["breached"] = False
        ks["status"] = "ARMED_CLEAR"
        ks["breached_limits"] = []
        ks["breached_since_ct"] = None
        ks["breach_memo_path"] = None
        ks["active_ceo_override"] = None
        # Acknowledge current realized-loss usage so check_limits won't instantly re-trip
        # until losses worsen beyond this snapshot (new adverse PnL).
        usage = limits.get("current_usage") or {}
        ks["ceo_ack"] = {
            "ts_ct": iso_ct(),
            "daily_realized_loss_pct": float(usage.get("daily_realized_loss_pct") or 0),
            "weekly_realized_loss_pct": float(usage.get("weekly_realized_loss_pct") or 0),
            "open_plus_staged_risk_pct": float(usage.get("open_plus_staged_risk_pct") or 0),
        }
        entry["result"] = "ARMED_CLEAR"
        entry["ceo_ack"] = ks["ceo_ack"]
    elif action == "raise_daily_1_5":
        override = {"max_daily_realized_loss_pct": 1.5, "expires_ct": iso_ct(now_ct() + timedelta(days=1)), "action": action}
    elif action == "raise_weekly_3_0":
        override = {"max_weekly_realized_loss_pct": 3.0, "expires_ct": iso_ct(now_ct() + timedelta(days=7)), "action": action}
    elif action == "raise_open_1_5":
        override = {"max_total_open_risk_pct": 1.5, "expires_ct": iso_ct(now_ct() + timedelta(days=1)), "action": action}
    else:
        raise SystemExit(f"Unknown kill-switch action: {action}")
    if override:
        ks["active_ceo_override"] = override
        # clear breach if override raises the breached limit enough — recompute
        ks["breached"] = False
        ks["status"] = "ARMED_CLEAR_WITH_OVERRIDE"
        ks["breached_limits"] = []
        entry["override"] = override
    log.append(entry)
    limits["override_audit_log"] = log[-50:]
    limits["updated_ct"] = iso_ct()
    wj(LIMITS, limits)

    dec = rj(CEO_D, {}) or {}
    dec["kill_switch_override"] = ks.get("active_ceo_override")
    dec["kill_switch_last_action"] = entry
    dec["updated_ct"] = iso_ct()
    wj(CEO_D, dec)

    # mark CEO question answered if present
    q = rj(CEO_Q, {}) or {}
    if q.get("questions"):
        for qq in q["questions"]:
            if qq.get("id") == "kill_switch_override":
                q["status"] = "answered"
                q.setdefault("responses", {})["kill_switch_override"] = action
        wj(CEO_Q, q)

    check_limits()
    return entry


def on_position_closed(ticket_id: str, exit_price: float, exit_kind: str, opened_ct: Optional[str] = None):
    """Hook from alpaca RTH exits."""
    reason = EXIT_MAP.get(exit_kind, "manual_close")
    return close_ticket(
        ticket_id, exit_price=exit_price, opened_ct=opened_ct,
        exit_reason=reason, skip_portfolio_refresh=False,
    )


def bootstrap_production_state() -> dict:
    """Generate all initial JSON/MD for live Friday-armed state."""
    REVIEWS.mkdir(parents=True, exist_ok=True)
    RISK.mkdir(parents=True, exist_ok=True)
    TRACK.mkdir(parents=True, exist_ok=True)
    if not TRADES.exists():
        TRADES.write_text("", encoding="utf-8")
    build_reviews_index()
    ensure_armed_queue()
    weekly = rebuild_weekly_summary()
    snap = compute_portfolio_snapshot()
    limits = check_limits()
    # register engines on roster
    rost = rj(ROSTER, {}) or {}
    engines = rost.setdefault("engines", [])
    wanted = [
        {"name": "Post-Trade Journal & Attribution", "type": "deterministic",
         "entrypoint": "python3 scripts/pod_engine.py close-ticket / complete-review",
         "owner": CIO_ID,
         "mandate": "Closed-ticket reviews → pod/reviews + scorecard attribution_log"},
        {"name": "Portfolio Risk Snapshot", "type": "deterministic",
         "entrypoint": "python3 scripts/pod_engine.py portfolio-risk",
         "owner": CIO_ID,
         "mandate": "Net/gross delta, capital at risk, concentration, specialist correlation"},
        {"name": "Rule-Based Paper Track Record", "type": "deterministic",
         "entrypoint": "python3 scripts/pod_engine.py track-record",
         "owner": CIO_ID,
         "mandate": "Intraday Tape + PTJ 200-SMA forward paper ledger & weekly summary"},
        {"name": "Kill Switch & Max Loss Limits", "type": "deterministic",
         "entrypoint": "python3 scripts/pod_engine.py check-limits",
         "owner": CIO_ID,
         "mandate": "Hard refuse staging on breach; CEO override via Decisions box"},
    ]
    names = {e.get("name") for e in engines}
    for e in wanted:
        if e["name"] not in names:
            engines.append(e)
    rost["updated_ct"] = iso_ct()
    wj(ROSTER, rost)
    return {"weekly": weekly, "snapshot": snap, "limits": limits, "reviews": rj(REVIEWS_INDEX)}

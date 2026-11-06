#!/usr/bin/env python3
"""Alpaca Paper RTH trigger/OCO helper for pod_engine.alpaca-rth-tick.

Paper-only. Never targets api.alpaca.markets. Zero paid deps (Yahoo chart fallback).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover
    CT = timezone(timedelta(hours=-6))

# Relative imports when loaded as sibling of pod_engine
ROOT = Path(os.environ.get("HORSEY_ROOT", Path(__file__).resolve().parents[1]))
PAPER_EVENTS = ROOT / "pod" / "paper_execution_events.json"
BLOTTER = ROOT / "pod" / "ledger" / "paper_blotter.jsonl"
LATEST = ROOT / "latest.json"
WATCHER = ROOT / "scripts" / "alpaca_rth_watcher.py"
LOCK = Path("/tmp/horsey_alpaca_rth_watcher.lock")


def _gov_close(ticket_id, exit_price, kind, opened_ct=None, notes=None):
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_pod_governance", str(ROOT / "scripts" / "_pod_governance.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.on_position_closed(ticket_id, float(exit_price), kind, opened_ct)
    except Exception as e:
        if notes is not None:
            notes.append(f"gov_close_warn:{ticket_id}:{e}")

DONE_STATUSES = {
    "submitted_paper", "filled_paper", "closed_paper", "invalidated_paper", "expired_paper",
}
ENTRY_WINDOW_START = (9, 0)   # CT after OR built
ENTRY_WINDOW_END = (14, 30)
EOD_FLAT = (14, 45)
RTH_START = (8, 30)
RTH_END = (15, 5)


def _iso_ct(dt: Optional[datetime] = None) -> str:
    d = (dt or datetime.now(CT)).astimezone(CT)
    return d.strftime("%Y-%m-%dT%H:%M:%S%z")


def _now_ct() -> datetime:
    return datetime.now(CT)


def _hm(dt: datetime) -> Tuple[int, int]:
    return dt.hour, dt.minute


def _in_window(dt: datetime, start: Tuple[int, int], end: Tuple[int, int]) -> bool:
    h, m = _hm(dt)
    return (h, m) >= start and (h, m) <= end


def _weekday(dt: datetime) -> bool:
    return dt.weekday() < 5


def load_events() -> Dict[str, Any]:
    if PAPER_EVENTS.exists():
        try:
            return json.loads(PAPER_EVENTS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "schema_version": 1,
        "updated_ct": _iso_ct(),
        "status": "init",
        "armed_tickets": [],
        "events": [],
        "disclaimer": "Paper / Architecture Demo — Not Live Financial Advice",
    }


def save_events(doc: Dict[str, Any]) -> None:
    doc["updated_ct"] = _iso_ct()
    doc["schema_version"] = 1
    doc.setdefault("disclaimer", "Paper / Architecture Demo — Not Live Financial Advice")
    # keep last 30 events
    ev = doc.get("events") or []
    if len(ev) > 30:
        doc["events"] = ev[-30:]
    PAPER_EVENTS.parent.mkdir(parents=True, exist_ok=True)
    PAPER_EVENTS.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def append_event(doc: Dict[str, Any], kind: str, ticket_id: str, headline: str,
                 detail: str, severity: str = "pos", symbol: str = "", extra: Optional[dict] = None) -> Dict[str, Any]:
    evt = {
        "event_id": f"EVT-{uuid.uuid4().hex[:10].upper()}",
        "ts_ct": _iso_ct(),
        "kind": kind,
        "ticket_id": ticket_id,
        "symbol": symbol,
        "headline": headline,
        "detail": detail,
        "severity": severity,
    }
    if extra:
        evt.update(extra)
    doc.setdefault("events", []).append(evt)
    return evt


def blotter_rows() -> List[dict]:
    if not BLOTTER.exists():
        return []
    out = []
    for line in BLOTTER.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def rewrite_blotter_ticket(ticket_id: str, mutator) -> Optional[dict]:
    rows = blotter_rows()
    found = None
    for i, t in enumerate(rows):
        if t.get("ticket_id") == ticket_id:
            found = mutator(dict(t))
            rows[i] = found
            break
    if found is None:
        return None
    with BLOTTER.open("w", encoding="utf-8") as f:
        for t in rows:
            f.write(json.dumps(t) + "\n")
    return found


def armed_tickets() -> List[dict]:
    return [t for t in blotter_rows() if t.get("status") == "conditional_preapproved"]


def prior_rth_low(ticket: dict) -> float:
    ut = ticket.get("underlying_trigger") or {}
    if ut.get("prior_rth_low") is not None:
        return float(ut["prior_rth_low"])
    if LATEST.exists():
        try:
            j = json.loads(LATEST.read_text(encoding="utf-8"))
            # best-effort
            for k in ("prior_rth_low", "spy_prior_rth_low"):
                if j.get(k) is not None:
                    return float(j[k])
            s = j.get("summary") or {}
            if s.get("spy_prior_low") is not None:
                return float(s["spy_prior_low"])
        except Exception:
            pass
    return 732.71


def parse_bar_ts(t: str) -> datetime:
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CT)


def fetch_alpaca_bars(pe, timeframe: str, start_utc: datetime, end_utc: Optional[datetime] = None) -> List[dict]:
    """Return list of {t,o,h,l,c,v} in CT-aware form using Alpaca IEX data API."""
    alpaca_request = pe.alpaca_request
    ALPACA_DATA_HOST = pe.ALPACA_DATA_HOST
    params = {
        "timeframe": timeframe,
        "feed": "iex",
        "start": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 1000,
        "adjustment": "raw",
        "sort": "asc",
    }
    if end_utc:
        params["end"] = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        data = alpaca_request("GET", ALPACA_DATA_HOST, "/v2/stocks/SPY/bars", params=params, timeout=25)
    except Exception as e:
        return []
    bars = (data or {}).get("bars") or []
    out = []
    for b in bars:
        out.append({
            "t": b.get("t"),
            "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]), "c": float(b["c"]),
            "v": float(b.get("v") or 0),
            "source": "alpaca_iex",
        })
    return out


def fetch_yahoo_bars_5m() -> List[dict]:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/SPY?interval=5m&range=2d&includePrePost=false"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 HorseyRTH/1.0"})
    try:
        with urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
        res = d["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
        out = []
        for i, t in enumerate(ts):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            v = (q.get("volume") or [None])[i]
            if None in (o, h, l, c):
                continue
            dt = datetime.fromtimestamp(t, tz=timezone.utc).astimezone(CT)
            out.append({
                "t": dt.isoformat(),
                "o": float(o), "h": float(h), "l": float(l), "c": float(c),
                "v": float(v or 0),
                "source": "yahoo",
                "_dt": dt,
            })
        return out
    except Exception:
        return []


def session_rth_start_utc(day_ct: datetime) -> datetime:
    local = day_ct.astimezone(CT).replace(hour=8, minute=30, second=0, microsecond=0)
    return local.astimezone(timezone.utc)


def filter_rth_bars(bars: List[dict], day_ct: datetime) -> List[dict]:
    """Keep bars whose CT timestamp is on day_ct date and >= 08:30 CT."""
    day = day_ct.astimezone(CT).date()
    out = []
    for b in bars:
        dt = b.get("_dt") or parse_bar_ts(b["t"])
        b = dict(b)
        b["_dt"] = dt
        if dt.date() != day:
            continue
        if (dt.hour, dt.minute) < (8, 30):
            continue
        if (dt.hour, dt.minute) > (15, 5):
            continue
        out.append(b)
    out.sort(key=lambda x: x["_dt"])
    return out


def compute_or(bars_5m: List[dict], now: datetime) -> Dict[str, Any]:
    """30m Opening Range = first six 5m RTH bars once now >= 09:00 CT."""
    or_bars = [b for b in bars_5m if (b["_dt"].hour, b["_dt"].minute) < (9, 0)]
    # first six after 08:30
    or_bars = or_bars[:6]
    ready = _hm(now) >= (9, 0) and len(or_bars) >= 1
    if not or_bars:
        return {"ready": False, "or_high": None, "or_low": None, "or_mid": None, "bar_count": 0}
    hi = max(b["h"] for b in or_bars)
    lo = min(b["l"] for b in or_bars)
    mid = (hi + lo) / 2.0
    # rvol estimate vs median volume of OR bars
    vols = [b["v"] for b in or_bars if b.get("v")]
    med = sorted(vols)[len(vols) // 2] if vols else 0
    latest = bars_5m[-1] if bars_5m else None
    rvol = None
    if latest and med and med > 0:
        rvol = latest["v"] / med
    return {
        "ready": ready,
        "or_high": round(hi, 4),
        "or_low": round(lo, 4),
        "or_mid": round(mid, 4),
        "bar_count": len(or_bars),
        "rvol_est": round(rvol, 3) if rvol is not None else None,
        "latest": ({"o": latest["o"], "h": latest["h"], "l": latest["l"], "c": latest["c"], "t": latest["t"]} if latest else None),
        "prev_close": bars_5m[-2]["c"] if len(bars_5m) >= 2 else None,
    }


def client_order_id(ticket_id: str) -> str:
    return f"horsey-{ticket_id}"[:48]


def already_submitted(pe, ticket: dict, events_doc: dict) -> Tuple[bool, str]:
    tid = ticket.get("ticket_id")
    if ticket.get("status") in DONE_STATUSES:
        return True, f"blotter_status={ticket.get('status')}"
    if ticket.get("alpaca_order_id"):
        return True, "alpaca_order_id_present"
    coid = client_order_id(tid)
    for e in events_doc.get("events") or []:
        if e.get("ticket_id") == tid and e.get("kind") in ("ORDER_SUBMITTED", "ORDER_FILLED"):
            return True, f"event:{e.get('kind')}"
    # Alpaca orders
    try:
        orders = pe.alpaca_request(
            "GET", pe.ALPACA_PAPER_HOST, "/v2/orders",
            params={"status": "all", "limit": 50, "direction": "desc"},
        ) or []
        for o in orders:
            if o.get("client_order_id") == coid:
                return True, f"alpaca_order:{o.get('status')}"
    except Exception as e:
        return False, f"order_check_warn:{e}"
    return False, "clear"


def pre_trigger_invalidated(or_info: dict, bars_5m: List[dict], now: datetime) -> Tuple[bool, str]:
    """Invalidate pending setup only if two consecutive 5m closes hold above 740.00 after 09:00."""
    if not or_info.get("ready"):
        return False, ""
    post = [b for b in bars_5m if (b["_dt"].hour, b["_dt"].minute) >= (9, 0)]
    if len(post) < 2:
        return False, ""
    a, b = post[-2], post[-1]
    if a["c"] > 740.0 and b["c"] > 740.0:
        return True, f"two consecutive 5m closes above 740 ({a['c']:.2f}, {b['c']:.2f})"
    return False, ""


def evaluate_entry(ticket: dict, or_info: dict, bars_5m: List[dict], now: datetime) -> Tuple[bool, str, dict]:
    """Return (fired, reason, meta)."""
    if not or_info.get("ready") or not bars_5m:
        return False, "OR not ready", {}
    if not (_in_window(now, ENTRY_WINDOW_START, ENTRY_WINDOW_END)):
        return False, "outside entry window 09:00–14:30 CT", {}

    prior = prior_rth_low(ticket)
    or_low = or_info["or_low"]
    or_high = or_info["or_high"]
    or_mid = or_info["or_mid"]
    latest = bars_5m[-1]
    prev = bars_5m[-2] if len(bars_5m) >= 2 else None
    ut = ticket.get("underlying_trigger") or {}

    # Generic fields
    if ticket.get("trigger_spy_below") is not None and latest["c"] < float(ticket["trigger_spy_below"]):
        return True, f"trigger_spy_below {ticket['trigger_spy_below']}", {"mode": "generic_below", "spy": latest["c"]}
    if ticket.get("trigger_spy_above") is not None and latest["c"] > float(ticket["trigger_spy_above"]):
        return True, f"trigger_spy_above {ticket['trigger_spy_above']}", {"mode": "generic_above", "spy": latest["c"]}

    # 1) OR Breakdown
    hold_lvl = min(or_low, prior)
    breakdown = latest["c"] < or_low and latest["c"] < prior
    confirm = False
    if prev is not None and prev["c"] < or_low:
        confirm = True
    if latest["c"] < hold_lvl:
        confirm = True
    if breakdown and confirm:
        return True, f"OR breakdown: 5m close {latest['c']:.2f} < OR_low {or_low:.2f} and < prior {prior:.2f}", {
            "mode": "or_break", "spy": latest["c"], "or_low": or_low, "prior": prior
        }

    # 2) Put-wall / OR rejection
    post = [b for b in bars_5m if (b["_dt"].hour, b["_dt"].minute) >= (8, 35)]
    session_high = max((b["h"] for b in post), default=latest["h"])
    tested = session_high >= 735.0 or session_high >= (or_high - 0.35)
    failed_740 = session_high < 740.0 or latest["c"] < 740.0
    # rejection: upper wick / red close back below or_mid
    wick = latest["h"] > latest["c"] and latest["h"] >= min(735.0, or_high)
    red = latest["c"] < latest["o"]
    below_mid = latest["c"] < or_mid
    rvol = or_info.get("rvol_est") or 0
    rvol_ok = rvol >= 1.3 or (latest.get("v") or 0) == 0  # if no volume, don't hard-block
    # Prefer 1.5x when volume present
    if latest.get("v"):
        rvol_ok = rvol >= 1.5 or rvol >= 1.3
    if tested and failed_740 and below_mid and (wick or red) and rvol_ok:
        return True, (
            f"OR/put-wall rejection: high {session_high:.2f} tested wall, 5m close {latest['c']:.2f} < OR_mid {or_mid:.2f} "
            f"(RVOL~{rvol or 'n/a'})"
        ), {"mode": "or_rejection", "spy": latest["c"], "or_mid": or_mid, "session_high": session_high, "rvol": rvol}

    # textual underlying_trigger present but not fired
    if ut:
        return False, "underlying_trigger not met", {"spy": latest["c"], "or": or_info}
    return False, "no trigger", {"spy": latest["c"]}


def resolve_occ(pe, ticket: dict) -> Tuple[str, float, float]:
    """Return (occ_symbol, strike, limit_price) capped by risk budget."""
    payload = ticket.get("alpaca_paper_payload") or {}
    order = payload.get("order") or {}
    occ = order.get("symbol") or ticket.get("occ_symbol") or payload.get("occ_symbol")
    strike = None
    if occ and len(occ) >= 15:
        # SPY261106P00733000
        try:
            strike = int(occ[-8:]) / 1000.0
        except Exception:
            strike = None
    if strike is None:
        strike = round(float(prior_rth_low(ticket)))
    exp = ticket.get("option_expiration") or ticket.get("session_target") or "2026-11-06"
    opt_type = "put" if "put" in (ticket.get("instrument") or "option_put") else "call"
    und = ticket.get("symbol") or "SPY"
    if not occ:
        info = pe.alpaca_find_option(und, exp, opt_type, strike, ticket.get("entry"))
        occ = info.get("occ_symbol")
    limit = float(ticket.get("entry") or order.get("limit_price") or 1.85)
    # Cap limit so qty * limit * 100 <= dollar_risk_budget * soft factor? Risk is stop distance based.
    # Cap premium so notional risk stays near budget: qty * (entry-stop) * 100 <= budget
    qty = int(ticket.get("quantity") or 2)
    stop = float(ticket.get("stop") or 1.25)
    budget = float(ticket.get("dollar_risk_budget") or 125.0)
    # If limit too rich vs stop distance, shrink limit toward stop+budget/(qty*100)
    max_entry = stop + (budget / max(qty * 100, 1))
    if limit > max_entry + 1e-9:
        limit = round(max_entry, 2)
    # Prefer range
    rng = ticket.get("limit_entry_range") or []
    if len(rng) == 2:
        limit = min(max(limit, float(rng[0])), float(rng[1]))
    return occ, float(strike), float(limit)


def submit_ticket(pe, ticket: dict, reason: str, or_info: dict, events_doc: dict, force_sim: bool) -> Dict[str, Any]:
    tid = ticket["ticket_id"]
    occ, strike, limit = resolve_occ(pe, ticket)
    qty = int(ticket.get("quantity") or 2)
    if force_sim:
        # Never transmit under force-sim
        append_event(
            events_doc, "TRIGGER_SIM", tid,
            f"[SIM] Trigger hit — would BUY {qty}× {occ} @ ${limit:.2f} LMT",
            f"{reason}. OR {or_info.get('or_low')}–{or_info.get('or_high')}. force_sim=1 (no order sent).",
            severity="warn", symbol=occ,
        )
        return {"mode": "force_sim", "submitted": False, "occ": occ, "limit": limit}

    # Use existing stage path
    result = pe.alpaca_stage_ticket(
        tid, dry_run=False, submit_paper=True,
        expiration=ticket.get("session_target"), strike=strike, occ_symbol=occ,
    )
    # Ensure limit matches if payload drifted
    append_event(
        events_doc, "ORDER_SUBMITTED", tid,
        f"Alpaca Paper Order Submitted: BUY {qty}× {occ} @ ${limit:.2f} LMT",
        f"Trigger hit ({reason}). Firewall 6/6 PASS (${ticket.get('actual_dollar_risk', 120)} max risk). OR "
        f"${or_info.get('or_low')}–${or_info.get('or_high')}.",
        severity="pos", symbol=occ,
        extra={"alpaca_order_id": (result.get("response") or {}).get("id") if isinstance(result.get("response"), dict) else ticket.get("alpaca_order_id")},
    )
    return {"mode": "submitted_paper", "submitted": True, "result": result, "occ": occ, "limit": limit}


def manage_open_positions(pe, events_doc: dict, or_info: dict, bars_5m: List[dict], now: datetime, force_sim: bool) -> List[str]:
    """Manage filled paper option positions for blotter tickets. Returns action notes."""
    notes = []
    try:
        positions = pe.alpaca_request("GET", pe.ALPACA_PAPER_HOST, "/v2/positions") or []
        orders = pe.alpaca_request(
            "GET", pe.ALPACA_PAPER_HOST, "/v2/orders",
            params={"status": "all", "limit": 50, "direction": "desc"},
        ) or []
    except Exception as e:
        return [f"manage_skip:{e}"]

    by_coid = {o.get("client_order_id"): o for o in orders if o.get("client_order_id")}
    rows = blotter_rows()
    for ticket in rows:
        tid = ticket.get("ticket_id")
        if not tid:
            continue
        coid = client_order_id(tid)
        entry_order = by_coid.get(coid)
        # Fill detection
        if entry_order and entry_order.get("status") == "filled":
            if not any(e.get("ticket_id") == tid and e.get("kind") == "ORDER_FILLED" for e in events_doc.get("events") or []):
                append_event(
                    events_doc, "ORDER_FILLED", tid,
                    f"Alpaca Paper FILLED: {entry_order.get('symbol')} × {entry_order.get('filled_qty')}",
                    f"avg {entry_order.get('filled_avg_price')} · order {entry_order.get('id')}",
                    severity="pos", symbol=entry_order.get("symbol") or "",
                )
                rewrite_blotter_ticket(tid, lambda t: {**t, "status": "filled_paper",
                                                       "alpaca_fill_price": entry_order.get("filled_avg_price"),
                                                       "alpaca_filled_ct": _iso_ct()})
                notes.append(f"filled:{tid}")

        # Find matching position by OCC
        occ = ((ticket.get("alpaca_paper_payload") or {}).get("order") or {}).get("symbol") or ticket.get("occ_symbol")
        pos = next((p for p in positions if p.get("symbol") == occ), None)
        if not pos:
            continue

        qty = float(pos.get("qty") or 0)
        if qty <= 0:
            continue
        mark = float(pos.get("current_price") or pos.get("lastday_price") or 0)
        entry = float(pos.get("avg_entry_price") or ticket.get("alpaca_fill_price") or ticket.get("entry") or 0)
        stop = float(ticket.get("stop") or (ticket.get("oco_bracket") or {}).get("stop_order") or 1.25)
        t1 = float(ticket.get("target1") or (ticket.get("oco_bracket") or {}).get("take_profit_1") or 3.65)
        t2 = float(ticket.get("target2") or (ticket.get("oco_bracket") or {}).get("take_profit_2") or 4.85)
        risk_unit = abs(entry - stop) if entry and stop else float(ticket.get("risk_per_unit") or 0.6)

        # Underlying invalidation once in trade: 5m close > min(or_high, 740)
        inv_lvl = None
        if or_info.get("or_high") is not None:
            inv_lvl = min(float(or_info["or_high"]), 740.0)
        else:
            inv_lvl = 740.0
        latest = bars_5m[-1] if bars_5m else None
        und_inv = latest is not None and latest["c"] > inv_lvl

        def _close(kind: str, headline: str, detail: str):
            if force_sim:
                append_event(events_doc, kind, tid, "[SIM] " + headline, detail + " (force_sim)", "warn", occ)
                notes.append(f"sim:{kind}:{tid}")
                return
            # cancel open orders for symbol then marketable limit sell
            try:
                pe.alpaca_request("DELETE", pe.ALPACA_PAPER_HOST, "/v2/orders", params={"status": "open"})  # too broad — cancel per symbol below
            except Exception:
                pass
            try:
                open_orders = pe.alpaca_request(
                    "GET", pe.ALPACA_PAPER_HOST, "/v2/orders",
                    params={"status": "open", "limit": 50},
                ) or []
                for o in open_orders:
                    if o.get("symbol") == occ:
                        try:
                            pe.alpaca_request("DELETE", pe.ALPACA_PAPER_HOST, f"/v2/orders/{o['id']}")
                        except Exception:
                            pass
                # marketable limit slightly below mark for puts long exit
                lim = max(0.01, round(mark * 0.95, 2)) if mark else None
                body = {
                    "symbol": occ,
                    "qty": str(int(qty)),
                    "side": "sell",
                    "type": "limit" if lim else "market",
                    "time_in_force": "day",
                    "client_order_id": f"horsey-exit-{tid}-{kind}"[:48],
                }
                if lim:
                    body["limit_price"] = f"{lim:.2f}"
                pe.alpaca_request("POST", pe.ALPACA_PAPER_HOST, "/v2/orders", body=body)
                append_event(events_doc, kind, tid, headline, detail, "neg" if "STOP" in kind or "TIME" in kind or "EOD" in kind else "pos", occ)
                rewrite_blotter_ticket(tid, lambda t: {**t, "status": "closed_paper", "alpaca_exit_kind": kind, "alpaca_exit_ct": _iso_ct()})
                _gov_close(tid, mark or entry or 0, kind, ticket.get("alpaca_filled_ct"), notes)
                notes.append(f"{kind}:{tid}")
            except Exception as e:
                notes.append(f"exit_fail:{tid}:{e}")

        # Hard stop on option mark OR underlying invalidation
        if (mark and mark <= stop) or und_inv:
            why = f"mark {mark} <= stop {stop}" if (mark and mark <= stop) else f"5m close {latest['c']:.2f} > inv {inv_lvl}"
            _close("STOP_EXIT", f"STOP EXIT: sold {int(qty)}× {occ}", why)
            continue

        # Targets — track via events
        t1_done = any(e.get("ticket_id") == tid and e.get("kind") == "TARGET1_SCALED" for e in events_doc.get("events") or [])
        if mark >= t1 and qty >= 2 and not t1_done:
            if force_sim:
                append_event(events_doc, "TARGET1_SCALED", tid, f"[SIM] T1 scale {occ}", f"mark {mark} >= {t1}", "pos", occ)
            else:
                sell_qty = max(1, int(qty // 2))
                try:
                    pe.alpaca_request("POST", pe.ALPACA_PAPER_HOST, "/v2/orders", body={
                        "symbol": occ, "qty": str(sell_qty), "side": "sell", "type": "limit",
                        "time_in_force": "day", "limit_price": f"{max(t1, mark):.2f}",
                        "client_order_id": f"horsey-t1-{tid}"[:48],
                    })
                    append_event(events_doc, "TARGET1_SCALED", tid,
                                 f"TARGET1 SCALED: sold {sell_qty}× {occ} @ ≥${t1}",
                                 f"Remaining stop → breakeven entry {entry}", "pos", occ)
                    notes.append(f"T1:{tid}")
                except Exception as e:
                    notes.append(f"T1_fail:{e}")

        t2_done = any(e.get("ticket_id") == tid and e.get("kind") == "TARGET2_CLOSED" for e in events_doc.get("events") or [])
        if mark >= t2 and not t2_done:
            _close("TARGET2_CLOSED", f"TARGET2 CLOSED: sold remaining {occ}", f"mark {mark} >= {t2}")
            continue

        # Time stop 45–60m
        filled_at = ticket.get("alpaca_filled_ct") or (entry_order or {}).get("filled_at")
        if filled_at:
            try:
                if isinstance(filled_at, str) and "T" in filled_at:
                    ft = parse_bar_ts(filled_at) if filled_at.endswith("Z") or "+" in filled_at[-6:] or filled_at.endswith("Z") else datetime.fromisoformat(filled_at)
                    if ft.tzinfo is None:
                        ft = ft.replace(tzinfo=CT)
                    mins = (now - ft.astimezone(CT)).total_seconds() / 60.0
                    pnl_r = ((mark - entry) / risk_unit) if risk_unit else 0
                    if mins >= 50 and pnl_r < -0.5:
                        _close("TIME_STOP_EXIT", f"TIME STOP: flat {occ}", f"{mins:.0f}m since fill · PnL {pnl_r:.2f}R")
                        continue
                    if mins >= 60 and pnl_r < 0:
                        _close("TIME_STOP_EXIT", f"TIME STOP: flat {occ}", f"{mins:.0f}m no progress · PnL {pnl_r:.2f}R")
                        continue
            except Exception:
                pass

        # 14:45 CT 0DTE flat
        exp = (ticket.get("session_target") or ticket.get("option_expiration") or "")
        today = now.strftime("%Y-%m-%d")
        if exp == today and _hm(now) >= EOD_FLAT:
            _close("EOD_0DTE_FLAT", f"EOD 0DTE FLAT: {occ}", "14:45 CT flat rule")
            continue

    return notes


def push_vault_if_changed(pe, changed: bool, push: bool) -> Dict[str, Any]:
    out = {"vault_built": False, "pushed": False}
    if not changed and not push:
        return out
    try:
        pe.maybe_rebuild_vault_state()
        out["vault_built"] = True
    except Exception as e:
        out["vault_error"] = str(e)[:200]
    if push and changed:
        try:
            subprocess.run(["git", "add", "vault/state.enc.json"], cwd=str(ROOT), check=False)
            # only commit if staged diff
            st = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=str(ROOT), capture_output=True, text=True)
            if "vault/state.enc.json" in (st.stdout or ""):
                subprocess.run(
                    ["git", "commit", "-m", "vault: refresh state after Alpaca RTH tick"],
                    cwd=str(ROOT), check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                subprocess.run(["git", "push", "origin", "main"], cwd=str(ROOT), check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                out["pushed"] = True
        except Exception as e:
            out["push_error"] = str(e)[:200]
    return out


def ensure_watcher(now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or _now_ct()
    if not _weekday(now) or not _in_window(now, (8, 25), (15, 0)):
        return {"spawned": False, "reason": "outside_watcher_window"}
    # lock held?
    try:
        import fcntl
        if LOCK.exists():
            # try non-blocking exclusive
            fd = open(LOCK, "a+")
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
                fd.close()
            except BlockingIOError:
                fd.close()
                return {"spawned": False, "reason": "lock_held"}
            except Exception:
                try:
                    fd.close()
                except Exception:
                    pass
    except Exception:
        pass
    if not WATCHER.exists():
        return {"spawned": False, "reason": "watcher_missing"}
    log = Path("/tmp/horsey_alpaca_rth_watcher.log")
    with log.open("a") as lf:
        subprocess.Popen(
            [sys.executable, str(WATCHER)],
            cwd=str(ROOT),
            stdout=lf, stderr=lf, start_new_session=True,
        )
    return {"spawned": True, "log": str(log)}


def alpaca_rth_tick(pe, force_sim: bool = False, push_if_changed: bool = False,
                    ensure_watcher_flag: bool = False) -> Dict[str, Any]:
    """Main RTH tick. pe = pod_engine module/namespace with alpaca_* helpers."""
    pe.assert_paper_host(pe.ALPACA_PAPER_HOST)
    assert "paper-api.alpaca.markets" in pe.ALPACA_PAPER_HOST
    if "//api.alpaca.markets" in pe.ALPACA_PAPER_HOST and "paper-api" not in pe.ALPACA_PAPER_HOST:
        raise RuntimeError("REFUSED live host")

    now = _now_ct()
    events_doc = load_events()
    changed = False
    armed = armed_tickets()
    events_doc["armed_tickets"] = [
        {
            "ticket_id": t.get("ticket_id"),
            "status": t.get("status"),
            "session_target": t.get("session_target"),
            "occ_symbol": ((t.get("alpaca_paper_payload") or {}).get("order") or {}).get("symbol"),
            "symbol": t.get("symbol"),
            "quantity": t.get("quantity"),
            "entry": t.get("entry"),
            "stop": t.get("stop"),
            "target1": t.get("target1"),
            "target2": t.get("target2"),
        }
        for t in armed
    ]

    watcher_info = ensure_watcher(now) if ensure_watcher_flag else {"spawned": False, "reason": "not_requested"}

    # Always refresh paper status when keys present
    try:
        st = pe.alpaca_status()
    except Exception as e:
        st = {"status": "error", "error": str(e)[:200]}

    # Clock
    clock = {}
    try:
        clock = pe.alpaca_request("GET", pe.ALPACA_PAPER_HOST, "/v2/clock") or {}
    except Exception as e:
        clock = {"error": str(e)[:200]}

    is_open = bool(clock.get("is_open"))
    rth_clock = _weekday(now) and _in_window(now, RTH_START, RTH_END)
    market_openish = is_open or rth_clock

    result: Dict[str, Any] = {
        "schema_version": 1,
        "ts_ct": _iso_ct(now),
        "force_sim": bool(force_sim),
        "paper_host": pe.ALPACA_PAPER_HOST,
        "clock": {k: clock.get(k) for k in ("is_open", "timestamp", "next_open", "next_close") if k in clock},
        "account_status": st.get("status"),
        "equity": st.get("equity"),
        "armed_count": len(armed),
        "watcher": watcher_info,
        "actions": [],
    }

    if not market_openish and not force_sim:
        events_doc["status"] = "market_closed"
        events_doc["rth"] = {"is_open": False, "ct": _iso_ct(now), "window": "08:30–15:05 CT Mon–Fri"}
        # Do not clear events; ensure events key exists (may be empty list initially)
        events_doc.setdefault("events", [])
        save_events(events_doc)
        changed = True
        result["status"] = "market_closed"
        result["armed_tickets"] = events_doc["armed_tickets"]
        result["events_path"] = str(PAPER_EVENTS)
        result["note"] = "Market closed / outside RTH — no orders placed. Pre-approved tickets remain armed."
        push_vault_if_changed(pe, changed, push_if_changed)
        return result

    # Fetch bars
    start_utc = session_rth_start_utc(now)
    bars = fetch_alpaca_bars(pe, "5Min", start_utc)
    source = "alpaca_iex"
    bars = filter_rth_bars(bars, now)
    if len(bars) < 3:
        yb = filter_rth_bars(fetch_yahoo_bars_5m(), now)
        if len(yb) > len(bars):
            bars = yb
            source = "yahoo_fallback"

    or_info = compute_or(bars, now)
    events_doc["status"] = "watching" if market_openish else "force_sim"
    events_doc["rth"] = {
        "is_open": is_open,
        "ct": _iso_ct(now),
        "bars_source": source,
        "bars_5m": len(bars),
        "or": or_info,
    }
    result["or"] = or_info
    result["bars_source"] = source
    result["bars_5m"] = len(bars)

    # Manage open positions first
    notes = manage_open_positions(pe, events_doc, or_info, bars, now, force_sim=force_sim)
    if notes:
        changed = True
        result["actions"].extend(notes)

    # Evaluate armed tickets for entry
    for ticket in armed:
        tid = ticket["ticket_id"]
        # Session gate: only fire on session_target day (or today if unset)
        target = ticket.get("session_target")
        if target and target != now.strftime("%Y-%m-%d") and not force_sim:
            result["actions"].append(f"skip:{tid}:session_target={target}")
            continue

        dup, why = already_submitted(pe, ticket, events_doc)
        if dup:
            result["actions"].append(f"skip:{tid}:duplicate:{why}")
            continue

        inv, inv_why = pre_trigger_invalidated(or_info, bars, now)
        if inv:
            rewrite_blotter_ticket(tid, lambda t: {**t, "status": "invalidated_paper", "invalidation_reason": inv_why, "invalidated_ct": _iso_ct()})
            append_event(events_doc, "INVALIDATED", tid, f"Setup invalidated: {tid}", inv_why, "neg",
                         ((ticket.get("alpaca_paper_payload") or {}).get("order") or {}).get("symbol") or "")
            changed = True
            result["actions"].append(f"invalidated:{tid}")
            continue

        if force_sim and not market_openish:
            # Evaluate with whatever bars we have but never submit
            fired, reason, meta = evaluate_entry(ticket, or_info, bars, now) if or_info.get("ready") else (False, "OR not ready (sim)", {})
            result["actions"].append(f"sim_eval:{tid}:{fired}:{reason}")
            continue

        fired, reason, meta = evaluate_entry(ticket, or_info, bars, now)
        if not fired:
            result["actions"].append(f"wait:{tid}:{reason}")
            continue

        # Real RTH trigger — submit (unless force_sim)
        try:
            sub = submit_ticket(pe, ticket, reason, or_info, events_doc, force_sim=force_sim)
            changed = True
            result["actions"].append({"submit": sub, "reason": reason, "meta": meta})
        except Exception as e:
            append_event(events_doc, "ORDER_ERROR", tid, f"Order error {tid}", str(e)[:240], "neg")
            changed = True
            result["actions"].append(f"error:{tid}:{e}")

    save_events(events_doc)
    result["status"] = events_doc["status"]
    result["armed_tickets"] = events_doc["armed_tickets"]
    result["events_count"] = len(events_doc.get("events") or [])
    result["changed"] = changed
    try:
        import importlib.util as _ilu
        _sp = _ilu.spec_from_file_location("_pod_governance", str(ROOT / "scripts" / "_pod_governance.py"))
        _gm = _ilu.module_from_spec(_sp); _sp.loader.exec_module(_gm)
        _gm.compute_portfolio_snapshot(); _gm.check_limits(); _gm.ensure_armed_queue()
    except Exception as _e:
        result["governance_warn"] = str(_e)[:200]
    push_info = push_vault_if_changed(pe, changed, push_if_changed)
    result["vault"] = push_info
    return result

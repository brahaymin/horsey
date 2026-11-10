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


# ---------------------------------------------------------------------------
# Per-ticket rule parsing (Nov 10 2026 fix). Every trigger, invalidation,
# window and time stop comes from the ticket's own blotter fields. There are
# no session-specific constants. If a ticket can't be parsed, rules["ok"] is
# False and the tick leaves it pending, never submitting or invalidating it.
# ---------------------------------------------------------------------------
import re as _re

_NUM = r"\$?(\d{2,4}(?:\.\d+)?)"
_RANGE = _NUM + r"\s*(?:-|–|to)\s*" + _NUM


def _hhmm(s: Any) -> Optional[Tuple[int, int]]:
    if not s:
        return None
    m = _re.search(r"(\d{1,2}):(\d{2})", str(s))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _hold_bars(text: str, default: int = 1) -> int:
    """'(5-min hold)' -> 1 bar, '10-min hold' -> 2, 'sustained' -> 2, '5-15m hold' -> 1."""
    t = text or ""
    m = _re.search(r"(\d+)\s*(?:-\s*\d+\s*)?(?:m|min|minute)s?\b[^.;]*?\bhold", t, _re.I) or \
        _re.search(r"(\d+)\s*-?\s*min(?:ute)?\s+hold", t, _re.I)
    if m:
        return max(1, int(m.group(1)) // 5)
    if _re.search(r"\bsustained\b", t, _re.I):
        return 2
    return default


def _parse_rvol(ut: dict, *texts: str) -> Optional[float]:
    if ut.get("rvol_min") is not None:
        try:
            return float(ut["rvol_min"])
        except Exception:
            pass
    for t in texts:
        m = _re.search(r"RVOL\s*(?:>=|≥|>)\s*(\d+(?:\.\d+)?)\s*x?", t or "", _re.I)
        if m:
            return float(m.group(1))
    return None


def _parse_hard_inv(text: str, direction: str, entry: Optional[float], anchor: Optional[float] = None) -> Optional[dict]:
    """In-trade underlying invalidation from hard_invalidation_underlying.

    The adverse side is 'below' for longs and 'above' for shorts. When a ticket
    gives several levels ('755.00 / 752.70'), the one closest to entry wins, which
    is the most protective. Segments split on ';' let a two-leg ticket (wall fade)
    pick the leg that mentions its own wall (anchor)."""
    if not text:
        return None
    adverse = "below" if direction == "long" else "above"
    segs = [s for s in _re.split(r";", text) if s.strip()]
    if anchor is not None and len(segs) > 1:
        tagged = [s for s in segs if _re.search(r"\bfor\s+%s\b" % int(round(anchor)), s)]
        if tagged:
            segs = tagged
    cands = []
    for seg in segs:
        for m in _re.finditer(r"\b(above|below)\s+" + _NUM + r"(?:\s*/\s*" + _NUM + r")?", seg, _re.I):
            if m.group(1).lower() != adverse:
                continue
            lv = [float(m.group(2))] + ([float(m.group(3))] if m.group(3) else [])
            cands.append((lv, _hold_bars(seg, 1)))
    if not cands:
        return None
    levels = [x for lv, _ in cands for x in lv]
    bars = max(b for _, b in cands)
    if entry is not None:
        level = min(levels, key=lambda x: abs(x - float(entry)))
    else:
        level = max(levels) if adverse == "below" else min(levels)
    return {"side": adverse, "level": level, "bars": bars, "levels": levels}


def _parse_time_stop(ts: str) -> dict:
    out: Dict[str, Any] = {}
    t = ts or ""
    m = _re.search(r"(\d+)\s*(?:-\s*(\d+))?\s*m(?:in)?\b\s*if\s*(?:<\s*(-?\d*\.?\d+)\s*R|no follow-through)", t, _re.I)
    if m:
        out["minutes"] = int(m.group(2) or m.group(1))
        out["min_progress_r"] = float(m.group(3)) if m.group(3) is not None else 0.0
    m = _re.search(r"no new risk after\s*(\d{1,2}:\d{2})", t, _re.I)
    if m:
        out["entry_end"] = _hhmm(m.group(1))
    m = _re.search(r"flat\s*(?:0DTE\s*)?by\s*(\d{1,2}:\d{2})", t, _re.I)
    if m:
        out["eod_flat"] = _hhmm(m.group(1))
    return out


def parse_ticket_rules(ticket: dict) -> Dict[str, Any]:
    """Normalise one blotter ticket into deterministic rules.

    Modes:
      legacy_or     : Friday Nov-6 style (or_break / or_rejection + put_wall_zone).
                      Keeps the original behaviour, with the wall taken from the
                      ticket's put_wall_zone instead of a constant.
      generic       : trigger_spy_below / trigger_spy_above numeric fields.
      acceptance    : 'acceptance above|below X (N-min hold)', a breakout or breakdown.
      wall_fade     : 'into A-B that FAILS|HOLDS (close back below|above C)'.
    """
    tid = ticket.get("ticket_id") or "?"
    ut = ticket.get("underlying_trigger") or {}
    if not isinstance(ut, dict):
        ut = {"text": str(ut)}
    direction = (ticket.get("direction") or "").lower()
    gate = str(ticket.get("cio_premarket_gate") or "")
    ts = _parse_time_stop(str(ticket.get("time_stop") or ""))
    entry = ticket.get("entry")
    try:
        entry_f = float(entry) if entry is not None else None
    except Exception:
        entry_f = None
    r: Dict[str, Any] = {
        "ticket_id": tid, "ok": False, "mode": None, "direction": direction,
        "warnings": [], "entry": entry_f,
        "stop": ticket.get("stop"), "target1": ticket.get("target1"), "target2": ticket.get("target2"),
        "entry_start": _hhmm(ticket.get("no_entry_before_ct")) or ENTRY_WINDOW_START,
        "entry_end": ts.get("entry_end") or ENTRY_WINDOW_END,
        "eod_flat": ts.get("eod_flat") or EOD_FLAT,
        "time_stop": ts, "pre_invalidation": None, "inv": None, "retest": None, "fill_guard": None,
    }
    m = _re.search(r"no entry before\s*(\d{1,2}:\d{2})", gate, _re.I)
    if m and not ticket.get("no_entry_before_ct"):
        r["entry_start"] = _hhmm(m.group(1))

    # legacy Friday OR/put-wall ticket
    if ("or_break" in ut or "or_rejection" in ut) and isinstance(ut.get("put_wall_zone"), (list, tuple)) \
            and len(ut["put_wall_zone"]) == 2:
        lo, hi = float(ut["put_wall_zone"][0]), float(ut["put_wall_zone"][1])
        r.update({"ok": True, "mode": "legacy_or", "wall_lo": lo, "wall_hi": hi,
                  "pre_invalidation": {"side": "above", "level": hi, "bars": 2},
                  "inv": {"side": "above", "level": hi, "bars": 1, "or_high_cap": True},
                  "entry_start": ENTRY_WINDOW_START, "entry_end": ENTRY_WINDOW_END, "eod_flat": EOD_FLAT,
                  "time_stop": {"legacy": True}})
        return r

    # generic numeric triggers
    if ticket.get("trigger_spy_below") is not None or ticket.get("trigger_spy_above") is not None:
        r.update({"ok": True, "mode": "generic"})
        return r

    if direction not in ("long", "short"):
        r["warnings"].append(f"{tid}: direction '{direction}' not long/short")
        return r

    # choose the primary trigger text deterministically
    text, key = None, None
    for k in ("primary", "breakout", "breakdown", "failure", "trigger", "text"):
        if isinstance(ut.get(k), str) and ut[k].strip():
            text, key = ut[k], k
            break
    if not text:
        r["warnings"].append(f"{tid}: no recognised underlying_trigger text (keys={sorted(ut.keys())})")
        return r
    r["trigger_key"] = key
    r["rvol_min"] = _parse_rvol(ut, text, gate)

    fade = _re.search(r"into\s+" + _RANGE + r"\s+that\s+(FAILS|HOLDS)\s*\(\s*close back\s+(below|above)\s+" + _NUM,
                      text, _re.I)
    acc = _re.search(r"acceptance\s+(above|below)\D*?" + _NUM, text, _re.I)
    if fade:
        a, b = sorted([float(fade.group(1)), float(fade.group(2))])
        verb, side, conf = fade.group(3).upper(), fade.group(4).lower(), float(fade.group(5))
        leg_dir = "short" if (verb == "FAILS" and side == "below") else ("long" if (verb == "HOLDS" and side == "above") else None)
        if leg_dir != direction:
            r["warnings"].append(f"{tid}: wall-fade leg direction {leg_dir} != ticket direction {direction}")
            return r
        r.update({"mode": "wall_fade", "zone": [a, b], "confirm_side": side, "confirm_level": conf})
        anchor = round(b) if direction == "short" else round(a)
        r["anchor"] = anchor
        if key == "primary" and isinstance(ut.get("alternate"), str):
            r["warnings"].append(f"{tid}: alternate leg ignored (ticket direction {direction}; not run live)")
    elif acc:
        side, lvl = acc.group(1).lower(), float(acc.group(2))
        if (side == "above") != (direction == "long"):
            r["warnings"].append(f"{tid}: acceptance {side} conflicts with direction {direction}")
            return r
        r.update({"mode": "acceptance", "level": lvl, "side": side, "hold_bars": _hold_bars(text, 1)})
        anchor = lvl
    else:
        r["warnings"].append(f"{tid}: unparseable underlying_trigger.{key}: {text[:120]!r}")
        return r

    if r["rvol_min"] is None:
        r["warnings"].append(f"{tid}: no RVOL threshold found")
        return r

    # CIO gate refinements, all optional
    m = _re.search(r"(?:rallies|trades)\s+back\s+(?:into|to)\s+" + _RANGE + r"\s+and\s+rejects", gate, _re.I)
    if m:
        a, b = sorted([float(m.group(1)), float(m.group(2))])
        r["retest"] = [a, b]
    m = _re.search(r"skip if a fill would be\s+(below|above)\s+" + _NUM, gate, _re.I)
    if m:
        r["fill_guard"] = {"side": m.group(1).lower(), "level": float(m.group(2))}

    inv = _parse_hard_inv(str(ticket.get("hard_invalidation_underlying") or ""), direction, entry_f,
                          anchor if r["mode"] == "wall_fade" else None)
    if inv is None:
        r["warnings"].append(f"{tid}: hard_invalidation_underlying not parsed; broker stop only")
    r["inv"] = inv
    if "minutes" not in ts:
        r["warnings"].append(f"{tid}: time_stop minutes not parsed; EOD flat only")
    r["ok"] = True
    return r


def _consec(closes: List[float], side: str, level: float, n: int) -> bool:
    if n < 1 or len(closes) < n:
        return False
    tail = closes[-n:]
    return all((c > level) if side == "above" else (c < level) for c in tail)


def pre_trigger_invalidated(or_info: dict, bars_5m: List[dict], now: datetime,
                            rules: Optional[dict] = None) -> Tuple[bool, str]:
    """Invalidate a pending setup only if the ticket's own rules say so.

    Only legacy_or tickets carry a pre-trigger rule: two consecutive 5m closes above
    their put_wall_zone top, which is 740.00 for the Friday Nov-6 ticket. Tickets whose
    hard_invalidation_underlying describes an in-trade condition, or that have no rule,
    are never invalidated pre-entry. Called without rules (old call sites), it never
    invalidates."""
    if not rules or not rules.get("ok") or not rules.get("pre_invalidation"):
        return False, ""
    if not or_info.get("ready"):
        return False, ""
    pi = rules["pre_invalidation"]
    post = [b for b in bars_5m if (b["_dt"].hour, b["_dt"].minute) >= (9, 0)]
    n = int(pi.get("bars") or 2)
    if len(post) < n:
        return False, ""
    closes = [b["c"] for b in post]
    if _consec(closes, pi["side"], float(pi["level"]), n):
        lvl = float(pi["level"])
        lvl_s = f"{lvl:g}" if lvl == int(lvl) else f"{lvl:.2f}"
        tail = ", ".join(f"{c:.2f}" for c in closes[-n:])
        word = "two" if n == 2 else str(n)
        return True, f"{word} consecutive 5m closes {pi['side']} {lvl_s} ({tail})"
    return False, ""


def _evaluate_legacy(ticket: dict, or_info: dict, bars_5m: List[dict], rules: dict) -> Tuple[bool, str, dict]:
    """Original Nov-6 logic. Wall levels come from the ticket's put_wall_zone (735/740 on Friday)."""
    wall_lo, wall_hi = rules["wall_lo"], rules["wall_hi"]
    prior = prior_rth_low(ticket)
    or_low = or_info["or_low"]
    or_high = or_info["or_high"]
    or_mid = or_info["or_mid"]
    latest = bars_5m[-1]
    prev = bars_5m[-2] if len(bars_5m) >= 2 else None

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

    post = [b for b in bars_5m if (b["_dt"].hour, b["_dt"].minute) >= (8, 35)]
    session_high = max((b["h"] for b in post), default=latest["h"])
    tested = session_high >= wall_lo or session_high >= (or_high - 0.35)
    failed_wall = session_high < wall_hi or latest["c"] < wall_hi
    wick = latest["h"] > latest["c"] and latest["h"] >= min(wall_lo, or_high)
    red = latest["c"] < latest["o"]
    below_mid = latest["c"] < or_mid
    rvol = or_info.get("rvol_est") or 0
    rvol_ok = rvol >= 1.3 or (latest.get("v") or 0) == 0
    if latest.get("v"):
        rvol_ok = rvol >= 1.5 or rvol >= 1.3
    if tested and failed_wall and below_mid and (wick or red) and rvol_ok:
        return True, (
            f"OR/put-wall rejection: high {session_high:.2f} tested wall, 5m close {latest['c']:.2f} < OR_mid {or_mid:.2f} "
            f"(RVOL~{rvol or 'n/a'})"
        ), {"mode": "or_rejection", "spy": latest["c"], "or_mid": or_mid, "session_high": session_high, "rvol": rvol}
    return False, "underlying_trigger not met", {"spy": latest["c"], "or": or_info}


def evaluate_entry(ticket: dict, or_info: dict, bars_5m: List[dict], now: datetime,
                   rules: Optional[dict] = None) -> Tuple[bool, str, dict]:
    """Return (fired, reason, meta), driven by parse_ticket_rules(ticket)."""
    rules = rules or parse_ticket_rules(ticket)
    if not rules.get("ok"):
        return False, "unparsed: " + "; ".join(rules.get("warnings") or ["unknown"]), {"mode": "unparsed"}
    if not or_info.get("ready") or not bars_5m:
        return False, "OR not ready", {}
    es, ee = rules["entry_start"], rules["entry_end"]
    if not _in_window(now, es, ee):
        return False, f"outside entry window {es[0]:02d}:{es[1]:02d}–{ee[0]:02d}:{ee[1]:02d} CT", {}
    latest = bars_5m[-1]
    mode = rules["mode"]

    if mode == "generic":
        if ticket.get("trigger_spy_below") is not None and latest["c"] < float(ticket["trigger_spy_below"]):
            return True, f"trigger_spy_below {ticket['trigger_spy_below']}", {"mode": "generic_below", "spy": latest["c"]}
        if ticket.get("trigger_spy_above") is not None and latest["c"] > float(ticket["trigger_spy_above"]):
            return True, f"trigger_spy_above {ticket['trigger_spy_above']}", {"mode": "generic_above", "spy": latest["c"]}
        return False, "no trigger", {"spy": latest["c"]}

    if mode == "legacy_or":
        return _evaluate_legacy(ticket, or_info, bars_5m, rules)

    # New-style tickets require a measured RVOL (no volume means no fire).
    rvol = or_info.get("rvol_est")
    rvol_min = float(rules["rvol_min"])
    rvol_ok = rvol is not None and rvol >= rvol_min
    short = rules["direction"] == "short"
    meta = {"mode": mode, "spy": latest["c"], "rvol": rvol, "rvol_min": rvol_min}

    if mode == "acceptance":
        closes = [b["c"] for b in bars_5m if (b["_dt"].hour, b["_dt"].minute) >= (8, 30)]
        accepted = _consec(closes, rules["side"], rules["level"], int(rules["hold_bars"]))
        cond = f"{rules['hold_bars']}×5m close {rules['side']} {rules['level']:.2f}"
        if not accepted:
            return False, f"waiting: {cond} (last {latest['c']:.2f})", meta
    elif mode == "wall_fade":
        a, b = rules["zone"]
        tested = (latest["h"] >= a) if short else (latest["l"] <= b)
        conf = (latest["c"] < rules["confirm_level"]) if rules["confirm_side"] == "below" else (latest["c"] > rules["confirm_level"])
        cond = f"test {a:.2f}-{b:.2f} and close {rules['confirm_side']} {rules['confirm_level']:.2f}"
        if not (tested and conf):
            return False, f"waiting: {cond} (h {latest['h']:.2f} l {latest['l']:.2f} c {latest['c']:.2f})", meta
    else:
        return False, f"unknown mode {mode}", meta

    rt = rules.get("retest")
    if rt:
        a, b = rt
        if short:
            ok_rt = latest["h"] >= a and latest["c"] < b
        else:
            ok_rt = latest["l"] <= b and latest["c"] > a
        if not ok_rt:
            return False, f"waiting: CIO gate retest {a:.2f}-{b:.2f} and reject (h {latest['h']:.2f} c {latest['c']:.2f})", meta
    fg = rules.get("fill_guard")
    if fg:
        bad = (latest["c"] < fg["level"]) if fg["side"] == "below" else (latest["c"] > fg["level"])
        if bad:
            return False, f"skip: price {latest['c']:.2f} {fg['side']} fill guard {fg['level']:.2f}", meta
    if not rvol_ok:
        return False, f"waiting: RVOL {rvol if rvol is not None else 'n/a'} < {rvol_min}", meta
    return True, f"{mode}: {cond}; RVOL {rvol:.2f} ≥ {rvol_min}" + (" + CIO retest/reject" if rt else ""), meta


def decide_armed_ticket(ticket: dict, or_info: dict, bars_5m: List[dict], now: datetime) -> Dict[str, Any]:
    """Pure decision for one armed ticket (no I/O): unparsed | invalidate | fire | wait."""
    rules = parse_ticket_rules(ticket)
    if not rules.get("ok"):
        return {"action": "unparsed", "reason": "; ".join(rules.get("warnings") or ["unknown"]), "rules": rules}
    inv, why = pre_trigger_invalidated(or_info, bars_5m, now, rules)
    if inv:
        return {"action": "invalidate", "reason": why, "rules": rules}
    fired, reason, meta = evaluate_entry(ticket, or_info, bars_5m, now, rules)
    return {"action": "fire" if fired else "wait", "reason": reason, "meta": meta, "rules": rules}


def in_trade_invalidation(rules: dict, or_info: dict, bars_5m: List[dict]) -> Tuple[bool, str, Optional[float]]:
    """Underlying hard invalidation once in a trade, from the ticket's own rules."""
    inv = (rules or {}).get("inv")
    if not inv or not bars_5m:
        return False, "", None
    lvl = float(inv["level"])
    if inv.get("or_high_cap"):
        lvl = min(float(or_info["or_high"]), lvl) if or_info.get("or_high") is not None else lvl
    closes = [b["c"] for b in bars_5m]
    n = int(inv.get("bars") or 1)
    if _consec(closes, inv["side"], lvl, n):
        return True, f"{n}×5m close {inv['side']} inv {lvl:.2f} (last {closes[-1]:.2f})", lvl
    return False, "", lvl


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


def _is_stock(ticket: dict) -> bool:
    return not str(ticket.get("instrument") or "stock").startswith("option")


def submit_ticket(pe, ticket: dict, reason: str, or_info: dict, events_doc: dict, force_sim: bool) -> Dict[str, Any]:
    tid = ticket["ticket_id"]
    qty = int(ticket.get("quantity") or 2)
    if _is_stock(ticket):
        # Equity bracket path: pod_engine builds the order from the ticket's entry/stop/target1.
        # No option lookup, and sizing is unchanged.
        occ = (ticket.get("symbol") or "SPY").upper()
        strike = None
        limit = float(ticket.get("entry"))
        side_word = "BUY" if (ticket.get("direction") or "long") == "long" else "SELL SHORT"
    else:
        occ, strike, limit = resolve_occ(pe, ticket)
        side_word = "BUY"
    if force_sim:
        # Never transmit under force-sim
        append_event(
            events_doc, "TRIGGER_SIM", tid,
            f"[SIM] Trigger hit — would {side_word} {qty}× {occ} @ ${limit:.2f} LMT",
            f"{reason}. OR {or_info.get('or_low')}–{or_info.get('or_high')}. force_sim=1 (no order sent).",
            severity="warn", symbol=occ,
        )
        return {"mode": "force_sim", "submitted": False, "occ": occ, "limit": limit}

    # Use existing stage path
    if _is_stock(ticket):
        result = pe.alpaca_stage_ticket(tid, dry_run=False, submit_paper=True)
    else:
        result = pe.alpaca_stage_ticket(
            tid, dry_run=False, submit_paper=True,
            expiration=ticket.get("session_target"), strike=strike, occ_symbol=occ,
        )
    append_event(
        events_doc, "ORDER_SUBMITTED", tid,
        f"Alpaca Paper Order Submitted: {side_word} {qty}× {occ} @ ${limit:.2f} LMT",
        f"Trigger hit ({reason}). Firewall 6/6 PASS (${ticket.get('actual_dollar_risk', 120)} max risk). OR "
        f"${or_info.get('or_low')}–${or_info.get('or_high')}.",
        severity="pos", symbol=occ,
        extra={"alpaca_order_id": (result.get("response") or {}).get("id") if isinstance(result.get("response"), dict) else ticket.get("alpaca_order_id")},
    )
    return {"mode": "submitted_paper", "submitted": True, "result": result, "occ": occ, "limit": limit}


ACTIVE_STATUSES = {"submitted_paper", "filled_paper"}


def manage_open_positions(pe, events_doc: dict, or_info: dict, bars_5m: List[dict], now: datetime, force_sim: bool) -> List[str]:
    """Manage filled paper positions for blotter tickets. Rules come from each ticket
    (parse_ticket_rules). Returns action notes."""
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
    # Stock tickets share the underlying symbol, so a position is only attributable
    # when exactly one active stock ticket trades that symbol.
    active_stock_by_sym: Dict[str, int] = {}
    for t in rows:
        if t.get("status") in ACTIVE_STATUSES and _is_stock(t):
            s = (t.get("symbol") or "SPY").upper()
            active_stock_by_sym[s] = active_stock_by_sym.get(s, 0) + 1

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
                ticket = {**ticket, "status": "filled_paper", "alpaca_fill_price": entry_order.get("filled_avg_price"),
                          "alpaca_filled_ct": _iso_ct()}
                notes.append(f"filled:{tid}")

        # Only tickets that are live in the pod are managed. Closed or invalidated rows never touch positions.
        if ticket.get("status") not in ACTIVE_STATUSES:
            continue
        stock = _is_stock(ticket)
        rules = parse_ticket_rules(ticket)
        direction = (ticket.get("direction") or "long").lower()
        sgn = 1.0 if direction == "long" else -1.0

        if stock:
            occ = (ticket.get("symbol") or "SPY").upper()
            if active_stock_by_sym.get(occ, 0) != 1:
                notes.append(f"manage_skip:{tid}:ambiguous_stock_position:{occ}")
                continue
        else:
            occ = ((ticket.get("alpaca_paper_payload") or {}).get("order") or {}).get("symbol") or ticket.get("occ_symbol")
        pos = next((p for p in positions if p.get("symbol") == occ), None)
        if not pos:
            continue

        qty = abs(float(pos.get("qty") or 0))
        pos_side = (pos.get("side") or ("short" if float(pos.get("qty") or 0) < 0 else "long")).lower()
        if qty <= 0 or pos_side != direction:
            continue
        mark = float(pos.get("current_price") or pos.get("lastday_price") or 0)
        entry = float(pos.get("avg_entry_price") or ticket.get("alpaca_fill_price") or ticket.get("entry") or 0)
        if stock:
            stop = float(ticket.get("stop"))
            t1 = float(ticket.get("target1"))
            t2 = float(ticket.get("target2") or ticket.get("target1"))
        else:
            stop = float(ticket.get("stop") or (ticket.get("oco_bracket") or {}).get("stop_order") or 1.25)
            t1 = float(ticket.get("target1") or (ticket.get("oco_bracket") or {}).get("take_profit_1") or 3.65)
            t2 = float(ticket.get("target2") or (ticket.get("oco_bracket") or {}).get("take_profit_2") or 4.85)
        risk_unit = abs(entry - stop) if entry and stop else float(ticket.get("risk_per_unit") or 0.6)

        # Underlying hard invalidation from the ticket's own hard_invalidation_underlying
        und_inv, und_why, inv_lvl = in_trade_invalidation(rules, or_info, bars_5m) if rules.get("ok") else (False, "", None)

        def _close(kind: str, headline: str, detail: str):
            if force_sim:
                append_event(events_doc, kind, tid, "[SIM] " + headline, detail + " (force_sim)", "warn", occ)
                notes.append(f"sim:{kind}:{tid}")
                return
            try:
                # cancel open orders for this symbol only (bracket legs / child orders)
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
                exit_side = "sell" if direction == "long" else "buy"
                if stock:
                    lim = round(mark * (0.998 if exit_side == "sell" else 1.002), 2) if mark else None
                else:
                    lim = max(0.01, round(mark * 0.95, 2)) if mark else None
                body = {
                    "symbol": occ,
                    "qty": str(int(qty)),
                    "side": exit_side,
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

        verb = "sold" if direction == "long" else "covered"
        # Hard stop on mark (direction-aware) OR underlying invalidation
        stop_hit = bool(mark) and ((mark <= stop) if direction == "long" else (mark >= stop))
        if stop_hit or und_inv:
            why = f"mark {mark} {'<=' if direction == 'long' else '>='} stop {stop}" if stop_hit else und_why
            _close("STOP_EXIT", f"STOP EXIT: {verb} {int(qty)}× {occ}", why)
            continue

        # Targets. Stock tickets carry a native broker bracket (stop + take-profit at T1),
        # so T1/T2 scaling stays at the broker. Options are scaled here.
        if not stock:
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

        # Time stop: legacy 50m/-0.5R + 60m/<0R; otherwise parsed from ticket.time_stop
        filled_at = ticket.get("alpaca_filled_ct") or (entry_order or {}).get("filled_at")
        ts = (rules or {}).get("time_stop") or {}
        if filled_at:
            try:
                if isinstance(filled_at, str) and "T" in filled_at:
                    ft = parse_bar_ts(filled_at) if filled_at.endswith("Z") or "+" in filled_at[-6:] else datetime.fromisoformat(filled_at)
                    if ft.tzinfo is None:
                        ft = ft.replace(tzinfo=CT)
                    mins = (now - ft.astimezone(CT)).total_seconds() / 60.0
                    pnl_r = (sgn * (mark - entry) / risk_unit) if risk_unit else 0
                    if ts.get("legacy") or not rules.get("ok"):
                        if mins >= 50 and pnl_r < -0.5:
                            _close("TIME_STOP_EXIT", f"TIME STOP: flat {occ}", f"{mins:.0f}m since fill · PnL {pnl_r:.2f}R")
                            continue
                        if mins >= 60 and pnl_r < 0:
                            _close("TIME_STOP_EXIT", f"TIME STOP: flat {occ}", f"{mins:.0f}m no progress · PnL {pnl_r:.2f}R")
                            continue
                    elif ts.get("minutes") is not None:
                        if mins >= ts["minutes"] and pnl_r < float(ts.get("min_progress_r", 0.0)):
                            _close("TIME_STOP_EXIT", f"TIME STOP: flat {occ}",
                                   f"{mins:.0f}m since fill · PnL {pnl_r:.2f}R < {ts.get('min_progress_r', 0.0)}R")
                            continue
            except Exception:
                pass

        # EOD flat (ticket time_stop 'flat ... by HH:MM', default 14:45 CT) on session day
        exp = (ticket.get("session_target") or ticket.get("option_expiration") or "")
        today = now.strftime("%Y-%m-%d")
        eod = (rules or {}).get("eod_flat") or EOD_FLAT
        if exp == today and _hm(now) >= tuple(eod):
            _close("EOD_0DTE_FLAT", f"EOD FLAT: {occ}", f"{eod[0]:02d}:{eod[1]:02d} CT flat rule")
            continue

    return notes


# ---------------------------------------------------------------------------
# Tick vault sync (Nov 10 2026). Order, all under /tmp/horsey_repo_sync.lock:
#   1) put back leftover derived vault/** artifacts (they are rebuilt anyway),
#   2) git pull --rebase --autostash origin main,
#   3) rebuild encrypted vault state (vault_crypto build-state),
#   4) commit + push index.html / vault/** if the change is meaningful, or as a
#      throttled heartbeat (at most 1 per VAULT_HEARTBEAT_SEC); else restore HEAD.
# The working tree is clean between ticks. Never force-push, and never stage anything
# outside index.html / vault/**.
# ---------------------------------------------------------------------------
VAULT_HEARTBEAT_SEC = int(os.environ.get("HORSEY_VAULT_HEARTBEAT_SEC", "300"))
VAULT_ALLOWED = ("index.html", "vault/")
VAULT_COMMIT_MSG = "vault: refresh state after Alpaca RTH tick"
# Keys the builder/tick rewrite on every run; they carry no new information.
VOLATILE_KEYS = {"updated_ct", "built_at", "updated_epoch",
                 # broker mark-to-market fields that move on every quote
                 "current_price", "lastday_price", "change_today", "market_value",
                 "unrealized_pl", "unrealized_plpc", "unrealized_intraday_pl", "unrealized_intraday_plpc"}
VOLATILE_PATH_KEYS = {
    "pod/paper_execution_events.json": {"rth"},
    "pod/paper_account_state.json": {"equity", "buying_power", "account"},
    "pod/risk/portfolio_snapshot.json": {"spy_spot", "vix", "delta_exposure", "net_gex_45d",
                                         "alpaca_paper_equity", "capital_at_risk"},
}
# Backstop: a semantic diff the tick didn't flag itself is committed at most this often.
VAULT_SEMANTIC_MIN_SEC = int(os.environ.get("HORSEY_VAULT_SEMANTIC_MIN_SEC", "60"))


def _git(root, *args, timeout=60):
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, timeout=timeout)


def _allowed_path(p: str) -> bool:
    return p == "index.html" or p.startswith("vault/")


def _porcelain(root, *paths) -> List[Tuple[str, str]]:
    r = _git(root, "status", "--porcelain", "--untracked-files=all", "--", *paths)
    out = []
    for line in (r.stdout or "").splitlines():
        if len(line) > 3:
            out.append((line[:2], line[3:].strip().strip('"')))
    return out


def _restore_derived(root, paths: Optional[List[str]] = None) -> List[str]:
    """Put vault/** (and optionally index.html) back to HEAD: tracked mods are checked out,
    untracked derived files under vault/ are removed. Returns the touched paths."""
    paths = paths or ["vault"]
    touched = []
    for st, p in _porcelain(root, *paths):
        if not _allowed_path(p):
            continue
        touched.append(p)
        if st == "??":
            try:
                (Path(root) / p).unlink()
            except Exception:
                pass
        else:
            _git(root, "reset", "-q", "--", p)
            if _git(root, "cat-file", "-e", f"HEAD:{p}").returncode == 0:
                _git(root, "checkout", "-q", "HEAD", "--", p)
            else:
                try:
                    (Path(root) / p).unlink()
                except Exception:
                    pass
    return touched


def _normalize(obj, path_keys=frozenset()):
    if isinstance(obj, dict):
        return {k: _normalize(v) for k, v in obj.items() if k not in VOLATILE_KEYS and k not in path_keys}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    return obj


def vault_semantic_fingerprint(root, rev: Optional[str] = None) -> Optional[str]:
    """sha256 of the decrypted vault state with timestamp-only keys removed.
    rev=None reads the working tree; rev='HEAD' reads the committed blob. None on failure."""
    import gzip, hashlib, importlib.util as _ilu
    try:
        if rev:
            r = _git(root, "show", f"{rev}:vault/state.enc.json")
            if r.returncode != 0:
                return None
            env = json.loads(r.stdout)
        else:
            env = json.loads((Path(root) / "vault" / "state.enc.json").read_text())
        sp = _ilu.spec_from_file_location("_vault_crypto_fp", str(Path(root) / "scripts" / "vault_crypto.py"))
        vc = _ilu.module_from_spec(sp); sp.loader.exec_module(vc)
        pw = vc.load_password()
        if not pw:
            return None
        raw = vc.decrypt_blob(pw, env)
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
        doc = json.loads(raw.decode("utf-8"))
        files = doc.get("files") or {}
        norm = {}
        for k, v in files.items():
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except Exception:
                    pass
            norm[k] = _normalize(v, frozenset(VOLATILE_PATH_KEYS.get(k, ())))
        blob = json.dumps({"files": norm, "aliases": doc.get("aliases")}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
    except Exception:
        return None


def _last_vault_commit_epoch(root) -> Optional[int]:
    r = _git(root, "log", "-1", "--format=%ct", "HEAD", "--", "vault/state.enc.json")
    try:
        return int((r.stdout or "").strip())
    except Exception:
        return None


def vault_sync_after_tick(pe, changed: bool, push: bool, root=None, fingerprint=None,
                          now_epoch: Optional[float] = None, heartbeat_sec: Optional[int] = None,
                          remote: str = "origin", branch: str = "main") -> Dict[str, Any]:
    """Pull, rebuild, then commit/push the encrypted vault, all under the repo lock. Never raises."""
    import time as _t
    root = Path(root or ROOT)
    fingerprint = fingerprint or vault_semantic_fingerprint
    heartbeat_sec = VAULT_HEARTBEAT_SEC if heartbeat_sec is None else heartbeat_sec
    out: Dict[str, Any] = {"vault_built": False, "pushed": False, "committed": False, "pulled": None, "log": []}
    log = out["log"]

    if not push:
        # Manual/no-push runs keep the old behaviour: rebuild only, no git.
        try:
            with pe.repo_sync_lock(label="alpaca_rth_tick build-state", timeout_sec=90):
                pe.maybe_rebuild_vault_state()
            out["vault_built"] = True
        except Exception as e:
            out["vault_error"] = str(e)[:200]
        return out

    def _pull(tag):
        r = _git(root, "pull", "--rebase", "--autostash", remote, branch, timeout=90)
        ok = r.returncode == 0
        if not ok:
            # never leave a half-done rebase behind
            if (root / ".git" / "rebase-merge").exists() or (root / ".git" / "rebase-apply").exists():
                _git(root, "rebase", "--abort")
            log.append(f"{tag}_fail:{(r.stderr or r.stdout or '').strip()[:160]}")
        else:
            log.append(f"{tag}_ok")
        return ok

    def _rebuild():
        pe.maybe_rebuild_vault_state()
        out["vault_built"] = True

    def _stage_and_commit(kind: str) -> Optional[str]:
        idx_clean_before = out.get("_index_clean_before", True)
        paths = ["vault"] + (["index.html"] if idx_clean_before else [])
        _git(root, "add", "-A", "--", *paths)
        staged = [p for p in (_git(root, "diff", "--cached", "--name-only").stdout or "").splitlines() if p.strip()]
        bad = [p for p in staged if not _allowed_path(p)]
        if bad:
            # Something else was staged (not by us). Unstage only our paths and refuse.
            _git(root, "reset", "-q", "--", *paths)
            log.append(f"refuse_commit:foreign_staged:{bad[:5]}")
            return None
        if not staged:
            return None
        msg = VAULT_COMMIT_MSG + (" (heartbeat)" if kind == "heartbeat" else "")
        r = _git(root, "commit", "-q", "-m", msg, "--", *staged)
        if r.returncode != 0:
            _git(root, "reset", "-q", "--", *staged)
            log.append(f"commit_fail:{(r.stderr or r.stdout or '').strip()[:160]}")
            return None
        sha = (_git(root, "rev-parse", "HEAD").stdout or "").strip()
        out["committed"] = True
        out["commit"] = sha[:12]
        out["commit_kind"] = kind
        return sha

    def _undo_own_commit(sha: str):
        head = (_git(root, "rev-parse", "HEAD").stdout or "").strip()
        if head == sha:
            _git(root, "reset", "-q", "--soft", "HEAD~1")
            _restore_derived(root, ["vault", "index.html"] if out.get("_index_clean_before", True) else ["vault"])
            log.append("undo_own_commit")

    try:
        with pe.repo_sync_lock(label="alpaca_rth_tick vault-sync", timeout_sec=90):
            # 1) leftover derived artifacts from an older flow block the pull; they are rebuilt below
            leftovers = _restore_derived(root, ["vault"])
            if leftovers:
                log.append(f"restored_leftover:{len(leftovers)}")
            out["_index_clean_before"] = not _porcelain(root, "index.html")
            # 2) pull first
            out["pulled"] = _pull("pull")
            # 3) rebuild on top of whatever we have now
            _rebuild()
            dirty = [p for _, p in _porcelain(root, "vault", "index.html") if _allowed_path(p)]
            if not out["_index_clean_before"]:
                dirty = [p for p in dirty if p != "index.html"]
            if not dirty:
                log.append("no_change")
            else:
                # 4) meaningful vs timestamp-only
                only_state = set(dirty) <= {"vault/state.enc.json", "vault/manifest.json"}
                meaningful = bool(changed) or not only_state
                if not meaningful:
                    fp_new, fp_head = fingerprint(root, None), fingerprint(root, "HEAD")
                    meaningful = (fp_new is None or fp_head is None or fp_new != fp_head)
                    out["fp_equal"] = (fp_new is not None and fp_new == fp_head)
                last = _last_vault_commit_epoch(root)
                now_e = now_epoch if now_epoch is not None else _t.time()
                since = (now_e - last) if last is not None else None
                if changed or not only_state:
                    kind = "change"            # tick acted, or new snapshots: commit now
                elif meaningful:
                    kind = "change" if (since is None or since >= VAULT_SEMANTIC_MIN_SEC) else None
                    if kind is None:
                        log.append("semantic_change_deferred")
                else:
                    kind = "heartbeat" if (since is None or since >= heartbeat_sec) else None
                if kind is None:
                    _restore_derived(root, ["vault"])
                    log.append("timestamp_only:restored_head")
                else:
                    sha = _stage_and_commit(kind)
                    if sha:
                        r = _git(root, "push", remote, f"HEAD:{branch}", timeout=90)
                        if r.returncode == 0:
                            out["pushed"] = True
                            log.append(f"push_ok:{kind}")
                        else:
                            log.append(f"push_fail_1:{(r.stderr or '').strip()[:160]}")
                            # retry once: pull --rebase; on conflict, rebuild on top of the remote
                            if _pull("retry_pull"):
                                head_subj = (_git(root, "log", "-1", "--format=%s").stdout or "").strip()
                                sha = (_git(root, "rev-parse", "HEAD").stdout or "").strip() if head_subj.startswith(VAULT_COMMIT_MSG) else None
                                if not sha:
                                    log.append("retry_pull:own_commit_missing")
                            else:
                                _undo_own_commit(sha)
                                sha = None
                                if _pull("retry_pull_clean"):
                                    _rebuild()
                                    sha = _stage_and_commit(kind)
                                    if not sha:
                                        _restore_derived(root, ["vault"])
                            if sha:
                                r2 = _git(root, "push", remote, f"HEAD:{branch}", timeout=90)
                                if r2.returncode == 0:
                                    out["pushed"] = True
                                    log.append(f"push_ok_retry:{kind}")
                                else:
                                    log.append(f"push_gave_up:{(r2.stderr or '').strip()[:160]}")
                                    _undo_own_commit(sha)
                    else:
                        _restore_derived(root, ["vault"])
            # also publish an earlier local commit that never got pushed (ahead of remote)
            ahead = (_git(root, "rev-list", "--count", f"{remote}/{branch}..HEAD").stdout or "0").strip()
            if ahead.isdigit() and int(ahead) > 0 and not out["pushed"]:
                # only if every ahead commit touches allowed paths
                files = (_git(root, "diff", "--name-only", f"{remote}/{branch}..HEAD").stdout or "").split()
                if files and all(_allowed_path(f) for f in files):
                    r = _git(root, "push", remote, f"HEAD:{branch}", timeout=90)
                    out["pushed"] = r.returncode == 0
                    log.append("push_ahead_ok" if r.returncode == 0 else "push_ahead_fail")
                else:
                    log.append(f"ahead_{ahead}_not_vault_only:left_alone")
    except Exception as e:
        out["vault_error"] = str(e)[:200]
    out.pop("_index_clean_before", None)
    out["tree_clean"] = not [p for _, p in _porcelain(root, "vault", "index.html")]
    out["log"] = [" ".join(str(x).split()) for x in out["log"]]
    if out["log"]:
        print("[alpaca_rth vault-sync] " + " | ".join(out["log"]), file=sys.stderr)
    return out


def push_vault_if_changed(pe, changed: bool, push: bool) -> Dict[str, Any]:
    """Back-compat entry point used by alpaca_rth_tick."""
    return vault_sync_after_tick(pe, changed, push)


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

        rules = parse_ticket_rules(ticket)
        if not rules.get("ok"):
            # Unknown/unparseable trigger: keep pending, never submit or invalidate.
            warn = "; ".join(rules.get("warnings") or ["unparsed"])
            result.setdefault("warnings", []).append(warn)
            result["actions"].append(f"pending_unparsed:{tid}")
            print(f"[alpaca_rth] WARNING ticket {tid} left pending: {warn}", file=sys.stderr)
            continue
        if rules.get("warnings"):
            result.setdefault("warnings", []).extend(rules["warnings"])

        inv, inv_why = pre_trigger_invalidated(or_info, bars, now, rules)
        if inv:
            rewrite_blotter_ticket(tid, lambda t: {**t, "status": "invalidated_paper", "invalidation_reason": inv_why, "invalidated_ct": _iso_ct()})
            append_event(events_doc, "INVALIDATED", tid, f"Setup invalidated: {tid}", inv_why, "neg",
                         ((ticket.get("alpaca_paper_payload") or {}).get("order") or {}).get("symbol") or "")
            changed = True
            result["actions"].append(f"invalidated:{tid}")
            continue

        if force_sim and not market_openish:
            # Evaluate with whatever bars we have but never submit
            fired, reason, meta = evaluate_entry(ticket, or_info, bars, now, rules) if or_info.get("ready") else (False, "OR not ready (sim)", {})
            result["actions"].append(f"sim_eval:{tid}:{fired}:{reason}")
            continue

        fired, reason, meta = evaluate_entry(ticket, or_info, bars, now, rules)
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

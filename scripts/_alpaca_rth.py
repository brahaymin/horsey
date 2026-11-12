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
    "cancelled_paper", "voided_paper", "skipped_paper",
}
ENTRY_WINDOW_START = (9, 0)   # CT after OR built (legacy tickets; OR30 completes at 09:00)
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


# ---------------------------------------------------------------------------
# Nov 12 2026: joint PTJ/Tactician framework v1.2 fixes (CIO tasking C2/C3).
# Engine settings live in pod/risk/limits.json -> "paper_engine" (local, gitignored).
# Every default below is the conservative one, so a missing/broken file never loosens a gate.
# ---------------------------------------------------------------------------
LIMITS_PATH = ROOT / "pod" / "risk" / "limits.json"
FED_SPEAKERS_DIR = ROOT / "pod" / "risk" / "fed_speakers"
OR15_END = (8, 45)
OR30_END = (9, 0)
EARLIEST_ENTRY_FLOOR = (8, 45)       # CEO-approved floor (Nov 12 11:21 CT); nothing earlier, whatever the config says
DEFAULT_EARLIEST_ENTRY = (9, 0)      # used when the config is missing or unreadable
BAR_MINUTES = 5
RVOL_FULL = 1.2                      # R15: >= 1.2x full size (the old flat gate)
RVOL_HALF_FLOOR = 0.8                # R15: 0.8-1.2x half size + 2-bar hold (behind a flag, default OFF)
BAR_RVOL_SUBSTITUTE = 1.5            # R15: a bar >= 1.5x its slot substitutes for the second hold bar
SPIKE_CAP_MULT = 8.0
MIN_RVOL_SESSIONS = 10
ENGINE_DEFAULTS: Dict[str, Any] = {
    "earliest_entry_ct": "09:00",
    "rvol_half_size_tier_enabled": False,
    "live_walls_max_age_min": 90,
    "option_quote_max_age_sec": 180,
    "option_max_spread_pct": 5.0,
    "reclaim_margin": 0.50,
}


def engine_config() -> Dict[str, Any]:
    cfg = dict(ENGINE_DEFAULTS)
    try:
        doc = json.loads(LIMITS_PATH.read_text(encoding="utf-8"))
        sec = doc.get("paper_engine") if isinstance(doc, dict) else None
        if isinstance(sec, dict):
            for k in ENGINE_DEFAULTS:
                if k in sec:
                    cfg[k] = sec[k]
    except Exception:
        pass
    # the half tier loosens the old gate, so only a literal JSON true turns it on
    cfg["rvol_half_size_tier_enabled"] = cfg.get("rvol_half_size_tier_enabled") is True
    return cfg


def earliest_entry(cfg: Optional[dict] = None) -> Tuple[int, int]:
    """Configured earliest arm/entry time (CT), never earlier than 08:45; 09:00 if unparsable."""
    cfg = cfg or engine_config()
    hm = _hhmm(cfg.get("earliest_entry_ct"))
    if not hm or not (0 <= hm[0] <= 23 and 0 <= hm[1] <= 59):
        return DEFAULT_EARLIEST_ENTRY
    return max(tuple(hm), EARLIEST_ENTRY_FLOOR)


def _bar_dt(b: dict) -> datetime:
    return b.get("_dt") or parse_bar_ts(b["t"])


def completed_bars(bars: List[dict], now: datetime) -> List[dict]:
    """Only bars whose 5-minute interval has ended (start + 5m <= now). A still-forming bar's
    last tick is never treated as a close (framework C2 / R8 / R13)."""
    out = []
    for b in bars or []:
        dt = _bar_dt(b)
        if dt + timedelta(minutes=BAR_MINUTES) <= now:
            if "_dt" not in b:
                b = dict(b); b["_dt"] = dt
            out.append(b)
    return out


def load_latest() -> Dict[str, Any]:
    try:
        return json.loads(LATEST.read_text(encoding="utf-8"))
    except Exception:
        return {}


def tod_curve_from_latest(j: dict, now: datetime) -> Tuple[Optional[dict], str]:
    """Per-slot 20-session curves published by the SPY Gamma Monitor in
    latest.json -> intraday_feed.spy_5m.slot_curves (slot_ct, avg_volume, avg_cum_volume)."""
    f5 = ((j or {}).get("intraday_feed") or {}).get("spy_5m") or {}
    curves = f5.get("slot_curves") or []
    sessions = [str(x) for x in (f5.get("sessions_used") or [])]
    if not curves:
        return None, "time-of-day volume curve missing from latest.json"
    if len(sessions) < MIN_RVOL_SESSIONS:
        return None, f"time-of-day volume curve has {len(sessions)} sessions (< {MIN_RVOL_SESSIONS})"
    today = now.astimezone(CT).date().isoformat()
    if max(sessions) >= today:
        return None, "time-of-day volume curve includes today's session"
    slots = {}
    for c in curves:
        try:
            k = str(c["slot_ct"])[:5]
            avg, cum = float(c["avg_volume"]), float(c["avg_cum_volume"])
        except Exception:
            continue
        if avg > 0 and cum > 0:
            slots[k] = (avg, cum)
    if not slots:
        return None, "time-of-day volume curve unusable"
    return {"slots": slots, "sessions": len(sessions), "monitor": f5}, "ok"


def compute_tod_rvol(vol_bars: List[dict], curve: dict, now: datetime) -> Dict[str, Any]:
    """R14: cumulative RTH volume through the last COMPLETED bar / 20-session average cumulative
    volume through the same slot; each bar capped at 8x its slot average (spike filter).
    vol_bars must be consolidated-volume bars (Yahoo), the same source as the curve. Fails
    closed (None) when today's bars have gaps or a slot is missing from the curve."""
    done = completed_bars(filter_rth_bars(vol_bars, now), now)
    if not done:
        return {"tod_rvol": None, "bar_rvol": None, "why": "no completed consolidated bars yet"}
    slots = curve["slots"]
    cum, last_cap, prev = 0.0, None, None
    for b in done:
        dt = b["_dt"]
        k = dt.strftime("%H:%M")
        if k not in slots:
            return {"tod_rvol": None, "bar_rvol": None, "why": f"slot {k} missing from curve"}
        if prev is not None and (dt - prev) != timedelta(minutes=BAR_MINUTES):
            return {"tod_rvol": None, "bar_rvol": None, "why": f"gap in consolidated bars before {k}"}
        prev = dt
        last_cap = min(float(b.get("v") or 0), SPIKE_CAP_MULT * slots[k][0])
        cum += last_cap
    if (done[0]["_dt"].hour, done[0]["_dt"].minute) != (8, 30):
        return {"tod_rvol": None, "bar_rvol": None, "why": "consolidated bars do not start at 08:30"}
    k = done[-1]["_dt"].strftime("%H:%M")
    avg, avgcum = slots[k]
    return {"tod_rvol": round(cum / avgcum, 4), "bar_rvol": round(last_cap / avg, 4), "slot_ct": k,
            "source": "yahoo_5m_consolidated_vs_monitor_curve", "sessions": curve["sessions"], "why": "ok"}


def live_rvol(now: datetime, entry_bars: List[dict], latest_doc: Optional[dict] = None,
              fetch_vol_bars=None) -> Dict[str, Any]:
    """Time-of-day RVOL for the tick. Curve from latest.json (monitor), numerator from Yahoo 5m
    consolidated bars. If Yahoo fails, the monitor's own tod_rvol is used only when it is for
    the same last completed slot as the watcher's bars. Otherwise None (gates fail closed)."""
    j = latest_doc if latest_doc is not None else load_latest()
    curve, why = tod_curve_from_latest(j, now)
    if curve is None:
        return {"tod_rvol": None, "bar_rvol": None, "why": why}
    fetch = fetch_vol_bars or fetch_yahoo_bars_5m
    try:
        vb = fetch()
    except Exception:
        vb = []
    out = compute_tod_rvol(vb, curve, now) if vb else {"tod_rvol": None, "bar_rvol": None, "why": "consolidated 5m bars unavailable"}
    if out.get("tod_rvol") is not None:
        eb = completed_bars(entry_bars, now)
        if eb and eb[-1]["_dt"].strftime("%H:%M") != out.get("slot_ct"):
            out["why"] = f"note: RVOL slot {out.get('slot_ct')} vs trigger bar {eb[-1]['_dt'].strftime('%H:%M')}"
        return out
    f5 = curve["monitor"]
    mt, last = f5.get("tod_rvol"), f5.get("last_completed_bar_ct")
    eb = completed_bars(entry_bars, now)
    try:
        if mt is not None and last and eb:
            ldt = datetime.fromisoformat(str(last)).astimezone(CT)
            if ldt.date() == now.astimezone(CT).date() and ldt.strftime("%H:%M") == eb[-1]["_dt"].strftime("%H:%M"):
                return {"tod_rvol": float(mt), "bar_rvol": f5.get("bar_rvol"), "slot_ct": ldt.strftime("%H:%M"),
                        "source": "monitor_snapshot_same_slot", "why": "ok"}
    except Exception:
        pass
    return {"tod_rvol": None, "bar_rvol": None, "why": out.get("why") or "RVOL unavailable"}


def rvol_tier(tod: Optional[float], full_min: float, half_enabled: bool) -> Dict[str, Any]:
    """R15 for breakout / continuation / reclaim. full_min = max(ticket RVOL, 1.2).
    Flag OFF (default): below full_min blocks, exactly like the old flat gate.
    Flag ON: 0.8x..full_min allows half size with a 2-bar hold; below 0.8x blocks."""
    if tod is None:
        return {"allow": False, "factor": 0.0, "hold2": False, "tier": "unavailable",
                "why": "RVOL unavailable (fail closed)"}
    if tod >= full_min:
        return {"allow": True, "factor": 1.0, "hold2": False, "tier": "full", "why": f"RVOL {tod:.2f} ≥ {full_min:g}"}
    if half_enabled and tod >= RVOL_HALF_FLOOR:
        return {"allow": True, "factor": 0.5, "hold2": True, "tier": "half",
                "why": f"RVOL {tod:.2f} in {RVOL_HALF_FLOOR:g}–{full_min:g} (half size, 2-bar hold)"}
    return {"allow": False, "factor": 0.0, "hold2": False, "tier": "blocked",
            "why": f"RVOL {tod:.2f} < {full_min:g}" + ("" if not half_enabled else f" and < {RVOL_HALF_FLOOR:g}")}


def combine_size_factors(factors: List[float]) -> float:
    """R21: the smallest factor applies (not multiplied); three or more separate 1/2 factors -> 1/4."""
    if not factors:
        return 1.0
    f = min(factors)
    if sum(1 for x in factors if abs(x - 0.5) < 1e-9) >= 3:
        f = min(f, 0.25)
    return f


def fed_speaker_blackout(now: datetime) -> Tuple[bool, str, Optional[str]]:
    """Framework §5 Fed-speaker pause: no new arming or entries from 5 minutes before to 10 minutes
    after each scheduled speaker start, read from pod/risk/fed_speakers/YYYY-MM-DD.json
    ({"date": ..., "speakers": [{"name": ..., "start_ct": "HH:MM"}]}).
    Returns (blocked, reason, warning). A missing file means no blackouts (warning only);
    an unreadable or malformed file blocks new entries (fail closed)."""
    day = now.astimezone(CT).strftime("%Y-%m-%d")
    p = FED_SPEAKERS_DIR / f"{day}.json"
    if not p.exists():
        return False, "", f"no Fed-speaker schedule for {day}; no blackout windows applied"
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        speakers = doc.get("speakers") if isinstance(doc, dict) else doc
        if isinstance(doc, dict) and doc.get("date") not in (None, day):
            raise ValueError("date mismatch")
        if not isinstance(speakers, list):
            raise ValueError("speakers not a list")
        wins = []
        for sp in speakers:
            hm = _hhmm((sp or {}).get("start_ct"))
            if not hm:
                raise ValueError("speaker without start_ct")
            st = now.astimezone(CT).replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            wins.append((str(sp.get("name") or "Fed speaker"), st))
    except Exception:
        return True, "Fed-speaker schedule unreadable; new entries held (fail closed)", None
    for name, st in wins:
        a, b = st - timedelta(minutes=5), st + timedelta(minutes=10)
        if a <= now <= b:
            return True, (f"Fed-speaker pause: {name} {st:%H:%M} CT; no new arming or entries "
                          f"{a:%H:%M}–{b:%H:%M} CT"), None
    return False, "", None


def live_wall_check(spot: Optional[float], now: datetime, latest_doc: Optional[dict] = None,
                    cfg: Optional[dict] = None) -> Tuple[bool, str]:
    r = wall_check(spot, now, latest_doc, cfg)
    return r["ok"], r["why"]


def wall_check(spot: Optional[float], now: datetime, latest_doc: Optional[dict] = None,
               cfg: Optional[dict] = None) -> Dict[str, Any]:
    """Same as live_wall_check, returning {"ok", "why", "code", "age_min"}; code is one of
    ok | missing | no_timestamp | stale | no_spot | equal | mid_range."""
    def _r(ok, why, code, age=None):
        return {"ok": ok, "why": why, "code": code, "age_min": None if age is None else round(age, 1)}
    try:
        ok, why, code, age = _wall_check_core(spot, now, latest_doc, cfg)
    except Exception as e:  # pragma: no cover
        return _r(False, f"Wall check error ({str(e)[:80]}); entry held (fail closed)", "error")
    return _r(ok, why, code, age)


def _wall_check_core(spot, now, latest_doc, cfg):
    """Firewall check 5 at arm time with LIVE 0DTE walls from the monitor snapshot
    (latest.json summary call_wall_0dte / put_wall_0dte). Blocks mid-range no-man's-land
    (the middle 40% between the walls). Fails closed when the walls are missing, equal,
    stale (older than live_walls_max_age_min) or from another session."""
    cfg = cfg or engine_config()
    j = latest_doc if latest_doc is not None else load_latest()
    s = (j or {}).get("summary") or {}
    try:
        put, call = float(s.get("put_wall_0dte")), float(s.get("call_wall_0dte"))
    except Exception:
        return False, "Live 0DTE walls unavailable; entry held (fail closed)", "missing", None
    ts = s.get("timestamp_ct") or (j or {}).get("timestamp_ct")
    try:
        tsd = datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%S%z").astimezone(CT)
    except Exception:
        try:
            tsd = datetime.fromisoformat(str(ts)).astimezone(CT)
        except Exception:
            return False, "Live 0DTE walls have no timestamp; entry held (fail closed)", "no_timestamp", None
    age_min = (now - tsd).total_seconds() / 60.0
    max_age = float(cfg.get("live_walls_max_age_min") or 90)
    if tsd.date() != now.astimezone(CT).date() or age_min > max_age or age_min < -5:
        return (False, f"0DTE walls are stale: snapshot {tsd:%m-%d %H:%M} CT is {age_min:.0f} min old "
                       f"(limit {max_age:g} min, same session); entry held (fail closed)", "stale", age_min)
    if spot is None:
        return False, "No completed-bar price for the wall check; entry held", "no_spot", age_min
    lo, hi = min(put, call), max(put, call)
    if hi - lo < 1e-9:
        return False, f"0DTE walls coincide (put {put:g} / call {call:g}); wall check cannot run; entry held (fail closed)", "equal", age_min
    w = hi - lo
    if (lo + 0.3 * w) < spot < (hi - 0.3 * w):
        return False, (f"SPY {spot:.2f} is mid-range between the 0DTE walls {lo:g}–{hi:g} "
                       f"(no-man's-land {lo + 0.3 * w:.2f}–{hi - 0.3 * w:.2f}); entry held"), "mid_range", age_min
    return True, f"0DTE walls {lo:g}/{hi:g} at {tsd:%H:%M} CT; SPY {spot:.2f} not mid-range", "ok", age_min


def option_live_quote(pe, occ: str) -> Tuple[Optional[dict], str]:
    """Latest indicative quote + greeks for one contract from the Alpaca data host (read only)."""
    try:
        d = pe.alpaca_request("GET", pe.ALPACA_DATA_HOST, "/v1beta1/options/snapshots",
                              params={"symbols": occ, "feed": "indicative"}, timeout=15) or {}
    except Exception as e:
        return None, f"quote request failed: {str(e)[:120]}"
    snap = (d.get("snapshots") or {}).get(occ) if isinstance(d, dict) else None
    if not isinstance(snap, dict):
        return None, "no snapshot for contract"
    q = snap.get("latestQuote") or {}
    g = snap.get("greeks") or {}
    try:
        bid, ask = float(q.get("bp")), float(q.get("ap"))
    except Exception:
        return None, "quote has no bid/ask"
    return {"bid": bid, "ask": ask, "t": q.get("t"), "delta": g.get("delta"), "gamma": g.get("gamma")}, "ok"


def reprice_option_ticket(ticket: dict, rules: dict, quote: Optional[dict], spot: Optional[float],
                          now: datetime, cfg: Optional[dict] = None) -> Tuple[Optional[dict], str]:
    """R24 at arm time. Returns (repriced_fields, why) or (None, void_reason).
    entry = live mid + $0.02. Premium stop = mid - (|delta|*d + 0.5*gamma*d^2) - $0.05, d = distance
    from spot to the ticket's underlying invalidation level; without greeks or that level, the
    staged stop/entry ratio is kept. Targets keep their staged R multiples. Quantity is capped so
    the dollar risk never exceeds the staged dollar risk. Void when the quote is missing, stale,
    crossed or wider than option_max_spread_pct of mid, or the repriced structure is invalid."""
    cfg = cfg or engine_config()
    if not quote:
        return None, "no live option quote at arm time"
    bid, ask = quote.get("bid"), quote.get("ask")
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None, f"unusable live quote (bid {bid}, ask {ask})"
    try:
        qt = parse_bar_ts(str(quote.get("t")))
        age = (now - qt).total_seconds()
    except Exception:
        return None, "live quote has no timestamp"
    if age > float(cfg.get("option_quote_max_age_sec") or 180) or age < -60:
        return None, f"live quote is stale ({age:.0f}s old)"
    mid = (bid + ask) / 2.0
    spr = 100.0 * (ask - bid) / mid
    if spr > float(cfg.get("option_max_spread_pct") or 5.0) + 1e-9:
        return None, f"spread {spr:.1f}% of mid is above {cfg.get('option_max_spread_pct')}%"
    s_entry = float(ticket.get("staged_entry", ticket.get("entry")))
    s_stop = float(ticket.get("staged_stop", ticket.get("stop")))
    s_t1 = ticket.get("staged_target1", ticket.get("target1"))
    s_t2 = ticket.get("staged_target2", ticket.get("target2"))
    s_qty = int(ticket.get("staged_quantity", ticket.get("quantity")))
    s_risk = ticket.get("staged_actual_dollar_risk", ticket.get("actual_dollar_risk"))
    s_risk = float(s_risk) if s_risk is not None else abs(s_entry - s_stop) * s_qty * 100
    if not (s_entry > s_stop > 0):
        return None, "staged premiums invalid"
    entry = round(mid + 0.02, 2)
    if rules and rules.get("is_option") and is_zero_dte(ticket, now):
        # R24: 0.35-0.50 delta at the trigger (fail closed when the delta is missing)
        dlt = quote.get("delta")
        if dlt is None or not (R24_DELTA_MIN - 1e-9 <= abs(float(dlt)) <= R24_DELTA_MAX + 1e-9):
            return None, f"contract delta {dlt} is outside the 0.35–0.50 band at the trigger"
    inv = (rules or {}).get("inv") or {}
    und_stop = underlying_working_stop(ticket, rules, spot)
    delta, gamma = quote.get("delta"), quote.get("gamma")
    method = "staged_ratio"
    stop = None
    if und_stop is not None and spot is not None and delta is not None and gamma is not None:
        stop = r24_premium_stop(mid, delta, gamma, float(spot) - float(und_stop))
        method = "greeks_R24_v1.3"
    if stop is None:
        stop = entry * (s_stop / s_entry)
    stop = round(stop, 2)
    if stop < 0.01 or stop >= entry:
        return None, f"repriced premium stop {stop:.2f} invalid vs entry {entry:.2f}"
    s_r = s_entry - s_stop
    r_new = entry - stop
    t1 = round(entry + (float(s_t1) - s_entry) / s_r * r_new, 2) if s_t1 is not None else None
    t2 = round(entry + (float(s_t2) - s_entry) / s_r * r_new, 2) if s_t2 is not None else None
    qty = min(s_qty, int(s_risk // (r_new * 100))) if r_new > 0 else 0
    if qty < 1:
        return None, f"repriced risk ${r_new * 100:,.0f}/contract exceeds the staged ${s_risk:,.0f} budget"
    return {"entry": entry, "stop": stop, "target1": t1, "target2": t2, "quantity": qty,
            "reprice": {"ct": _iso_ct(now), "bid": bid, "ask": ask, "mid": round(mid, 4), "spread_pct": round(spr, 2),
                        "quote_age_sec": round(age, 1), "delta": delta, "gamma": gamma, "stop_method": method,
                        "underlying_spot": spot, "underlying_inv": inv.get("level"), "underlying_stop_used": und_stop}}, "ok"


GEX45_CAP_DOLLARS = -5e9  # R22: 45D net GEX below -$5B caps size at 1/2
R24_NO_NEW_0DTE_AFTER = (13, 30)
R24_DELTA_MIN, R24_DELTA_MAX = 0.35, 0.50
R24_TIME_STOP_MIN, R24_TIME_STOP_R = 30, 0.5
R16_FADE_DISARM_RVOL = 1.2
R16_REJECTION_BAR_MAX = 1.0
_OCC_EXP = __import__("re").compile(r"^[A-Z]{1,6}(\d{2})(\d{2})(\d{2})[CP]\d{8}$")


def option_expiry(ticket: dict) -> Optional[str]:
    """YYYY-MM-DD from the ticket's explicit expiry or OCC symbol (None if neither)."""
    exp = ticket.get("option_expiration") or ticket.get("expiry")
    if exp:
        return str(exp)[:10]
    m = _OCC_EXP.match(str(ticket.get("occ_symbol") or "").upper())
    return f"20{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def is_zero_dte(ticket: dict, now: datetime) -> bool:
    if _is_stock(ticket):
        return False
    return option_expiry(ticket) == now.astimezone(CT).strftime("%Y-%m-%d")


def _num(v) -> Optional[float]:
    try:
        v = float(v)
        return v if v == v and abs(v) != float("inf") else None
    except Exception:
        return None


def r24_premium_stop_old(mid: float, delta: float, gamma: float, d: float) -> float:
    """Pre-v1.3 (wider) form, kept only for comparison: mid - (|Δ|·d + ½|Γ|·d²) - 0.05."""
    d = abs(float(d))
    return mid - (abs(float(delta)) * d + 0.5 * abs(float(gamma)) * d * d) - 0.05


def r24_premium_stop(mid: float, delta: float, gamma: float, d: float) -> float:
    """R24 premium stop, v1.3 sign correction (11:28 CT, PTJ-agreed; framework R24 and the
    Nov 13 card): mid - |Δ|·d + ½Γ·d² - $0.05, d = distance from the trigger price to the
    underlying working stop (same form for calls and puts, |Δ|; d taken as a distance, so its
    sign never matters). Per the CIO, the TIGHTER (higher) of the corrected and the old form is
    used in every case, so the stop can never be wider than before."""
    d = abs(float(d))
    new = mid - abs(float(delta)) * d + 0.5 * abs(float(gamma)) * d * d - 0.05
    return max(new, r24_premium_stop_old(mid, delta, gamma, d))


def underlying_working_stop(ticket: dict, rules: Optional[dict], spot: Optional[float]) -> Optional[float]:
    """Nearer (tighter) of the ticket's underlying working stop (underlying_stop) and its parsed
    hard invalidation level. None if neither exists."""
    cands = [x for x in (_num(ticket.get("underlying_stop")),
                         _num(((rules or {}).get("inv") or {}).get("level"))) if x is not None]
    if not cands:
        return None
    if spot is None:
        return cands[0]
    return min(cands, key=lambda x: abs(float(spot) - x))


def underlying_target(ticket: dict) -> Optional[float]:
    ut = ticket.get("underlying_trigger") if isinstance(ticket.get("underlying_trigger"), dict) else {}
    for v in (ticket.get("underlying_target"), ut.get("underlying_target"), ut.get("t1_wall")):
        if _num(v) is not None:
            return _num(v)
    return None


def c4_check(ticket: dict, rules: dict, spot: Optional[float], now: datetime) -> Tuple[bool, str]:
    """R19 + C4 at arm time for option tickets (premium R:R is what the sizer measured).
    Passes if the underlying R (first real target vs working stop, from the trigger price) meets
    the minimum (3:1 tactical, 5:1 PTJ macro), or, for 0DTE tactical tickets only, if premium R
    meets 3:1 AND the underlying target is at least 1.0x the stop distance away. Also R24:
    never a strike beyond the target. Missing target or stop => fail closed."""
    if _is_stock(ticket):
        return True, "stock ticket (underlying R measured by the sizer)"
    tgt = underlying_target(ticket)
    stp = underlying_working_stop(ticket, rules, spot)
    if tgt is None or stp is None or spot is None:
        return False, "C4/R19: underlying target or stop missing on the ticket; entry held (fail closed)"
    sgn = 1.0 if rules.get("direction") == "long" else -1.0
    reward, risk = sgn * (tgt - spot), sgn * (spot - stp)
    if risk <= 0 or reward <= 0:
        return False, f"C4/R19: target {tgt:g} / stop {stp:g} not on the right sides of {spot:.2f}; entry held"
    k = _num(ticket.get("strike"))
    if k is None:
        po = _OCC_EXP.match(str(ticket.get("occ_symbol") or "").upper())
        k = int(str(ticket.get("occ_symbol"))[-8:]) / 1000.0 if po else None
    if k is not None and sgn * (k - tgt) > 1e-9:
        return False, f"R24: strike {k:g} is beyond the target {tgt:g}; entry held"
    macro = str(ticket.get("strategy_agent") or "").lower() in ("ptj", "ptj macro & regime strategist") or \
        "ptj" in str(ticket.get("strategy_agent") or "").lower()
    min_rr = 5.0 if macro else max(3.0, _num(ticket.get("min_rr")) or 3.0)
    und_r = reward / risk
    if und_r >= min_rr - 1e-9:
        return True, f"underlying R {und_r:.2f} ≥ {min_rr:g}"
    if macro or not is_zero_dte(ticket, now):
        return False, f"R19: underlying R {und_r:.2f} < {min_rr:g} (premium R does not count here); entry held"
    e, st = _num(ticket.get("entry")), _num(ticket.get("stop"))
    prem = [((_num(ticket.get(f)) or 0) - e) / (e - st) for f in ("target1", "target2")
            if e is not None and st is not None and e > st and _num(ticket.get(f)) is not None]
    prem_r = max(prem) if prem else None
    if prem_r is not None and prem_r >= min_rr - 1e-9 and und_r >= 1.0 - 1e-9:
        return True, f"C4: premium R {prem_r:.2f} ≥ {min_rr:g} and underlying target {und_r:.2f}× the stop distance"
    return False, (f"C4: underlying R {und_r:.2f} < {min_rr:g} and premium R "
                   f"{'n/a' if prem_r is None else f'{prem_r:.2f}'} / target distance {und_r:.2f}× do not qualify; entry held")


def vol_regime_factor(latest_doc: Optional[dict]) -> Tuple[Optional[float], str]:
    """R23: 1/4 if VIX9D/VIX > 1.10 and VVIX > 125; 1/2 if VIX9D/VIX > 1.00 or VIX/VIX3M > 1.00 or
    VVIX > 110; else 1. None (fail closed) if any input is missing."""
    s = ((latest_doc or {}).get("summary") or {})
    vix, v9, v3, vv = (_num(s.get(k)) for k in ("vix", "vix9d", "vix3m", "vvix"))
    if None in (vix, v9, v3, vv) or vix <= 0 or v3 <= 0:
        return None, "volatility regime inputs missing (VIX9D, VIX, VIX3M, VVIX)"
    a, b = v9 / vix, vix / v3
    txt = f"VIX9D/VIX {a:.3f}, VIX/VIX3M {b:.3f}, VVIX {vv:g}"
    if a > 1.10 and vv > 125:
        return 0.25, "vol regime quarter size (" + txt + ")"
    if a > 1.00 or b > 1.00 or vv > 110:
        return 0.5, "vol regime half size (" + txt + ")"
    return 1.0, txt


def grid_net_gex_at(latest_doc: Optional[dict], scope: str, spot: Optional[float]) -> Optional[float]:
    """Linear read of intraday_feed.gex_by_spot.<scope>.grid at spot (None outside the grid)."""
    if spot is None:
        return None
    grid = ((((latest_doc or {}).get("intraday_feed") or {}).get("gex_by_spot") or {}).get(scope) or {}).get("grid") or []
    pts = sorted((_num(g.get("spot")), _num(g.get("net_gex"))) for g in grid
                 if _num(g.get("spot")) is not None and _num(g.get("net_gex")) is not None)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= spot <= x1 and x1 > x0:
            return y0 + (y1 - y0) * (spot - x0) / (x1 - x0)
    return None


def net_gex_45d_conservative(latest_doc: Optional[dict] = None, spot: Optional[float] = None) -> Tuple[Optional[float], Dict[str, Any]]:
    """CIO rule (Nov 12 2026): wherever 45D net GEX sizes or gates, use the MORE NEGATIVE of
    (a) the GEX-by-spot grid reading at chain spot (intraday_feed.gex_by_spot.upcoming_45d
    .net_gex_at_chain_spot) and (b) the chain's printed-gamma 45D net GEX (summary
    .net_gex_45d_dollars, else gex_scopes.upcoming_45d.totals.net_gex). None if both missing."""
    j = latest_doc if latest_doc is not None else load_latest()
    j = j or {}

    def _f(v):
        try:
            v = float(v)
            return v if v == v and abs(v) != float("inf") else None
        except Exception:
            return None
    grid = _f((((j.get("intraday_feed") or {}).get("gex_by_spot") or {}).get("upcoming_45d") or {}).get("net_gex_at_chain_spot"))
    printed = _f((j.get("summary") or {}).get("net_gex_45d_dollars"))
    if printed is None:
        printed = _f((((j.get("gex_scopes") or {}).get("upcoming_45d") or {}).get("totals") or {}).get("net_gex"))
    at_spot = grid_net_gex_at(j, "upcoming_45d", spot)  # R22 "read at current spot"; only ever more conservative
    vals = [v for v in (grid, printed) if v is not None]
    used = min(vals) if vals else None
    if used is not None and at_spot is not None:
        used = min(used, at_spot)
    src = None if used is None else ("grid_at_chain_spot" if used == grid else
                                     ("printed_chain_gamma" if used == printed else "grid_at_current_spot"))
    return used, {"net_gex_45d_grid_at_chain_spot": grid, "net_gex_45d_printed": printed,
                  "net_gex_45d_grid_at_current_spot": at_spot,
                  "net_gex_45d_used": used, "net_gex_45d_source": src}


def _order_by_coid(pe, coid: str) -> Optional[dict]:
    """Exact lookup of one of OUR orders by client_order_id (not limited to the last N orders)."""
    try:
        o = pe.alpaca_request("GET", pe.ALPACA_PAPER_HOST, "/v2/orders:by_client_order_id",
                              params={"client_order_id": coid})
    except Exception:
        return None
    if isinstance(o, dict) and o.get("client_order_id") == coid and o.get("id"):
        return o
    return None


def compute_or(bars_5m: List[dict], now: datetime, rvol: Optional[dict] = None) -> Dict[str, Any]:
    """Opening ranges from COMPLETED 5m bars only (Nov 12 2026 fix).

    OR30 = 08:30-08:55 bars, ready once now >= 09:00 CT (breakout/continuation triggers).
    OR15 = 08:30-08:40 bars, ready once now >= 08:45 CT (reclaim triggers).
    RVOL is time-of-day RVOL (framework R14) passed in by the tick (see live_rvol); the old
    rvol_est (forming bar volume / OR-bar median) is retired. rvol_est is kept as an alias of
    tod_rvol for older readers. Missing RVOL stays None and every RVOL gate fails closed."""
    bars_5m = completed_bars(bars_5m, now)
    rv = rvol or {}
    tod = rv.get("tod_rvol")
    base = {"tod_rvol": tod, "bar_rvol": rv.get("bar_rvol"), "rvol_est": tod,
            "rvol_source": rv.get("source"), "rvol_note": rv.get("why"),
            "rvol_metric": "time-of-day RVOL (cumulative to last completed bar / 20-session same-slot average)"}
    or_bars = [b for b in bars_5m if (b["_dt"].hour, b["_dt"].minute) < OR30_END][:6]
    o15 = [b for b in bars_5m if (b["_dt"].hour, b["_dt"].minute) < OR15_END][:3]
    or15 = {"or15_ready": _hm(now) >= OR15_END and len(o15) >= 1,
            "or15_high": round(max(b["h"] for b in o15), 4) if o15 else None,
            "or15_low": round(min(b["l"] for b in o15), 4) if o15 else None}
    if or15["or15_high"] is not None:
        or15["or15_mid"] = round((or15["or15_high"] + or15["or15_low"]) / 2.0, 4)
    latest = bars_5m[-1] if bars_5m else None
    if not or_bars:
        return {"ready": False, "or_high": None, "or_low": None, "or_mid": None, "bar_count": 0, **base, **or15,
                "latest": None, "completed_bars": len(bars_5m)}
    ready = _hm(now) >= OR30_END and len(or_bars) >= 1
    hi = max(b["h"] for b in or_bars)
    lo = min(b["l"] for b in or_bars)
    mid = (hi + lo) / 2.0
    return {
        "ready": ready,
        "or_high": round(hi, 4),
        "or_low": round(lo, 4),
        "or_mid": round(mid, 4),
        "bar_count": len(or_bars),
        **base, **or15,
        "completed_bars": len(bars_5m),
        "latest": ({"o": latest["o"], "h": latest["h"], "l": latest["l"], "c": latest["c"], "t": latest["t"]} if latest else None),
        "prev_close": bars_5m[-2]["c"] if len(bars_5m) >= 2 else None,
    }


ORDER_SCAN_LIMIT = 500  # Alpaca maximum page size; tickets not in the page are looked up by client_order_id


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
            params={"status": "all", "limit": ORDER_SCAN_LIMIT, "direction": "desc"},
        ) or []
        for o in orders:
            if o.get("client_order_id") == coid:
                return True, f"alpaca_order:{o.get('status')}"
    except Exception as e:
        return False, f"order_check_warn:{e}"
    o = _order_by_coid(pe, coid)  # exact lookup, independent of how many orders came after it
    if o:
        return True, f"alpaca_order:{o.get('status')}"
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


def _parse_rvol_max(ut: dict, *texts: str) -> Optional[float]:
    """Upper RVOL bound written on a ticket (rvol_max, or 'RVOL < 1.2x' / 'RVOL <= 1.0x')."""
    if ut.get("rvol_max") is not None:
        try:
            return float(ut["rvol_max"])
        except Exception:
            pass
    for t in texts:
        m = _re.search(r"RVOL\s*(?:<=|≤|<)\s*(\d+(?:\.\d+)?)\s*x?", t or "", _re.I)
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


def _option_right(ticket: dict) -> Optional[str]:
    rt = str(ticket.get("right") or ticket.get("option_type") or "").lower()
    if rt in ("put", "p", "call", "c"):
        return "put" if rt.startswith("p") else "call"
    instr = str(ticket.get("instrument") or "").lower()
    if instr in ("option_put", "option_call"):
        return "put" if instr.endswith("put") else "call"
    return None


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
        "entry_start": None,
        "entry_end": ts.get("entry_end") or ENTRY_WINDOW_END,
        "eod_flat": ts.get("eod_flat") or EOD_FLAT,
        "time_stop": ts, "pre_invalidation": None, "inv": None, "retest": None, "fill_guard": None,
    }
    # Earliest entry (Nov 12 2026, CEO-approved C3): the configured paper_engine.earliest_entry_ct
    # (never before 08:45 CT). A ticket's own no_entry_before_ct / CIO-gate time can only make it later.
    # Breakout/continuation modes still need the completed 30-minute opening range (09:00 CT).
    t_start = _hhmm(ticket.get("no_entry_before_ct"))
    m = _re.search(r"no entry before\s*(\d{1,2}:\d{2})", gate, _re.I)
    if m and not t_start:
        t_start = _hhmm(m.group(1))
    floor = earliest_entry()
    r["entry_start"] = max(tuple(t_start), floor) if t_start else floor

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

    is_option = str(ticket.get("instrument") or "").lower().startswith("option")
    if is_option:
        # Option tickets are long premium; the UNDERLYING view comes from the right
        # (call = long SPY, put = short SPY). Entries are capped at 14:30 CT.
        right = _option_right(ticket)
        if direction not in ("", "long"):
            r["warnings"].append(f"{tid}: option direction '{direction}' not supported (long premium only)")
            return r
        if right is None:
            r["warnings"].append(f"{tid}: option right unknown (instrument option_put/option_call or right)")
            return r
        direction = "short" if right == "put" else "long"
        r.update({"direction": direction, "is_option": True, "right": right,
                  "entry_end": min(tuple(r["entry_end"]), ENTRY_WINDOW_END)})
    if direction not in ("long", "short"):
        r["warnings"].append(f"{tid}: direction '{direction}' not long/short")
        return r

    # choose the primary trigger text deterministically
    text, key = None, None
    for k in ("primary", "breakout", "breakdown", "failure", "reclaim", "trigger", "text"):
        if isinstance(ut.get(k), str) and ut[k].strip():
            text, key = ut[k], k
            break
    if not text:
        r["warnings"].append(f"{tid}: no recognised underlying_trigger text (keys={sorted(ut.keys())})")
        return r
    r["trigger_key"] = key
    r["rvol_min"] = _parse_rvol(ut, text, gate)
    r["rvol_max"] = _parse_rvol_max(ut, text, gate)

    fade = _re.search(r"into\s+" + _RANGE + r"\s+that\s+(FAILS|HOLDS)\s*\(\s*close back\s+(below|above)\s+" + _NUM,
                      text, _re.I)
    acc = _re.search(r"acceptance\s+(above|below)\D*?" + _NUM, text, _re.I)
    # Reclaim trigger (framework R4/R13): explicit type/key, or text that starts with "reclaim".
    # "reclaim above X": fire on completed close(s) >= X + reclaim_margin ($0.50 default; R13).
    rec = None
    if str(ut.get("type") or "").lower() == "reclaim" or key == "reclaim" or _re.match(r"\s*reclaim\b", text, _re.I):
        rec = _re.search(r"reclaim\s+(?:back\s+)?(above|below)\D*?" + _NUM, text, _re.I)
        if not rec:
            r["warnings"].append(f"{tid}: reclaim trigger without 'reclaim above|below LEVEL': {text[:120]!r}")
            return r
    if rec:
        side, lvl = rec.group(1).lower(), float(rec.group(2))
        if (side == "above") != (direction == "long"):
            r["warnings"].append(f"{tid}: reclaim {side} conflicts with direction {direction}")
            return r
        try:
            margin = float(ut["reclaim_margin"]) if ut.get("reclaim_margin") is not None else float(engine_config().get("reclaim_margin", 0.5))
        except Exception:
            margin = 0.5
        margin = max(0.0, margin)
        trig = lvl + margin if side == "above" else lvl - margin
        r.update({"mode": "reclaim", "level": lvl, "side": side, "reclaim_margin": margin,
                  "trigger_level": round(trig, 4), "hold_bars": _hold_bars(text, 1)})
        if r["rvol_min"] is None:
            r["rvol_min"] = RVOL_FULL  # R15 applies to reclaims too
        anchor = lvl
    elif fade:
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

    if r["rvol_min"] is None and not (r["mode"] == "wall_fade" and r.get("rvol_max") is not None):
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

    inv_ref = (r.get("level") if r["mode"] in ("acceptance", "reclaim") else r.get("confirm_level")) if r.get("is_option") else entry_f
    inv = _parse_hard_inv(str(ticket.get("hard_invalidation_underlying") or ""), direction, inv_ref,
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
    bars_5m = completed_bars(bars_5m, now)
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
    rvol = or_info.get("tod_rvol")
    rvol_ok = rvol is not None and rvol >= 1.3  # time-of-day RVOL; missing => no fire
    if tested and failed_wall and below_mid and (wick or red) and rvol_ok:
        return True, (
            f"OR/put-wall rejection: high {session_high:.2f} tested wall, 5m close {latest['c']:.2f} < OR_mid {or_mid:.2f} "
            f"(RVOL~{rvol or 'n/a'})"
        ), {"mode": "or_rejection", "spy": latest["c"], "or_mid": or_mid, "session_high": session_high, "rvol": rvol}
    return False, "underlying_trigger not met", {"spy": latest["c"], "or": or_info}


def evaluate_entry(ticket: dict, or_info: dict, bars_5m: List[dict], now: datetime,
                   rules: Optional[dict] = None) -> Tuple[bool, str, dict]:
    """Return (fired, reason, meta), driven by parse_ticket_rules(ticket).

    Nov 12 2026 (framework v1.2): COMPLETED 5m bars only; time-of-day RVOL (or_info tod_rvol) with
    R15 tiers for acceptance/reclaim; reclaim triggers may arm from the configured earliest entry
    (08:45 floor) on OR15, everything else needs the completed OR30 (09:00). meta["size_factor"]
    carries the R21 size factor (reclaim before 09:00 = 1/2, RVOL half tier = 1/2)."""
    rules = rules or parse_ticket_rules(ticket)
    if not rules.get("ok"):
        return False, "unparsed: " + "; ".join(rules.get("warnings") or ["unknown"]), {"mode": "unparsed"}
    bars_5m = completed_bars(bars_5m, now)
    mode = rules["mode"]
    es, ee = rules["entry_start"], rules["entry_end"]
    if rules.get("is_option") and is_zero_dte(ticket, now) and _hm(now) >= R24_NO_NEW_0DTE_AFTER:
        # R24 (v1.3): no new 0DTE entries after 13:30 CT (nothing at or after 13:30:00)
        return False, "outside entry window: R24 no new 0DTE entries after 13:30 CT", {}
    if not _in_window(now, es, ee):
        return False, f"outside entry window {es[0]:02d}:{es[1]:02d}–{ee[0]:02d}:{ee[1]:02d} CT", {}
    if mode == "reclaim":
        if not or_info.get("or15_ready") or not bars_5m:
            return False, "OR15 not ready (reclaim arms from 08:45 CT on completed bars)", {}
    elif not or_info.get("ready") or not bars_5m:
        return False, "OR not ready (breakout/continuation needs the completed 30-minute range, 09:00 CT)", {}
    latest = bars_5m[-1]

    if mode == "generic":
        if ticket.get("trigger_spy_below") is not None and latest["c"] < float(ticket["trigger_spy_below"]):
            return True, f"trigger_spy_below {ticket['trigger_spy_below']}", {"mode": "generic_below", "spy": latest["c"]}
        if ticket.get("trigger_spy_above") is not None and latest["c"] > float(ticket["trigger_spy_above"]):
            return True, f"trigger_spy_above {ticket['trigger_spy_above']}", {"mode": "generic_above", "spy": latest["c"]}
        return False, "no trigger", {"spy": latest["c"]}

    if mode == "legacy_or":
        return _evaluate_legacy(ticket, or_info, bars_5m, rules)

    # New-style tickets require a measured time-of-day RVOL (missing => no fire).
    rvol = or_info.get("tod_rvol")
    bar_rvol = or_info.get("bar_rvol")
    rvol_min = float(rules["rvol_min"]) if rules.get("rvol_min") is not None else None
    short = rules["direction"] == "short"
    meta = {"mode": mode, "spy": latest["c"], "rvol": rvol, "bar_rvol": bar_rvol, "rvol_min": rvol_min,
            "trigger_bar_ct": latest["_dt"].strftime("%H:%M")}
    factors: List[float] = []
    reasons: List[str] = []
    closes = [b["c"] for b in bars_5m if (b["_dt"].hour, b["_dt"].minute) >= (8, 30)]
    tier = None

    if mode in ("acceptance", "reclaim"):
        cfg = engine_config()
        full_min = max(rvol_min, RVOL_FULL)
        tier = rvol_tier(rvol, full_min, cfg["rvol_half_size_tier_enabled"])
        meta.update({"rvol_tier": tier["tier"], "rvol_full_min": full_min,
                     "rvol_half_tier_enabled": cfg["rvol_half_size_tier_enabled"]})
        lvl = rules["level"] if mode == "acceptance" else rules["trigger_level"]
        side = rules["side"]
        hold = int(rules["hold_bars"])
        if mode == "acceptance":
            cond = f"{hold}×5m close {side} {lvl:.2f}"
            accepted = _consec(closes, side, lvl, hold)
        else:
            # reclaim: close back inside by >= margin (inclusive at the margin)
            eps = 1e-9
            tail = closes[-hold:] if len(closes) >= hold else []
            accepted = bool(tail) and all((c >= lvl - eps) if side == "above" else (c <= lvl + eps) for c in tail)
            cond = f"{hold}×5m close {'≥' if side == 'above' else '≤'} {lvl:.2f} (reclaim of {rules['level']:.2f} by {rules['reclaim_margin']:.2f})"
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

    if tier is not None:
        if not tier["allow"]:
            return False, f"waiting: {tier['why']}", meta
        if tier["hold2"]:
            # half tier: 2-bar hold unless the trigger bar is >= 1.5x its slot
            sub = bar_rvol is not None and float(bar_rvol) >= BAR_RVOL_SUBSTITUTE
            if not sub:
                lvl = rules["level"] if mode == "acceptance" else rules["trigger_level"]
                n2 = max(2, int(rules["hold_bars"]))
                if mode == "acceptance":
                    ok2 = _consec(closes, rules["side"], lvl, n2)
                else:
                    tail = closes[-n2:] if len(closes) >= n2 else []
                    ok2 = bool(tail) and all((c >= lvl - 1e-9) if rules["side"] == "above" else (c <= lvl + 1e-9) for c in tail)
                if not ok2:
                    return False, f"waiting: half-size RVOL tier needs a {n2}-bar hold ({tier['why']})", meta
            factors.append(0.5); reasons.append("RVOL half tier")
        rv_txt = tier["why"]
    elif mode == "wall_fade":
        # CIO ruling (Nov 12 PM): BOTH R16 and the ticket's own RVOL rule; the stricter wins.
        # R16: disarm the fade when time-of-day RVOL >= 1.2x on the break; rejection bar <= 1.0x its slot.
        if rvol is None:
            return False, "waiting: RVOL unavailable (fail closed)", meta
        if rvol_min is not None and rvol < rvol_min:
            return False, f"waiting: RVOL {rvol:.2f} < ticket minimum {rvol_min}", meta
        cap = R16_FADE_DISARM_RVOL
        if rules.get("rvol_max") is not None:
            cap = min(cap, float(rules["rvol_max"]))
        if rvol >= cap - 1e-12:
            meta["r16_disarm"] = True
            return False, (f"waiting: fade disarmed (R16/ticket): RVOL {rvol:.2f} ≥ {cap:g}"
                           + ("; ticket minimum makes this fade unfireable" if rvol_min is not None and rvol_min >= cap else "")), meta
        if bar_rvol is None or float(bar_rvol) > R16_REJECTION_BAR_MAX + 1e-12:
            return False, (f"waiting: R16 rejection bar must be ≤ {R16_REJECTION_BAR_MAX:g}× its slot "
                           f"(bar {('n/a' if bar_rvol is None else f'{float(bar_rvol):.2f}')}×; fail closed when unavailable)"), meta
        rv_txt = (f"RVOL {rvol:.2f}" + (f" ≥ {rvol_min}" if rvol_min is not None else "") + f" and < {cap:g} (R16); "
                  f"rejection bar {float(bar_rvol):.2f}× ≤ {R16_REJECTION_BAR_MAX:g}×")
    else:
        if rvol is None or rvol < rvol_min:
            return False, f"waiting: RVOL {rvol if rvol is not None else 'n/a'} < {rvol_min} (fail closed when unavailable)", meta
        rv_txt = f"RVOL {rvol:.2f} ≥ {rvol_min}"

    if mode == "reclaim" and _hm(now) < OR30_END:
        factors.append(0.5); reasons.append("reclaim armed before 09:00 CT")
    meta["size_factor"] = combine_size_factors(factors)
    meta["size_factors"] = factors
    meta["size_reasons"] = reasons
    sz = f"; size ×{meta['size_factor']:g} ({', '.join(reasons)})" if reasons else ""
    return True, f"{mode}: {cond}; {rv_txt}" + (" + CIO retest/reject" if rt else "") + sz, meta


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
    """Return (occ_symbol, strike, limit_price) from EXPLICIT ticket fields (fail closed).

    Nov 11 2026: no default strike (prior_rth_low/732), expiry, premium or qty.
    The limit is the ticket's entry premium, which is also what the order builder sends."""
    spec, err = pe.option_contract_spec(ticket)
    if err:
        raise RuntimeError(f"option contract unresolved: {err}")
    if ticket.get("entry") is None:
        raise RuntimeError("option ticket missing entry premium")
    return spec["occ_symbol"], float(spec["strike"]), float(ticket["entry"])


def option_ticket_problem(pe, ticket: dict) -> Optional[str]:
    """None if an option ticket can be routed; otherwise why it must stay pending."""
    if _is_stock(ticket):
        return None
    try:
        spec, err = pe.option_contract_spec(ticket)
    except Exception as e:  # pragma: no cover
        return f"contract check error: {e}"
    if err:
        return err
    for f in ("entry", "stop", "target1"):
        try:
            if ticket.get(f) is None or float(ticket.get(f)) <= 0:
                return f"missing/invalid {f} premium"
        except Exception:
            return f"missing/invalid {f} premium"
    q = ticket.get("quantity")
    if not isinstance(q, int) or isinstance(q, bool) or q < 1:
        return "quantity (contracts) must be a positive int"
    return None


def _is_stock(ticket: dict) -> bool:
    return not str(ticket.get("instrument") or "stock").startswith("option")


def _governance():
    import importlib.util as _ilu
    sp = _ilu.spec_from_file_location("_pod_governance_submit", str(ROOT / "scripts" / "_pod_governance.py"))
    gm = _ilu.module_from_spec(sp)
    sp.loader.exec_module(gm)
    return gm


def submit_risk_check(ticket: dict, gm=None) -> Tuple[bool, str]:
    """Submit-time gate for every new entry (stock and option), Nov 11 2026 CIO tightening.
    Blocks when the kill switch is breached, when this ticket alone exceeds
    max_risk_per_ticket_pct, or when open risk (other submitted/filled tickets) plus this
    ticket would exceed max_total_open_risk_pct. Limits and the equity reference are read
    from governance unchanged (equity_reference flag left as configured, default OFF).
    Any error => blocked (fail closed)."""
    try:
        gm = gm or _governance()
        blocked, why = gm.kill_switch_blocks_new_entries()
        if blocked:
            lims = str(why).split(":", 1)[1].strip() if ":" in str(why) else ""
            lims = lims.replace("_pct", "").replace("_", " ")
            return False, "Kill switch breached; no new entries" + (f" ({lims})" if lims else "")
        limits = gm.rj(gm.LIMITS, {}) or {}
        lim = limits.get("limits") or {}
        eq = float(gm.equity_reference_value(limits))
        if eq <= 0:
            return False, "Risk check unavailable (no equity reference); entry held"
        max_ticket = float(lim.get("max_risk_per_ticket_pct") or 0.5)
        max_open = float(lim.get("max_total_open_risk_pct") or 1.0)
        tid = ticket.get("ticket_id")
        this_risk = float(gm.ticket_dollar_risk(ticket))
        if this_risk <= 0:
            return False, "Risk check unavailable (ticket risk undefined); entry held"
        other_open = sum(float(gm.ticket_dollar_risk(t)) for t in gm.active_blotter_tickets()
                         if t.get("status") in ("submitted_paper", "filled_paper") and t.get("ticket_id") != tid)
        this_pct = 100.0 * this_risk / eq
        open_pct = 100.0 * (other_open + this_risk) / eq
        if this_pct > max_ticket + 1e-9:
            return False, f"Ticket risk {this_pct:.3f}% (${this_risk:,.0f}) is above the {max_ticket}% per-ticket limit"
        if open_pct > max_open + 1e-9:
            return False, (f"Open risk would reach {open_pct:.3f}% (${other_open + this_risk:,.0f}), "
                           f"above the {max_open}% total open-risk limit")
        return True, f"Risk check passed: ticket {this_pct:.3f}% · open risk after fill {open_pct:.3f}% of {max_open}%"
    except Exception as e:
        print(f"[alpaca_rth] risk check error: {e}", file=sys.stderr)
        return False, "Risk check unavailable; entry held (fail closed)"


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
    ok, risk_why = submit_risk_check(ticket)
    if not ok:
        # Stays unsubmitted (status unchanged); reason logged once per distinct reason.
        if not any(e.get("ticket_id") == tid and e.get("kind") == "SUBMIT_BLOCKED" and e.get("detail") == risk_why
                   for e in events_doc.get("events") or []):
            append_event(events_doc, "SUBMIT_BLOCKED", tid, f"Entry not submitted: {tid}", risk_why,
                         severity="neg", symbol=occ)
        print(f"[alpaca_rth] submit blocked {tid}: {risk_why}", file=sys.stderr)
        return {"mode": "blocked", "submitted": False, "reason": risk_why, "occ": occ}
    if force_sim:
        # Never transmit under force-sim
        append_event(
            events_doc, "TRIGGER_SIM", tid,
            f"[SIM] Trigger hit — would {side_word} {qty}× {occ} @ ${limit:.2f} LMT",
            f"{reason}. OR {or_info.get('or_low')}–{or_info.get('or_high')}. force_sim=1 (no order sent).",
            severity="warn", symbol=occ, extra=(dict(ticket["gex45_check"]) if ticket.get("gex45_check") else None),
        )
        return {"mode": "force_sim", "submitted": False, "occ": occ, "limit": limit}

    # Use existing stage path
    if _is_stock(ticket):
        result = pe.alpaca_stage_ticket(tid, dry_run=False, submit_paper=True)
    else:
        result = pe.alpaca_stage_ticket(tid, dry_run=False, submit_paper=True, occ_symbol=occ)
    append_event(
        events_doc, "ORDER_SUBMITTED", tid,
        f"Alpaca Paper Order Submitted: {side_word} {qty}× {occ} @ ${limit:.2f} LMT",
        f"Trigger hit ({reason}). Firewall 6/6 PASS (${ticket.get('actual_dollar_risk', 120)} max risk). OR "
        f"${or_info.get('or_low')}–${or_info.get('or_high')}.",
        severity="pos", symbol=occ,
        extra={"alpaca_order_id": (result.get("response") or {}).get("id") if isinstance(result.get("response"), dict) else ticket.get("alpaca_order_id"),
               **(dict(ticket["gex45_check"]) if ticket.get("gex45_check") else {})},
    )
    return {"mode": "submitted_paper", "submitted": True, "result": result, "occ": occ, "limit": limit}


def _log_once(events_doc: dict, kind: str, tid: str, headline: str, detail: str, symbol: str = "",
              severity: str = "neg") -> None:
    if not any(e.get("ticket_id") == tid and e.get("kind") == kind and e.get("detail") == detail
               for e in events_doc.get("events") or []):
        append_event(events_doc, kind, tid, headline, detail, severity=severity, symbol=symbol)


def arm_time_gates(pe, ticket: dict, rules: dict, meta: dict, bars_5m: List[dict], now: datetime,
                   events_doc: dict, force_sim: bool, latest_doc: Optional[dict] = None,
                   cfg: Optional[dict] = None) -> Dict[str, Any]:
    """Checks between a trigger firing and order submission (Nov 12 2026, all tightening):
      1) firewall check 5 with LIVE 0DTE walls (fail closed);
      2) option premiums repriced from a live quote (R24) or the ticket is voided;
      3) size factor (reclaim before 09:00 / RVOL half tier) applied to the quantity.
    Returns {"action": "submit"|"hold"|"void", "ticket": ticket_to_submit, "why": str}.
    Staged values are kept on the ticket as staged_* and every re-arm recomputes from them."""
    cfg = cfg or engine_config()
    tid = ticket.get("ticket_id")
    done = completed_bars(bars_5m, now)
    spot = done[-1]["c"] if done else None
    wc = wall_check(spot, now, latest_doc, cfg)
    why = wc["why"]
    if not wc["ok"]:
        out = {"action": "hold", "ticket": ticket, "why": why}
        if wc["code"] == "stale":
            out.update({"kind": "WALL_STALE", "wall_age_min": wc["age_min"]})
        return out
    if latest_doc is None:
        latest_doc = load_latest()
    g45, g45_info = net_gex_45d_conservative(latest_doc, spot)
    g45_txt = (f"45D net GEX grid {g45_info['net_gex_45d_grid_at_chain_spot']} / printed "
               f"{g45_info['net_gex_45d_printed']} → used {g45}")
    if g45 is None:
        return {"action": "hold", "ticket": ticket, "gex45": g45_info,
                "why": "45D net GEX unavailable (grid and printed both missing); entry held (fail closed)"}
    factors = list(meta.get("size_factors") or ([] if float(meta.get("size_factor") or 1.0) >= 1.0 else [float(meta["size_factor"])]))
    reasons = list(meta.get("size_reasons") or [])
    if g45 < GEX45_CAP_DOLLARS:
        factors.append(0.5); reasons.append("45D net GEX < -$5B (more negative of grid/printed)")
    vf, vwhy = vol_regime_factor(latest_doc)  # R23
    if vf is None:
        return {"action": "hold", "ticket": ticket, "gex45": g45_info, "why": f"R23 {vwhy}; entry held (fail closed)"}
    if vf < 1.0:
        factors.append(vf); reasons.append(vwhy)
    # R21 is relative to the 0.25% base: factors the card already applied when staging
    # (ticket.size_factors_applied) are counted once, never multiplied in again.
    pre = [float(x) for x in (ticket.get("size_factors_applied") or []) if _num(x) is not None and 0 < float(x) < 1]
    total = combine_size_factors(pre + factors)
    rel = total / combine_size_factors(pre) if pre else total
    meta = {**meta, "size_factor": min(1.0, rel), "size_factors": factors, "size_reasons": reasons,
            "size_factor_vs_base": total, "size_factors_applied_at_staging": pre}
    t = dict(ticket)
    t["gex45_check"] = {**g45_info, "cap_applied": g45 < GEX45_CAP_DOLLARS}
    mult = 100 if not _is_stock(t) else 1
    if "staged_entry" not in t:  # first arm: freeze the staged card values once
        for f in ("entry", "stop", "target1", "target2", "quantity", "actual_dollar_risk"):
            t[f"staged_{f}"] = t.get(f)
        if t.get("staged_actual_dollar_risk") is None:
            t["staged_actual_dollar_risk"] = round(abs(float(t["entry"]) - float(t["stop"])) * int(t["quantity"]) * mult, 2)
    for f in ("entry", "stop", "target1", "target2", "quantity", "actual_dollar_risk"):
        t[f] = t.get(f"staged_{f}")
    if not _is_stock(t):
        c4ok, c4why = c4_check(t, rules, spot, now)
        if not c4ok:
            return {"action": "hold", "ticket": ticket, "gex45": g45_info, "why": c4why}
        occ, _, _ = resolve_occ(pe, t)
        quote, qwhy = option_live_quote(pe, occ)
        if quote is None:  # one retry inside the tick before voiding
            quote, qwhy = option_live_quote(pe, occ)
        rp, rwhy = reprice_option_ticket(t, rules, quote, spot, now, cfg)
        if rp is None:
            if quote is not None and rwhy.startswith("contract delta"):
                return {"action": "void", "ticket": ticket, "plain": "Voided: contract no longer in the 0.35–0.50 delta band",
                        "why": f"R24 contract spec: {rwhy}; ticket voided (no chase)"}
            if quote is not None and ("budget" in rwhy or "invalid" in rwhy):
                return {"action": "void", "ticket": ticket, "plain": "Voided: live premium risk above the approved budget",
                        "why": f"Stale premium: {rwhy}; ticket voided (no chase)"}
            return {"action": "void", "ticket": ticket, "plain": "Voided: quote stale or spread too wide",
                    "why": f"Stale premium: {rwhy if quote else qwhy}; ticket voided (no chase)"}
        t.update({k: v for k, v in rp.items() if k != "reprice"})
        t["arm_reprice"] = rp["reprice"]
        t["oco_bracket"] = {**(t.get("oco_bracket") or {}), "stop_order": t["stop"],
                            "take_profit_1": t.get("target1"), "take_profit_2": t.get("target2")}
    factor = float(meta.get("size_factor") or 1.0)
    q0 = int(t.get("quantity") or 0)
    q = int(q0 * factor) if factor < 1.0 else q0
    if q < 1:
        # CIO ruling 7: round down; 0 contracts => SKIP the ticket for this trigger, never a full contract
        return {"action": "skip", "ticket": ticket, "kind": "ENTRY_SKIPPED",
                "why": f"Size ×{factor:g} ({', '.join(meta.get('size_reasons') or [])}) rounds {q0} to 0; ticket skipped (never a full contract)"}
    t["quantity"] = q
    t["size_factor"] = factor
    t["size_factor_vs_base"] = meta.get("size_factor_vs_base", factor)
    t["size_reasons"] = meta.get("size_reasons") or []
    t["actual_dollar_risk"] = round(abs(float(t["entry"]) - float(t["stop"])) * q * mult, 2)
    return {"action": "submit", "ticket": t, "why": f"{why}; {g45_txt}", "gex45": g45_info}


def fetch_option_completed_bars(pe, occ: str, now: datetime) -> List[dict]:
    """Today's COMPLETED 5m bars for one option contract from the Alpaca data host (read only).
    Each bar: {"_dt": bar start (CT), "c": close}. Empty list on any error."""
    try:
        start = now.astimezone(CT).replace(hour=8, minute=30, second=0, microsecond=0).astimezone(timezone.utc)
        d = pe.alpaca_request("GET", pe.ALPACA_DATA_HOST, "/v1beta1/options/bars",
                              params={"symbols": occ, "timeframe": "5Min", "limit": 200,
                                      "start": start.strftime("%Y-%m-%dT%H:%M:%SZ")}, timeout=15) or {}
        raw = ((d.get("bars") or {}).get(occ) or []) if isinstance(d, dict) else []
        out = []
        for b in raw:
            dt = parse_bar_ts(str(b.get("t"))).astimezone(CT)
            out.append({"_dt": dt, "c": float(b["c"])})
        return [b for b in out if b["_dt"] + timedelta(minutes=BAR_MINUTES) <= now]
    except Exception:
        return []


def completed_progress_price(bars: List[dict], since: Optional[datetime], now: datetime) -> Optional[Tuple[float, str]]:
    """Close of the latest completed 5m bar that ENDED after `since` (the fill). None if none."""
    best = None
    for b in bars or []:
        dt = b.get("_dt") or _bar_dt(b)
        end = dt + timedelta(minutes=BAR_MINUTES)
        if end > now or (since is not None and end <= since):
            continue
        if best is None or dt > best[0]:
            best = (dt, float(b["c"]))
    return None if best is None else (best[1], best[0].strftime("%H:%M"))


ACTIVE_STATUSES = {"submitted_paper", "filled_paper"}
OPEN_ENTRY_STATES = {"new", "accepted", "pending_new", "accepted_for_bidding", "held", "pending_replace", "replaced"}


def manage_open_positions(pe, events_doc: dict, or_info: dict, bars_5m: List[dict], now: datetime, force_sim: bool) -> List[str]:
    """Manage filled paper positions for blotter tickets. Rules come from each ticket
    (parse_ticket_rules). Returns action notes."""
    notes = []
    try:
        positions = pe.alpaca_request("GET", pe.ALPACA_PAPER_HOST, "/v2/positions") or []
        orders = pe.alpaca_request(
            "GET", pe.ALPACA_PAPER_HOST, "/v2/orders",
            params={"status": "all", "limit": ORDER_SCAN_LIMIT, "direction": "desc"},
        ) or []
    except Exception as e:
        return [f"manage_skip:{e}"]

    by_coid = {o.get("client_order_id"): o for o in orders if o.get("client_order_id")}
    rows = blotter_rows()
    # Nov 12 2026: orders older than the listed page are fetched by OUR client_order_id, so the
    # 14:30 cancel, fill detection and breakeven never miss a ticket because of paging.
    for t in rows:
        if t.get("status") not in ACTIVE_STATUSES or not t.get("ticket_id"):
            continue
        for coid in (client_order_id(t["ticket_id"]), f"horsey-t1-{t['ticket_id']}"[:48]):
            if coid in by_coid:
                continue
            if coid.startswith("horsey-t1-") and not any(
                    e.get("ticket_id") == t["ticket_id"] and e.get("kind") == "TARGET1_SCALED"
                    for e in events_doc.get("events") or []):
                continue
            o = _order_by_coid(pe, coid)
            if o:
                by_coid[coid] = o
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
        elif (entry_order and entry_order.get("status") == "partially_filled"
              and float(entry_order.get("filled_qty") or 0) > 0 and not ticket.get("alpaca_filled_ct")):
            # Partial fill: start the time-stop clock at the first observed fill. Exits manage
            # the filled qty, and the resting remainder is cancelled by the per-contract cancel.
            ts_src = entry_order.get("filled_at") or entry_order.get("updated_at")
            try:
                first_ct = _iso_ct(parse_bar_ts(ts_src)) if ts_src else _iso_ct(now)
            except Exception:
                first_ct = _iso_ct(now)
            append_event(
                events_doc, "ORDER_PARTIAL_FILL", tid,
                f"Alpaca Paper PARTIAL FILL: {entry_order.get('symbol')} {entry_order.get('filled_qty')} of {entry_order.get('qty')}",
                f"avg {entry_order.get('filled_avg_price')} · order {entry_order.get('id')}",
                severity="pos", symbol=entry_order.get("symbol") or "",
            )
            rewrite_blotter_ticket(tid, lambda t: {**t, "alpaca_filled_ct": first_ct,
                                                   "alpaca_partial_fill_qty": entry_order.get("filled_qty")})
            ticket = {**ticket, "alpaca_filled_ct": first_ct, "alpaca_partial_fill_qty": entry_order.get("filled_qty")}
            notes.append(f"partial_fill:{tid}")

        # 14:30 CT: cancel an entry order that is still completely unfilled (that order only,
        # by id; never cancel-all). Partially/fully filled entries and positions are left alone.
        if (ticket.get("status") == "submitted_paper" and entry_order and _hm(now) >= ENTRY_WINDOW_END
                and entry_order.get("client_order_id") == coid
                and entry_order.get("status") in OPEN_ENTRY_STATES and float(entry_order.get("filled_qty") or 0) == 0
                and entry_order.get("id")):
            sym = entry_order.get("symbol") or ""
            if force_sim:
                append_event(events_doc, "ENTRY_CANCELLED", tid, f"[SIM] Unfilled entry would be cancelled: {sym}",
                             "No fill by 14:30 CT (simulation, nothing sent)", "warn", sym)
                notes.append(f"sim:ENTRY_CANCELLED:{tid}")
                continue
            try:
                pe.alpaca_request("DELETE", pe.ALPACA_PAPER_HOST, f"/v2/orders/{entry_order['id']}")
                append_event(events_doc, "ENTRY_CANCELLED", tid, f"Unfilled entry cancelled at 14:30 CT: {sym}",
                             "No fill by 14:30 CT; this entry order was cancelled (no new risk after 14:30 CT)",
                             "warn", sym, extra={"alpaca_order_id": entry_order.get("id")})
                rewrite_blotter_ticket(tid, lambda t: {**t, "status": "cancelled_paper",
                                                       "alpaca_cancel_reason": "unfilled_at_1430", "alpaca_cancelled_ct": _iso_ct()})
                notes.append(f"ENTRY_CANCELLED:{tid}")
            except Exception as e:
                notes.append(f"cancel_fail:{tid}:{e}")
            continue

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

        # CIO ruling 5 (Nov 12 PM): protective stops / invalidation / kill switch / EOD flat act on
        # live or forming prices (below, unchanged). Scale-outs (T1/T2) and time-stop R use the
        # close of the last COMPLETED 5m bar that ended after the fill (option bars for options,
        # underlying bars for stock tickets).
        _fa = ticket.get("alpaca_filled_ct") or (entry_order or {}).get("filled_at")
        _since = None
        try:
            if isinstance(_fa, str) and "T" in _fa:
                _since = parse_bar_ts(_fa) if _fa.endswith("Z") or "+" in _fa[-6:] else datetime.fromisoformat(_fa)
                if _since.tzinfo is None:
                    _since = _since.replace(tzinfo=CT)
                _since = _since.astimezone(CT)
        except Exception:
            _since = None
        _src = (completed_bars(bars_5m, now) if stock else fetch_option_completed_bars(pe, occ, now))
        _pp = completed_progress_price(_src, _since, now)
        prog_px = _pp[0] if _pp else None
        prog_txt = f"completed {_pp[1]} bar close {_pp[0]}" if _pp else "no completed bar since fill"

        # Breakeven: once the T1 scale-out has (partially) filled, the remaining contracts'
        # stop moves to the entry premium. Persisted on the ticket so it survives order-list paging.
        be_note = ""
        if not stock and direction == "long":
            be = ticket.get("alpaca_breakeven_stop")
            t1_order = by_coid.get(f"horsey-t1-{tid}"[:48])
            if be is None and t1_order and float(t1_order.get("filled_qty") or 0) > 0 and entry:
                be = round(entry, 2)
                append_event(events_doc, "STOP_TO_BREAKEVEN", tid,
                             f"Stop moved to breakeven: {int(qty)}× {occ} @ ${be:.2f}",
                             f"T1 filled {int(float(t1_order.get('filled_qty') or 0))} · remaining stop ${stop:.2f} → ${be:.2f} entry premium", "pos", occ)
                rewrite_blotter_ticket(tid, lambda t, _be=be: {**t, "alpaca_breakeven_stop": _be, "alpaca_breakeven_ct": _iso_ct()})
                ticket = {**ticket, "alpaca_breakeven_stop": be}
                notes.append(f"BREAKEVEN:{tid}")
            if be is not None and float(be) > stop:
                stop = float(be)
                be_note = " (breakeven stop)"

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
                    params={"status": "open", "limit": ORDER_SCAN_LIMIT, "symbols": occ},
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
            why = f"mark {mark} {'<=' if direction == 'long' else '>='} stop {stop}{be_note}" if stop_hit else und_why
            _close("STOP_EXIT", f"{'BREAKEVEN STOP' if (stop_hit and be_note) else 'STOP EXIT'}: {verb} {int(qty)}× {occ}", why)
            continue

        # Targets. Stock tickets carry a native broker bracket (stop + take-profit at T1),
        # so T1/T2 scaling stays at the broker. Options are scaled here.
        if not stock:
            t1_done = any(e.get("ticket_id") == tid and e.get("kind") == "TARGET1_SCALED" for e in events_doc.get("events") or [])
            if prog_px is not None and prog_px >= t1 and qty >= 2 and not t1_done:
                if force_sim:
                    append_event(events_doc, "TARGET1_SCALED", tid, f"[SIM] T1 scale {occ}", f"{prog_txt} >= {t1}", "pos", occ)
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
                                     f"T1 limit order placed ({prog_txt}); remaining stop moves to breakeven (entry {entry}) once T1 fills",
                                     "pos", occ)
                        notes.append(f"T1:{tid}")
                    except Exception as e:
                        notes.append(f"T1_fail:{e}")

            t2_done = any(e.get("ticket_id") == tid and e.get("kind") == "TARGET2_CLOSED" for e in events_doc.get("events") or [])
            if prog_px is not None and prog_px >= t2 and not t2_done:
                _close("TARGET2_CLOSED", f"TARGET2 CLOSED: sold remaining {occ}", f"{prog_txt} >= {t2}")
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
                    # time-stop R on the last completed bar close; the live mark is used only when no
                    # bar has completed since the fill (illiquid contract), and that is labelled.
                    _px = prog_px if prog_px is not None else mark
                    _lbl = prog_txt if prog_px is not None else f"live mark {mark} (no completed bar since fill)"
                    pnl_r = (sgn * (_px - entry) / risk_unit) if risk_unit else 0
                    if not stock and rules.get("ok") and is_zero_dte(ticket, now):
                        # R24 (v1.3) + P1: 30-minute time stop if progress < +0.5R, R measured on the premium
                        if mins >= R24_TIME_STOP_MIN and pnl_r < R24_TIME_STOP_R:
                            _close("TIME_STOP_EXIT", f"TIME STOP: flat {occ}",
                                   f"R24: {mins:.0f}m since fill · premium PnL {pnl_r:.2f}R < {R24_TIME_STOP_R}R ({_lbl})")
                            continue
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
                                   f"{mins:.0f}m since fill · PnL {pnl_r:.2f}R < {ts.get('min_progress_r', 0.0)}R ({_lbl})")
                            continue
            except Exception:
                pass

        # EOD flat (ticket time_stop 'flat ... by HH:MM', default 14:45 CT) on session day
        exp = (ticket.get("session_target") or ticket.get("option_expiration") or "")
        today = now.strftime("%Y-%m-%d")
        eod = tuple((rules or {}).get("eod_flat") or EOD_FLAT)
        if not stock:
            try:
                spec, _err = pe.option_contract_spec(ticket)
            except Exception:
                spec = None
            if spec and spec.get("expiry") == today:  # 0DTE: flat by 14:45 CT at the latest
                exp = today
                eod = min(eod, EOD_FLAT)
        if exp == today and _hm(now) >= eod:
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

    cfg = engine_config()
    latest_doc = load_latest()
    all_bars = bars                       # exits keep today's behaviour (all bars)
    bars = completed_bars(all_bars, now)  # triggers: completed 5m bars only
    rv = live_rvol(now, bars, latest_doc) if bars else {"tod_rvol": None, "bar_rvol": None, "why": "no completed bars"}
    or_info = compute_or(bars, now, rvol=rv)
    fed_blocked, fed_why, fed_warn = fed_speaker_blackout(now)
    if fed_warn:
        result.setdefault("warnings", []).append(fed_warn)
        print(f"[alpaca_rth] WARNING {fed_warn}", file=sys.stderr)
    result["engine"] = {"earliest_entry_ct": "%02d:%02d" % earliest_entry(cfg),
                        "rvol_half_size_tier_enabled": cfg["rvol_half_size_tier_enabled"],
                        "fed_blackout": fed_why or None}
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
    notes = manage_open_positions(pe, events_doc, or_info, all_bars, now, force_sim=force_sim)
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
        opt_problem = option_ticket_problem(pe, ticket)
        if opt_problem:
            # Option contract not fully specified: keep pending, never submit or default.
            warn = f"{tid}: option ticket left pending: {opt_problem}"
            result.setdefault("warnings", []).append(warn)
            result["actions"].append(f"pending_invalid_option:{tid}")
            print(f"[alpaca_rth] WARNING {warn}", file=sys.stderr)
            continue

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
            fired, reason, meta = evaluate_entry(ticket, or_info, bars, now, rules)
            result["actions"].append(f"sim_eval:{tid}:{fired}:{reason}")
            continue

        if fed_blocked:
            result["actions"].append(f"blackout:{tid}:{fed_why}")
            continue

        fired, reason, meta = evaluate_entry(ticket, or_info, bars, now, rules)
        if not fired:
            result["actions"].append(f"wait:{tid}:{reason}")
            continue

        # Real RTH trigger — arm-time gates, then submit (unless force_sim)
        try:
            g = arm_time_gates(pe, ticket, rules, meta, bars, now, events_doc, force_sim, latest_doc, cfg)
            sym = ((ticket.get("alpaca_paper_payload") or {}).get("order") or {}).get("symbol") or ticket.get("symbol") or ""
            if g["action"] == "hold" and g.get("kind") == "WALL_STALE":
                age = g.get("wall_age_min")
                snap = (now - timedelta(minutes=float(age))).strftime("%Y-%m-%dT%H:%M") if age is not None else "unknown"
                if not any(e.get("ticket_id") == tid and e.get("kind") == "WALL_STALE" and e.get("wall_snapshot_ct") == snap
                           for e in events_doc.get("events") or []):
                    append_event(events_doc, "WALL_STALE", tid, f"Entry held: 0DTE wall data stale ({tid})", g["why"],
                                 severity="warn", symbol=sym,
                                 extra={"reason_code": "WALL_STALE", "wall_age_min": age, "wall_snapshot_ct": snap})
                changed = True
                result["actions"].append(f"hold:WALL_STALE:{tid}:{age}")
                continue
            if g["action"] == "skip":
                if force_sim:
                    append_event(events_doc, "ENTRY_SKIPPED", tid, f"[SIM] Entry would be skipped: {tid}", g["why"], "warn", sym)
                else:
                    rewrite_blotter_ticket(tid, lambda t, _w=g["why"]: {**t, "status": "skipped_paper", "skip_reason": _w,
                                                                     "skip_reason_plain": "Skipped: reduced size rounds to zero contracts",
                                                                     "skipped_ct": _iso_ct()})
                    append_event(events_doc, "ENTRY_SKIPPED", tid, f"Entry skipped (size rounds to zero): {tid}", g["why"], "warn", sym)
                changed = True
                result["actions"].append(f"skipped:{tid}:{g['why']}")
                continue
            if g["action"] == "hold":
                _log_once(events_doc, "SUBMIT_BLOCKED", tid, f"Entry not submitted: {tid}", g["why"], sym)
                if g.get("gex45"):
                    for e in reversed(events_doc.get("events") or []):
                        if e.get("ticket_id") == tid and e.get("kind") == "SUBMIT_BLOCKED":
                            e.update(g["gex45"]); break
                changed = True
                result["actions"].append(f"hold:{tid}:{g['why']}")
                continue
            if g["action"] == "void":
                if force_sim:
                    append_event(events_doc, "TICKET_VOIDED", tid, f"[SIM] Ticket would be voided: {tid}", g["why"], "warn", sym)
                else:
                    rewrite_blotter_ticket(tid, lambda t, _w=g["why"], _p=g.get("plain"): {
                        **t, "status": "voided_paper", "void_reason": _w,
                        "void_reason_plain": _p or "Voided: quote stale or spread too wide", "voided_ct": _iso_ct()})
                    append_event(events_doc, "TICKET_VOIDED", tid, f"Ticket voided at arm time: {tid}", g["why"], "neg", sym)
                changed = True
                result["actions"].append(f"voided:{tid}:{g['why']}")
                continue
            ticket = g["ticket"]
            if not force_sim:
                _new = ticket
                rewrite_blotter_ticket(tid, lambda t, _n=_new: {**t, **{k: _n[k] for k in _n if k.startswith("staged_") or k in (
                    "entry", "stop", "target1", "target2", "quantity", "actual_dollar_risk", "oco_bracket",
                    "arm_reprice", "size_factor", "size_factor_vs_base", "size_reasons", "gex45_check")}})
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

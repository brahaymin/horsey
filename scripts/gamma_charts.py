#!/usr/bin/env python3
"""Morning SPY charts (dark, TradingView-style) drawn with matplotlib from real archived data only.

  (a) "SPY <Date> Outlook – Gamma Regime Rotation": prior-session 5-minute candles, prior close,
      model gamma flip, POC, call/put walls (+ HVL model and value area when present), and up to two
      scenario arrows ONLY from a CIO-approved pre-market card for that date.
  (b) "SPY <Date> Daily Range Sketch": last 20 daily candles, the same key levels, the prior-session
      value area band, and the card's paths (if any).

Levels come from the Daily Gamma Tracker row for the date (history/daily_tracker.jsonl, written by the
pre-open archive run). No row -> no charts (exit 3). No approved card -> no arrows, no scenario text.
Card file (CIO-owned, local): pod/premarket_cards/<YYYY-MM-DD>.json
  {"date": "YYYY-MM-DD", "approved_by": "CIO & Risk Gatekeeper", "approved": true,
   "scenarios": [{"label": "...", "path": [p1, p2, ...]}, {"label": "...", "path": [...]}]}
Outputs (gitignored, under pod/): pod/gamma_charts/<date>_outlook.png, <date>_range.png, latest.json
(levels + provenance) and latest_images.json (base64 data URIs the encrypted vault carries).
Run: /workspace/gamma_charts_venv/bin/python scripts/gamma_charts.py --date YYYY-MM-DD
"""
import argparse, base64, datetime as dt, json, math, os, sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("HORSEY_ROOT", Path(__file__).resolve().parents[1]))
NY = ZoneInfo("America/New_York"); CT = ZoneInfo("America/Chicago")
BG, PANEL, GRID, TXT, MUT = "#131722", "#131722", "#1e222d", "#d1d4dc", "#787b86"
UP, DN = "#26a69a", "#ef5350"
LEVEL_STYLE = {  # key: (label, color, linestyle)
    "prior_close": ("Prior close", "#d1d4dc", "--"),
    "premarket": ("Pre-market", "#f7c948", ":"),
    "flip_45d": ("Gamma flip (model)", "#ff9800", "-"),
    "hvl_45d": ("HVL (model)", "#b388ff", "-."),
    "poc": ("POC", "#00bcd4", "-"),
    "call_wall_45d": ("Call wall", "#26a69a", "-"),
    "put_wall_45d": ("Put wall", "#ef5350", "-"),
}
SCEN_COLORS = ["#4caf50", "#ff5252"]
MAX_CARD_DEVIATION = 0.05  # card path prices must sit within ±5% of the prior close


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) else None


def load_tracker_row(root, day):
    p = Path(root) / "history" / "daily_tracker.jsonl"
    if not p.exists():
        return None
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("date") == day:
            return r
    return None


def load_card(root, day, prior_close):
    """Return (scenarios, reason). Only an explicitly approved card for exactly `day` counts."""
    p = Path(root) / "pod" / "premarket_cards" / f"{day}.json"
    if not p.exists():
        return [], "no CIO-approved card for this date"
    try:
        c = json.loads(p.read_text())
    except Exception:
        return [], "card unreadable"
    if not isinstance(c, dict) or c.get("date") != day or c.get("approved") is not True or "CIO" not in str(c.get("approved_by") or ""):
        return [], "card not approved by the CIO for this date"
    out = []
    for s in (c.get("scenarios") or [])[:2]:
        if not isinstance(s, dict) or not isinstance(s.get("label"), str) or not s["label"].strip():
            return [], "card scenario malformed"
        path = [_num(x) for x in (s.get("path") or [])]
        if not (1 <= len(path) <= 5) or any(x is None for x in path):
            return [], "card scenario path malformed"
        if prior_close and any(abs(x / prior_close - 1) > MAX_CARD_DEVIATION for x in path):
            return [], "card scenario price outside sanity band"
        out.append({"label": s["label"].strip()[:80], "path": path})
    return out, ("ok" if out else "card has no scenarios")


def _read_table(path):
    import pandas as pd
    if not Path(path).exists():
        return None
    df = pd.read_json(path, orient="table")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def prior_session_bars(intra, day):
    """5-minute RTH candles of the latest session strictly before `day`."""
    import pandas as pd
    if intra is None or intra.empty:
        return None, None
    df = intra.copy(); df.index = df.index.tz_convert(NY)
    d = dt.date.fromisoformat(day)
    df = df[(df.index.date < d) & (df.index.time >= dt.time(9, 30)) & (df.index.time < dt.time(16, 0))]
    if df.empty:
        return None, None
    sess = df.index[-1].date(); df = df[df.index.date == sess]
    o = df.resample("5min").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    return o, sess


def daily_bars(hist, day, n=20):
    if hist is None or hist.empty:
        return None
    df = hist.copy(); df.index = df.index.tz_convert(NY)
    df = df[df.index.date < dt.date.fromisoformat(day)].dropna(subset=["Close"])
    return df.tail(n)


def _style(ax, fig):
    fig.patch.set_facecolor(BG); ax.set_facecolor(PANEL)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.tick_params(colors=MUT, labelsize=9)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.yaxis.tick_right()


def _candles(ax, df):
    xs = list(range(len(df)))
    for x, (_, r) in zip(xs, df.iterrows()):
        c = UP if r.Close >= r.Open else DN
        ax.vlines(x, r.Low, r.High, color=c, linewidth=0.9)
        lo, hi = sorted([r.Open, r.Close])
        ax.add_patch(__import__("matplotlib.patches", fromlist=["Rectangle"]).Rectangle((x - 0.32, lo), 0.64, max(hi - lo, 0.01), color=c))
    return xs


def _levels(ax, levels, x0, x1, ylo, yhi):
    """Horizontal key levels with de-overlapped right-edge labels."""
    items = sorted([(v, k) for k, v in levels.items() if v is not None and ylo <= v <= yhi])
    min_gap = (yhi - ylo) * 0.035; placed = []
    for v, k in items:
        label, color, ls = LEVEL_STYLE[k]
        ax.hlines(v, x0, x1, colors=color, linestyles=ls, linewidth=1.3, alpha=0.95)
        y = v if not placed or v - placed[-1] >= min_gap else placed[-1] + min_gap
        placed.append(y)
        ax.text(x1, y, f" {label} {v:,.2f}", color=color, fontsize=8.5, va="center", ha="left",
                bbox=dict(boxstyle="round,pad=0.2", fc=BG, ec=color, lw=0.6))


def _arrows(ax, scenarios, x_start, x_end, p_start):
    handles = []
    for i, s in enumerate(scenarios):
        pts = [p_start] + s["path"]; n = len(pts) - 1
        xs = [x_start + (x_end - x_start) * j / n for j in range(n + 1)]
        c = SCEN_COLORS[i % 2]; ls = "-" if i == 0 else "--"
        ax.plot(xs[:-1], pts[:-1], color=c, linewidth=2, linestyle=ls)
        ax.annotate("", xy=(xs[-1], pts[-1]), xytext=(xs[-2], pts[-2]),
                    arrowprops=dict(arrowstyle="-|>", color=c, lw=2, linestyle=ls, mutation_scale=16))
        handles.append(__import__("matplotlib.lines", fromlist=["Line2D"]).Line2D([], [], color=c, lw=2, linestyle=ls, label=s["label"]))
    return handles


def _legend(ax, handles, va=None):
    import matplotlib.patches as mp
    h = list(handles or [])
    if va:
        h.append(mp.Patch(color="#00bcd4", alpha=0.25, label="Prior-session value area"))
    if h:
        lg = ax.legend(handles=h, loc="best", fontsize=9, frameon=True, facecolor=BG, edgecolor=GRID, labelcolor=TXT)
        lg.set_zorder(5)


def _ylim(vals, pad=0.15):
    v = [x for x in vals if x is not None]
    lo, hi = min(v), max(v); span = max(hi - lo, 1.0)
    return lo - span * pad, hi + span * pad


def nice_date(day):
    d = dt.date.fromisoformat(day)
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def render(day, root=ROOT, data_dir="/workspace/spy_gamma_data", out_dir=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    root = Path(root); out_dir = Path(out_dir or root / "pod" / "gamma_charts"); out_dir.mkdir(parents=True, exist_ok=True)
    row = load_tracker_row(root, day)
    if not row:
        raise LookupError(f"no tracker row for {day}")
    intra = _read_table(Path(data_dir) / "yf_SPY_intraday.json")
    hist = _read_table(Path(data_dir) / "yf_SPY_history.json")
    bars, sess = prior_session_bars(intra, day)
    dly = daily_bars(hist, day)
    prior_close = float(dly.Close.iloc[-1]) if dly is not None and len(dly) else None
    gap = _num(row.get("premarket_gap_pct"))
    pre = prior_close * (1 + gap / 100) if prior_close and gap is not None else None
    levels = {"prior_close": prior_close, "premarket": pre}
    for k in ("flip_45d", "hvl_45d", "poc", "call_wall_45d", "put_wall_45d"):
        levels[k] = _num(row.get(k))
    scen, reason = load_card(root, day, prior_close)
    vah, val = _num(row.get("vah")), _num(row.get("val"))
    title_date = nice_date(day)
    foot = "Data only · Cboe delayed chain (45-day scope, model estimates) · Yahoo bars · paper trading"
    files = {}

    # (a) Outlook
    fig, ax = plt.subplots(figsize=(13, 7), dpi=110); _style(ax, fig)
    n = len(bars) if bars is not None else 0
    if n:
        _candles(ax, bars)
    proj0, proj1 = n + 2, n + 2 + max(int(n * 0.6), 30)
    ax.axvspan(proj0 - 1, proj1 + 1, color="#1a1e2b", zorder=0)
    ax.text((proj0 + proj1) / 2, 1.0, "Today", transform=ax.get_xaxis_transform(), color=MUT, ha="center", va="bottom", fontsize=9)
    vals = list(levels.values()) + ([bars.Low.min(), bars.High.max()] if n else []) + [p for s in scen for p in s["path"]]
    ylo, yhi = _ylim(vals)
    if vah and val:
        ax.axhspan(val, vah, xmin=0, xmax=1, color="#00bcd4", alpha=0.07)
    _levels(ax, levels, 0, proj1 + 1, ylo, yhi)
    _legend(ax, _arrows(ax, scen, proj0, proj1 - 6, pre or prior_close) if scen else [], bool(vah and val))
    ax.set_ylim(ylo, yhi); ax.set_xlim(-1, proj1 + 14)
    if n:
        ticks = [i for i, t in enumerate(bars.index) if t.minute == 30 and t.hour in (9, 11, 13, 15)]
        ax.set_xticks(ticks); ax.set_xticklabels([bars.index[i].astimezone(CT).strftime("%-I:%M") for i in ticks])
    ax.set_title(f"SPY {title_date} Outlook – Gamma Regime Rotation", color=TXT, fontsize=15, fontweight="bold", loc="left", pad=24)
    sub = f"Prior session {sess.strftime('%b %-d') if sess else 'unavailable'} (5-min, CT) · regime {row.get('regime') or 'unavailable'}"
    sub += " · scenarios from the CIO-approved pre-market card" if scen else " · no CIO-approved scenario card for today"
    fig.text(0.01, 0.015, foot, color=MUT, fontsize=8); ax.text(0, 1.01, sub, transform=ax.transAxes, color=MUT, fontsize=9.5, va="bottom")
    fig.tight_layout(rect=(0, 0.03, 1, 1)); p = out_dir / f"{day}_outlook.png"; fig.savefig(p, facecolor=BG); plt.close(fig); files["outlook"] = p

    # (b) Daily range sketch
    fig, ax = plt.subplots(figsize=(13, 7), dpi=110); _style(ax, fig)
    m = len(dly) if dly is not None else 0
    if m:
        _candles(ax, dly)
    today_x = m + 1
    vals = list(levels.values()) + ([dly.Low.min(), dly.High.max()] if m else []) + [p for s in scen for p in s["path"]]
    ylo, yhi = _ylim(vals, 0.08)
    if vah and val:
        ax.axhspan(val, vah, color="#00bcd4", alpha=0.10)
    ax.axvspan(today_x - 0.6, today_x + 4.6, color="#1a1e2b", zorder=0)
    ax.text(today_x + 2, 1.0, "Today", transform=ax.get_xaxis_transform(), color=MUT, ha="center", va="bottom", fontsize=9)
    _levels(ax, levels, -0.5, today_x + 4.6, ylo, yhi)
    _legend(ax, _arrows(ax, scen, today_x - 0.4, today_x + 3.8, pre or prior_close) if scen else [], bool(vah and val))
    ax.set_ylim(ylo, yhi); ax.set_xlim(-1, today_x + 9)
    if m:
        ticks = list(range(0, m, 4)); ax.set_xticks(ticks); ax.set_xticklabels([dly.index[i].strftime("%b %-d") for i in ticks])
    ax.set_title(f"SPY {title_date} Daily Range Sketch", color=TXT, fontsize=15, fontweight="bold", loc="left", pad=24)
    ax.text(0, 1.01, "Last 20 sessions · key levels" + (" · paths from the CIO-approved card" if scen else " · no CIO-approved paths for today"),
            transform=ax.transAxes, color=MUT, fontsize=9.5, va="bottom")
    fig.text(0.01, 0.015, foot, color=MUT, fontsize=8)
    fig.tight_layout(rect=(0, 0.03, 1, 1)); p = out_dir / f"{day}_range.png"; fig.savefig(p, facecolor=BG); plt.close(fig); files["range"] = p

    now_ct = dt.datetime.now(dt.timezone.utc).astimezone(CT)
    manifest = {"date": day, "generated_ct": now_ct.strftime("%Y-%m-%d %H:%M CT"),
                "titles": {"outlook": f"SPY {title_date} Outlook – Gamma Regime Rotation", "range": f"SPY {title_date} Daily Range Sketch"},
                "levels": {LEVEL_STYLE[k][0]: (round(v, 2) if v is not None else None) for k, v in levels.items()},
                "value_area": {"low": val, "high": vah}, "prior_session": sess.isoformat() if sess else None,
                "scenarios": scen, "scenario_status": reason, "card_found": bool(scen),
                "images": {k: v.name for k, v in files.items()}}
    (out_dir / "latest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    imgs = {"date": day, "titles": manifest["titles"]}
    for k, v in files.items():
        imgs[k] = "data:image/png;base64," + base64.b64encode(v.read_bytes()).decode("ascii")
    tmp = out_dir / ".latest_images.json.tmp"; tmp.write_text(json.dumps(imgs)); os.replace(tmp, out_dir / "latest_images.json")
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", default=dt.datetime.now(dt.timezone.utc).astimezone(CT).date().isoformat())
    ap.add_argument("--root", default=str(ROOT)); ap.add_argument("--data-dir", default="/workspace/spy_gamma_data")
    ap.add_argument("--out-dir", default=None)
    a = ap.parse_args(argv)
    try:
        m = render(a.date, a.root, a.data_dir, a.out_dir)
    except LookupError as e:
        print("charts skipped:", e); return 3
    print(json.dumps({"ok": True, "date": m["date"], "images": m["images"], "scenario_status": m["scenario_status"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

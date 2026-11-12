#!/usr/bin/env python3
"""Daily Gamma Tracker fields, tracker history upsert, vault bundling, and chart generation.
SYNTHETIC fixture data only (temp dirs); never touches the live repo state, network or git.
Run: /workspace/spy_gamma_venv/bin/python -m unittest test_gamma_tracker   (from scripts/)
"""
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

HERE = Path(__file__).resolve().parent
NY = ZoneInfo("America/New_York"); CT = ZoneInfo("America/Chicago")
CHARTS_PY = os.environ.get("GAMMA_CHARTS_PYTHON", "/workspace/gamma_charts_venv/bin/python")


def _load(name, path, env=None):
    old = {}
    for k, v in (env or {}).items():
        old[k] = os.environ.get(k); os.environ[k] = v
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


mon = _load("sgm_under_test", HERE / "spy_gamma_monitor.py")
sync = _load("sync_under_test", HERE / "sync_github_archive.py")


def bars(rows):
    """rows: [(ny_datetime, o, h, l, c, v)] -> Yahoo-like 1-minute frame."""
    idx = pd.DatetimeIndex([r[0] for r in rows], name="Datetime")
    return pd.DataFrame({"Open": [r[1] for r in rows], "High": [r[2] for r in rows], "Low": [r[3] for r in rows],
                         "Close": [r[4] for r in rows], "Volume": [r[5] for r in rows]}, index=idx)


def t(d, hh, mm):
    return dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY)


class TradingDays(unittest.TestCase):
    def test_nov12_is_34(self):
        self.assertEqual(mon.trading_days_left_ytd(dt.date(2026, 11, 12)), 34)

    def test_holidays_and_early_closes(self):
        self.assertFalse(mon.is_nyse_session(dt.date(2026, 11, 26)))
        self.assertFalse(mon.is_nyse_session(dt.date(2026, 12, 25)))
        self.assertTrue(mon.is_nyse_session(dt.date(2026, 11, 27)))  # early close still counts
        self.assertTrue(mon.is_nyse_session(dt.date(2026, 12, 24)))
        self.assertEqual(mon.trading_days_left_ytd(dt.date(2026, 12, 31)), 1)
        self.assertEqual(mon.trading_days_left_ytd(dt.date(2026, 12, 24)), 5)  # 24, 28, 29, 30, 31
        self.assertIsNone(mon.trading_days_left_ytd(dt.date(2031, 1, 2)))  # unknown calendar -> null


PROFILE = [
    {"strike": 740.0, "call_gex": 1e8, "put_gex": -6e8, "net_gex": -5e8},
    {"strike": 745.0, "call_gex": 2e8, "put_gex": -4e8, "net_gex": -2e8},
    {"strike": 750.0, "call_gex": 9e8, "put_gex": -1e8, "net_gex": 8e8},
    {"strike": 755.0, "call_gex": 3e8, "put_gex": -1e8, "net_gex": 2e8},
]


class GexFields(unittest.TestCase):
    def test_gamma_split(self):
        g = mon.gamma_split(PROFILE)
        self.assertAlmostEqual(g["call_gex_abs"], 1.5e9); self.assertAlmostEqual(g["put_gex_abs"], 1.2e9)
        self.assertAlmostEqual(g["call_share"], 1.5 / 2.7); self.assertEqual(g["dominant_side"], "call")
        self.assertIsNone(mon.gamma_split([]))

    def test_hvl_interpolated_zero_crossing(self):
        h = mon.hvl_model(PROFILE, spot=752)
        # cumulative: -5e8, -7e8, +1e8 -> crosses between 745 and 750 at 745 + 7/8*5
        self.assertAlmostEqual(h["level"], 745 + 7 / 8 * 5)
        self.assertEqual(h["crossings"][0]["direction"], "negative_to_positive")
        self.assertIn("not vendor HVL", h["label"])
        self.assertIsNone(mon.hvl_model([{"strike": 1, "net_gex": -1}, {"strike": 2, "net_gex": -1}])["level"])

    def test_pc_ratios_and_regime(self):
        p = mon.pc_ratios({"put_call_volume_ratio": 0.9, "put_call_oi_ratio": 1.7})
        self.assertEqual((p["put_call_volume"], p["put_call_oi"]), (0.9, 1.7)); self.assertIn("prior session", p["note"])
        self.assertEqual(mon.regime_from_gex(-5e8), "neutral"); self.assertEqual(mon.regime_from_gex(-1.2e9), "short")
        self.assertEqual(mon.regime_from_gex(2e9), "long"); self.assertIsNone(mon.regime_from_gex(None))


class VolumeProfile(unittest.TestCase):
    def test_poc_value_area_rth_only(self):
        d = dt.date(2026, 11, 11)
        rows = [(t(d, 9, 0), 1, 1, 1, 1, 10**9)]  # pre-market bar: must be excluded
        prices = {750.0: 100, 750.25: 400, 750.5: 1000, 750.75: 300, 751.0: 100, 752.0: 50}
        m = 30
        for p, v in prices.items():
            rows.append((t(d, 9 + m // 60, m % 60), p, p, p, p, v)); m += 1
        rows.append((t(d, 16, 0), 2, 2, 2, 2, 10**9))  # 16:00 ET bar: outside RTH
        vp = mon.volume_profile(bars(rows), d)
        self.assertEqual(vp["status"], "ok"); self.assertEqual(vp["poc"], 750.5)
        self.assertEqual(vp["total_volume"], sum(prices.values()))
        # 70% of 1950 = 1365: 1000 -> +400 (750.25) = 1400 >= 1365
        self.assertEqual((vp["val"], vp["vah"]), (750.25, 750.5))
        self.assertIn("not a tick profile", vp["label"])
        self.assertEqual(mon.volume_profile(None, d)["poc"], None)

    def test_typical_price_binning(self):
        d = dt.date(2026, 11, 11)
        vp = mon.volume_profile(bars([(t(d, 10, 0), 700, 701.0, 700.0, 700.4, 5)]), d)  # (701+700+700.4)/3 = 700.47
        self.assertEqual(vp["poc"], 700.5)


class PrevGexAndFutures(unittest.TestCase):
    def test_prev_net_gex_prior_post_close(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ts.jsonl"
            rows = [{"timestamp_ct": "2026-11-10T15:21:00-0600", "run_tag": "post-close", "net_gex_45d_dollars": -1, "net_gex_0dte_dollars": -2},
                    {"timestamp_ct": "2026-11-11T15:17:00-0600", "run_tag": "post-close", "net_gex_45d_dollars": -4e8, "net_gex_0dte_dollars": 7e8, "session_date": "2026-11-11"},
                    {"timestamp_ct": "2026-11-12T08:23:00-0600", "run_tag": "pre-open", "net_gex_45d_dollars": 9, "net_gex_0dte_dollars": 9},
                    {"timestamp_ct": "2026-11-12T15:17:00-0600", "run_tag": "post-close", "net_gex_45d_dollars": 5, "net_gex_0dte_dollars": 5}]
            p.write_text("".join(json.dumps(r) + "\n" for r in rows))
            r = mon.prev_net_gex(p, dt.date(2026, 11, 12))
            self.assertEqual((r["net_gex_45d"], r["net_gex_0dte"]), (-4e8, 7e8)); self.assertIn("not strictly comparable", r["note"])
            self.assertEqual(mon.prev_net_gex(Path(td) / "missing.jsonl", dt.date(2026, 11, 12))["status"], "unavailable_no_history")

    def _fut(self, low, p0820):
        d0, d1 = dt.date(2026, 11, 11), dt.date(2026, 11, 12)
        rows = [(t(d0, 15, 58), 100, 100, 100, 100, 1), (t(d0, 15, 59), 100, 100, 100, 100.0, 1),
                (t(d0, 16, 30), 101, 120, 90, 100, 1), (t(d0, 17, 59), 101, 121, 91, 100, 1),  # post-cash 15:30/16:59 CT: outside window
                (t(d1, 3, 0), max(low, 99), max(low, 99), max(low, 99), max(low, 99), 1), (t(d1, 6, 10), low, low, low, low, 1), (t(d1, 9, 19), p0820, p0820, p0820, p0820, 1),
                (t(d1, 9, 25), 1, 1, 1, 1, 1)]  # after 8:20 CT: ignored
        return mon.overnight_futures(bars(rows), d1)

    def test_overnight_futures_fields(self):
        o = self._fut(98.0, 99.5)
        self.assertEqual(o["ref"], 100.0); self.assertEqual(o["low"], 98.0); self.assertEqual(o["p0820"], 99.5)
        self.assertTrue(o["ref_time_ct"].startswith("2026-11-11T15:00")); self.assertTrue(o["p0820_time_ct"].startswith("2026-11-12T08:20"))
        self.assertTrue(o["low_time_ct"].startswith("2026-11-12T05:10"))
        self.assertAlmostEqual(o["gap_dollars"], -0.5); self.assertAlmostEqual(o["gap_pct"], -0.5)
        self.assertAlmostEqual(o["pct_retraced"], 0.75); self.assertEqual(o["classification"], "dipped_recovering")
        self.assertEqual(o["label"], "Yahoo delayed ~10 min")
        self.assertTrue(o["window_start_ct"].startswith("2026-11-12T02:00"))  # first bar at/after the 17:00 CT reopen
        self.assertEqual(o["high"], max(99.5, 99.0)); self.assertIn("5:00 PM CT reopen", o["window"])

    def test_window_starts_at_globex_reopen_weekend(self):
        fri, mon_ = dt.date(2026, 11, 6), dt.date(2026, 11, 9)
        rows = [(t(fri, 15, 59), 100, 100, 100, 100.0, 1), (t(fri, 16, 40), 100, 100, 95, 100, 1),  # Fri 15:40 CT post-cash
                (t(dt.date(2026, 11, 8), 18, 0), 100, 101, 99, 100, 1), (t(mon_, 9, 19), 99.6, 99.6, 99.6, 99.6, 1)]
        o = mon.overnight_futures(bars(rows), mon_)
        self.assertEqual(o["low"], 99); self.assertEqual(o["high"], 101); self.assertTrue(o["window_start_ct"].startswith("2026-11-08T17:00"))
        self.assertAlmostEqual(o["pct_retraced"], 0.6); self.assertEqual(o["classification"], "dipped_recovering")

    def test_classification_rules(self):
        self.assertEqual(self._fut(98.0, 98.5)["classification"], "dipped_not_recovering")
        self.assertEqual(self._fut(99.8, 99.81)["classification"], "flat_or_green")  # low within 0.25%
        self.assertEqual(self._fut(97.0, 100.2)["classification"], "flat_or_green")  # p0820 >= ref
        self.assertEqual(mon.classify_futures(100, 98, 99.0), "dipped_recovering")  # exactly 50%
        self.assertIsNone(mon.overnight_futures(None, dt.date(2026, 11, 12))["classification"])


def fixture_options(spot=752.0):
    prof = [dict(z, call_oi=1, put_oi=1, call_volume=1, put_volume=1) for z in PROFILE]
    tot = {"net_gex": -1.5e9, "put_call_volume_ratio": 0.8, "put_call_oi_ratio": 1.9}
    sc = lambda: {"strike_profile": [dict(z) for z in prof], "totals": dict(tot),
                  "call_walls_by_gex": [{"strike": 760.0}], "put_walls_by_gex": [{"strike": 745.0}],
                  "zero_gamma_repriced_model": {"zero_gamma_level": 749.1}}
    return {"status": "ok", "gex_spot": spot, "scopes": {k: sc() for k in ("upcoming_45d", "upcoming_all", "next_session", "monthly_opex")}}


class TrackerRow(unittest.TestCase):
    def test_build_row_columns_and_values(self):
        now = dt.datetime(2026, 11, 12, 14, 20, tzinfo=dt.timezone.utc)  # 8:20 CT
        spy = {"close": 759.81, "cboe_quote": {"data": {"current_price": 755.0}}}
        with tempfile.TemporaryDirectory() as td:
            g = mon.build_gamma_tracker(fixture_options(), spy, {"^VIX": {"close": 18.6}}, {}, now, Path(td) / "none.jsonl", dt.date(2026, 11, 11))
        r = g["tracker_row"]
        self.assertEqual(list(r), mon.TRACKER_COLUMNS); self.assertEqual(r["date"], "2026-11-12")
        self.assertEqual(g["trading_days_left_ytd"], 34)
        self.assertAlmostEqual(r["premarket_gap_pct"], (755 / 759.81 - 1) * 100)
        self.assertEqual((r["call_wall_45d"], r["put_wall_45d"], r["flip_45d"], r["vix"]), (760.0, 745.0, 749.1, 18.6))
        self.assertAlmostEqual(r["hvl_45d"], 745 + 7 / 8 * 5); self.assertEqual(r["regime"], "short")
        self.assertEqual((r["pc_vol"], r["pc_oi"]), (0.8, 1.9))
        for k in ("close", "notes", "poc", "vah", "val", "prev_net_gex_45d", "futures_bias_class"):
            self.assertIsNone(r[k], k)  # no data in fixture -> null, never invented

    def test_options_unavailable_gives_nulls(self):
        now = dt.datetime(2026, 11, 12, 14, 20, tzinfo=dt.timezone.utc)
        g = mon.build_gamma_tracker({"status": "stale_or_not_asof"}, {}, {}, {}, now, None, dt.date(2026, 11, 11))
        r = g["tracker_row"]
        self.assertTrue(all(r[k] is None for k in mon.TRACKER_COLUMNS if k not in ("date",)))

    def test_markdown_section(self):
        now = dt.datetime(2026, 11, 12, 14, 20, tzinfo=dt.timezone.utc)
        g = mon.build_gamma_tracker(fixture_options(), {}, {}, {}, now, None, dt.date(2026, 11, 11))
        txt = "\n".join(mon.tracker_md_lines(g, lambda v, n=4: "NA" if v is None else f"{v:,.{n}f}"))
        self.assertIn("Trading days left in 2026 (including today): 34", txt); self.assertIn("not vendor HVL", txt)


class TrackerHistory(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); self.hist = os.path.join(self.td.name, "history")
        self._repo = sync.REPO_DIR; sync.REPO_DIR = self.td.name  # temp files go next to the target, never the live repo

    def tearDown(self):
        sync.REPO_DIR = self._repo; self.td.cleanup()

    def snap(self, **row):
        base = {c: None for c in sync.TRACKER_COLUMNS}; base.update(date="2026-11-12", net_gex_45d=-1.23456e9, poc=759.251, pc_vol=0.87739)
        base.update(row)
        return {"gamma_tracker": {"tracker_date": "2026-11-12", "is_nyse_session": True, "tracker_row": base},
                "spy": {"session": "2026-11-12", "status": "ok", "close": 761.234}}

    def test_preopen_then_postclose_upsert(self):
        ts = [{"timestamp_ct": "2026-11-12T08:23:00-0600", "timestamp_utc": "a", "net_gex_0dte_dollars": -5e8, "net_gex_45d_dollars": -1e9},
              {"timestamp_ct": "2026-11-12T10:21:00-0600", "timestamp_utc": "b", "net_gex_0dte_dollars": 2e8, "net_gex_45d_dollars": -1e9},
              {"timestamp_ct": "2026-11-11T15:17:00-0600", "timestamp_utc": "0", "net_gex_0dte_dollars": 9e8}]
        r = sync.upsert_daily_tracker(self.snap(), "pre-open", ts, self.hist, "2026-11-12T08:23")
        self.assertIsNone(r["close"]); self.assertEqual(r["net_gex_45d"], -1234560000); self.assertEqual(r["poc"], 759.25); self.assertEqual(r["pc_vol"], 0.877)
        self.assertIsNone(sync.upsert_daily_tracker(self.snap(poc=1), "rth-hourly", ts, self.hist, "x"))  # RTH never rewrites
        r = sync.upsert_daily_tracker(self.snap(poc=1.0), "post-close", ts, self.hist, "2026-11-12T15:17")
        self.assertEqual(r["close"], 761.23); self.assertEqual(r["poc"], 759.25)  # pre-open fields kept
        self.assertEqual(r["notes"], "0DTE flipped long 10:21 CT")
        rows = sync.load_daily_tracker(self.hist); self.assertEqual(list(rows), ["2026-11-12"])
        csv_lines = Path(self.hist, "daily_tracker.csv").read_text().splitlines()
        self.assertEqual(csv_lines[0].split(","), sync.TRACKER_COLUMNS); self.assertEqual(len(csv_lines), 2)
        self.assertFalse([p for p in os.listdir(self.td.name) if p.startswith(".tmp_")])  # no temp leftovers

    def test_preopen_confirms_prior_close(self):
        s1 = self.snap(date="2026-11-11"); s1["gamma_tracker"]["tracker_date"] = "2026-11-11"
        s1["spy"] = {"session": "2026-11-11", "status": "ok", "close": 759.84}
        sync.upsert_daily_tracker(s1, "post-close", [], self.hist, "x")
        s2 = self.snap(); s2["spy"] = {"session": "2026-11-11", "status": "ok", "close": 759.81}
        sync.upsert_daily_tracker(s2, "pre-open", [], self.hist, "y")
        rows = sync.load_daily_tracker(self.hist)
        self.assertEqual(rows["2026-11-11"]["close"], 759.81); self.assertIsNone(rows["2026-11-12"]["close"])

    def test_upsert_by_date_and_holiday_skip(self):
        sync.upsert_daily_tracker(self.snap(), "pre-open", [], self.hist, "x")
        s2 = self.snap(date="2026-11-13"); s2["gamma_tracker"]["tracker_date"] = "2026-11-13"
        sync.upsert_daily_tracker(s2, "pre-open", [], self.hist, "x"); sync.upsert_daily_tracker(s2, "pre-open", [], self.hist, "x")
        self.assertEqual(list(sync.load_daily_tracker(self.hist)), ["2026-11-12", "2026-11-13"])
        h = self.snap(date="2026-11-26"); h["gamma_tracker"].update(tracker_date="2026-11-26", is_nyse_session=False)
        self.assertIsNone(sync.upsert_daily_tracker(h, "pre-open", [], self.hist, "x"))

    def test_notes_data_only(self):
        self.assertIsNone(sync.tracker_day_notes([], "2026-11-12"))
        rows = [{"timestamp_ct": "2026-11-12T08:23:00-0600", "net_gex_0dte_dollars": 1, "net_gex_45d_dollars": 1}]
        self.assertEqual(sync.tracker_day_notes(rows, "2026-11-12"), "no net GEX sign change across archived runs")

    def test_chart_hook_fail_safe_and_card_refresh(self):
        calls = []
        orig = sync.subprocess.run
        sync.subprocess.run = lambda *a, **k: calls.append(a[0]) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        try:
            os.makedirs(os.path.join(self.td.name, "scripts")); Path(self.td.name, "scripts", "gamma_charts.py").write_text("")
            self.assertIsNone(sync.run_morning_charts("2026-11-12", "post-close")); self.assertEqual(calls, [])
            sync.run_morning_charts("2026-11-12", "rth-hourly"); self.assertEqual(len(calls), 1)  # missing -> draw
            out = Path(self.td.name, "pod", "gamma_charts"); out.mkdir(parents=True)
            for k in ("outlook", "range"):
                (out / f"2026-11-12_{k}.png").write_bytes(b"x"); os.utime(out / f"2026-11-12_{k}.png", (1000, 1000))
            sync.run_morning_charts("2026-11-12", "rth-hourly"); self.assertEqual(len(calls), 1)  # present -> skip
            cards = Path(self.td.name, "pod", "premarket_cards"); cards.mkdir(parents=True); (cards / "2026-11-12.json").write_text("{}")
            sync.run_morning_charts("2026-11-12", "rth-hourly"); self.assertEqual(len(calls), 2)  # newer card -> redraw
            sync.run_morning_charts("2026-11-12", "pre-open"); self.assertEqual(len(calls), 3)
            sync.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
            self.assertIsNone(sync.run_morning_charts("2026-11-12", "pre-open"))  # never raises
        finally:
            sync.subprocess.run = orig

    def test_tracker_never_in_publish_allowlist(self):
        for p in ("history/daily_tracker.jsonl", "history/daily_tracker.csv", "pod/gamma_charts/latest_images.json"):
            self.assertFalse(sync.in_publish_allowlist(p), p)


def write_chart_fixture(root, data, day="2026-11-12"):
    (root / "history").mkdir(parents=True, exist_ok=True); data.mkdir(parents=True, exist_ok=True)
    row = {c: None for c in sync.TRACKER_COLUMNS}
    row.update(date=day, premarket_gap_pct=-0.6, net_gex_45d=-1.5e9, flip_45d=761.3, hvl_45d=764.6, call_wall_45d=770.0,
               put_wall_45d=750.0, poc=759.25, vah=761.5, val=758.75, regime="short")
    (root / "history" / "daily_tracker.jsonl").write_text(json.dumps(row) + "\n")
    d0 = dt.date(2026, 11, 11); rows = []; p = 756.0
    for i in range(390):
        ts = t(d0, 9, 30) + dt.timedelta(minutes=i); q = p + 0.01 * (i % 50) - 0.2
        rows.append((ts, q, q + 0.1, q - 0.1, q + 0.03, 1000 + i))
    bars(rows).to_json(data / "yf_SPY_intraday.json", orient="table", date_format="iso")
    didx = pd.DatetimeIndex([dt.datetime(2026, 10, 1, tzinfo=NY) + dt.timedelta(days=i) for i in range(42)], name="Date")
    pd.DataFrame({"Open": [750.0 + i % 7 for i in range(42)], "High": [756.0 + i % 7 for i in range(42)], "Low": [745.0 + i % 7 for i in range(42)],
                  "Close": [752.0 + i % 5 for i in range(42)], "Volume": [5e7] * 42}, index=didx).to_json(data / "yf_SPY_history.json", orient="table", date_format="iso")


@unittest.skipUnless(os.path.exists(CHARTS_PY), "charts venv missing")
class Charts(unittest.TestCase):
    def run_charts(self, root, data, day="2026-11-12"):
        return subprocess.run([CHARTS_PY, str(HERE / "gamma_charts.py"), "--date", day, "--root", str(root), "--data-dir", str(data)],
                              capture_output=True, text=True, timeout=180)

    def test_render_without_card_draws_no_scenarios(self):
        with tempfile.TemporaryDirectory() as td:
            root, data = Path(td) / "r", Path(td) / "d"; write_chart_fixture(root, data)
            r = self.run_charts(root, data); self.assertEqual(r.returncode, 0, r.stderr)
            out = root / "pod" / "gamma_charts"
            for k in ("outlook", "range"):
                self.assertEqual((out / f"2026-11-12_{k}.png").read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            m = json.loads((out / "latest.json").read_text())
            self.assertFalse(m["card_found"]); self.assertEqual(m["scenarios"], [])
            self.assertEqual(m["titles"]["outlook"], "SPY Nov 12, 2026 Outlook – Gamma Regime Rotation")
            self.assertEqual(m["levels"]["Call wall"], 770.0); self.assertEqual(m["levels"]["Prior close"], 753.0)  # last daily close before the date
            imgs = json.loads((out / "latest_images.json").read_text())
            self.assertTrue(imgs["outlook"].startswith("data:image/png;base64,")); self.assertTrue(imgs["range"].startswith("data:image/png;base64,"))

    def test_card_rules(self):
        with tempfile.TemporaryDirectory() as td:
            root, data = Path(td) / "r", Path(td) / "d"; write_chart_fixture(root, data)
            cards = root / "pod" / "premarket_cards"; cards.mkdir(parents=True)
            good = {"date": "2026-11-12", "approved": True, "approved_by": "CIO & Risk Gatekeeper",
                    "scenarios": [{"label": "Probe and recover", "path": [754, 758]}, {"label": "Alternative", "path": [750]}]}
            cases = [(good, 2), (dict(good, approved=False), 0), (dict(good, date="2026-11-11"), 0),
                     (dict(good, scenarios=[{"label": "far", "path": [900]}]), 0), (dict(good, approved_by="someone"), 0)]
            for card, n in cases:
                (cards / "2026-11-12.json").write_text(json.dumps(card))
                r = self.run_charts(root, data); self.assertEqual(r.returncode, 0, r.stderr)
                m = json.loads((root / "pod" / "gamma_charts" / "latest.json").read_text())
                self.assertEqual(len(m["scenarios"]), n, card)

    def test_no_tracker_row_no_charts(self):
        with tempfile.TemporaryDirectory() as td:
            root, data = Path(td) / "r", Path(td) / "d"; write_chart_fixture(root, data)
            r = self.run_charts(root, data, day="2026-11-13")
            self.assertEqual(r.returncode, 3); self.assertFalse((root / "pod" / "gamma_charts" / "2026-11-13_outlook.png").exists())


class IntradayFeed(unittest.TestCase):
    def five(self, days, today=None, today_slots=0, spike=None):
        rows = []
        for d in days:
            for i in range(78):
                ts = t(d, 9, 30) + dt.timedelta(minutes=5 * i)
                v = 1000.0 if spike is None or (d, i) != spike else 1e6
                rows.append((ts, 10, 11, 9, 10.5, v))
        if today:
            for i in range(today_slots):
                rows.append((t(today, 9, 30) + dt.timedelta(minutes=5 * i), 10, 11, 9, 10.5, 2000.0))
        return bars(rows)

    def test_tod_rvol_completed_bars_and_spike_cap(self):
        prior = [dt.date(2026, 10, 1) + dt.timedelta(days=i) for i in range(25)]
        today = dt.date(2026, 11, 12)
        df = self.five(prior, today, today_slots=4, spike=(prior[-1], 0))
        now = t(today, 9, 47)  # bars 9:30/9:35/9:40 complete; 9:45 still forming
        f = mon.spy_5m_feed(df, now)
        self.assertEqual(f["status"], "ok"); self.assertEqual(len(f["sessions_used"]), 20)
        self.assertEqual(len(f["today_bars"]), 3); self.assertTrue(f["last_completed_bar_ct"].startswith("2026-11-12T08:40"))
        self.assertAlmostEqual(f["tod_rvol"], 6000 / ((19 * 3000 + 8000 + 2000) / 20))  # 1e6 spike capped at 8x slot median
        self.assertAlmostEqual(f["slot_curves"][0]["avg_volume"], (19 * 1000 + 8000) / 20)
        self.assertAlmostEqual(f["bar_rvol"], 2.0)

    def test_preopen_has_curves_but_no_tod_rvol(self):
        prior = [dt.date(2026, 10, 1) + dt.timedelta(days=i) for i in range(5)]
        f = mon.spy_5m_feed(self.five(prior), t(dt.date(2026, 11, 12), 9, 20))
        self.assertEqual(f["status"], "ok_no_completed_bars_today"); self.assertIsNone(f["tod_rvol"]); self.assertEqual(len(f["slot_curves"]), 78)
        self.assertEqual(mon.spy_5m_feed(None, t(dt.date(2026, 11, 12), 9, 20))["status"], "unavailable_no_5m_bars")

    def test_gex_by_spot_grid(self):
        now = dt.datetime(2026, 11, 12, 15, 0, tzinfo=dt.timezone.utc)
        rows = [{"strike": 750.0, "type": "C", "oi": 1000.0, "iv": 0.2, "expiry_at": "2026-11-20T16:15:00-05:00"},
                {"strike": 740.0, "type": "P", "oi": 1000.0, "iv": 0.2, "expiry_at": "2026-11-20T16:15:00-05:00"}]
        g = mon.gex_by_spot(rows, 750.0, now, 0.04, 0.012, center=750.0, live_spot=752.0)
        self.assertEqual(g["grid"][0]["spot"], 735.0); self.assertEqual(g["grid"][-1]["spot"], 765.0); self.assertEqual(len(g["grid"]), 31)
        self.assertGreater(g["net_gex_at_chain_spot"], 0)  # call strike nearer spot dominates
        lowest = g["grid"][0]["net_gex"]; self.assertLess(lowest, g["net_gex_at_chain_spot"])
        self.assertIn("model estimate", g["label"])
        self.assertEqual(mon.gex_by_spot([], 750.0, now, 0.04, 0.0)["status"], "unavailable_no_iv")

    def test_es_overnight_in_spy_terms(self):
        es = {"ref": 7600.0, "ref_time_ct": "2026-11-11T15:00:00-06:00", "high": 7650.0, "low": 7524.0, "p0820": 7562.0}
        hist = pd.DataFrame({"Close": [700.0, 760.0]}, index=pd.DatetimeIndex([dt.datetime(2026, 11, 10, tzinfo=NY), dt.datetime(2026, 11, 11, tzinfo=NY)]))
        o = mon.es_overnight_spy_terms(es, hist)
        self.assertAlmostEqual(o["ratio_spy_per_es"], 0.1); self.assertAlmostEqual(o["spy_equiv_low"], 752.4)
        self.assertAlmostEqual(o["spy_equiv_high"], 765.0); self.assertIn("estimate", o["label"])
        self.assertEqual(mon.es_overnight_spy_terms({"ref": None}, hist)["status"], "unavailable")


class VaultBundling(unittest.TestCase):
    def test_tracker_and_charts_reach_vault_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "history").mkdir(); (root / "history" / "daily_tracker.jsonl").write_text('{"date":"2026-11-12"}\n')
            (root / "pod" / "gamma_charts").mkdir(parents=True)
            (root / "pod" / "gamma_charts" / "latest_images.json").write_text('{"outlook":"data:image/png;base64,AAAA"}')
            (root / "pod" / "gamma_charts" / "2026-11-12_outlook.png").write_bytes(b"\x89PNG")
            vc = _load("vc_under_test", HERE / "vault_crypto.py", {"HORSEY_ROOT": str(root)})
            files = vc.collect_state_files()
            self.assertIn("history/daily_tracker.jsonl", files)
            self.assertIn("pod/gamma_charts/latest_images.json", files)
            self.assertNotIn("pod/gamma_charts/2026-11-12_outlook.png", files)  # raw PNGs stay local


if __name__ == "__main__":
    unittest.main()

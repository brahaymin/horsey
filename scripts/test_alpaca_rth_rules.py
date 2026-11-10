#!/usr/bin/env python3
"""Unit tests for the per-ticket rule engine in _alpaca_rth.py.

All bars are SYNTHETIC: generated in-test with no market data, no network, no secrets.
Ticket dicts are synthetic fixtures that use the same blotter field schema.
Run:  python3 scripts/test_alpaca_rth_rules.py   (or set RTH_MODULE=/path/to/_alpaca_rth.py)
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

_TMP_ROOT = tempfile.mkdtemp(prefix="rth_test_root_")
os.environ["HORSEY_ROOT"] = _TMP_ROOT  # isolate: module never sees the real pod/
MOD_PATH = os.environ.get("RTH_MODULE") or str(Path(__file__).resolve().parent / "_alpaca_rth.py")
_spec = importlib.util.spec_from_file_location("_alpaca_rth_under_test", MOD_PATH)
rth = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rth)
CT = rth.CT
DAY = (2030, 1, 8)  # synthetic session date


def bar(h, m, o, hi, lo, c, v=1000.0):
    dt = datetime(*DAY, h, m, tzinfo=CT)
    return {"t": dt.isoformat(), "o": o, "h": hi, "l": lo, "c": c, "v": v, "_dt": dt}


def or_bars(lo, hi):
    """Six synthetic 08:30–08:55 bars spanning [lo, hi], volume 1000 each."""
    mid = (lo + hi) / 2
    out = []
    for i in range(6):
        out.append(bar(8, 30 + 5 * i, mid, hi if i == 1 else mid + 0.2, lo if i == 3 else mid - 0.2, mid))
    return out


def at(h, m):
    return datetime(*DAY, h, m, 30, tzinfo=CT)


def run(ticket, bars, now):
    or_info = rth.compute_or(bars, now)
    return rth.decide_armed_ticket(ticket, or_info, bars, now), or_info


# Synthetic fixtures (same field schema as the blotter)
def T_BREAKOUT():
    return {
        "ticket_id": "TKT-TEST-LONG", "status": "conditional_preapproved", "instrument": "stock",
        "direction": "long", "entry": 755.5, "stop": 754.0, "target1": 761.25, "target2": 762.09,
        "underlying_trigger": {"breakout": "acceptance above Monday high 755.42 (5-min hold) with RVOL >= 1.2x",
                               "monday_high": 755.42, "rvol_min": 1.2},
        "hard_invalidation_underlying": "5-15 minute reclaim/hold back below 755.00 / 752.70",
        "time_stop": "45m if <0.5R progress; no new risk after 14:30 CT; flat 0DTE by 14:45 CT",
        "no_entry_before_ct": "09:00",
    }


def T_BREAKDOWN():
    return {
        "ticket_id": "TKT-TEST-SHORT", "status": "conditional_preapproved", "instrument": "stock",
        "direction": "short", "entry": 749.5, "stop": 751.0, "target1": 745.0, "target2": 741.0,
        "underlying_trigger": {"failure": "decisive acceptance below 750 (5-min hold) with expanding volume / RVOL >= 1.2x",
                               "rvol_min": 1.2},
        "hard_invalidation_underlying": "sustained reclaim above 752.70 / 755.00",
        "time_stop": "45m if <0.5R progress; no new risk after 14:30 CT; flat 0DTE by 14:45 CT",
        "cio_premarket_gate": ("Enter only on a failed reclaim: price rallies back to 749.50-750.00 and rejects with "
                               "expanding volume; limit 749.50, skip if a fill would be below 749.00. No entry before 09:00 CT."),
        "no_entry_before_ct": "09:00",
    }


def T_FADE():
    return {
        "ticket_id": "TKT-TEST-FADE", "status": "conditional_preapproved", "instrument": "stock",
        "direction": "short", "entry": 754.5, "stop": 755.5, "target1": 751.5, "target2": 750.0,
        "underlying_trigger": {
            "primary": "rejection at 755 call/put wall: wick/test into 755.00-755.50 that FAILS (close back below 754.50) with RVOL >= 1.2x — short fade",
            "alternate": "rejection bounce at 750 put wall: wick/test into 749.50-750.50 that HOLDS (close back above 750.50) with RVOL >= 1.2x — long fade",
            "rvol_min": 1.2, "pin_zone": [750.0, 755.0], "confirmation_required": True},
        "hard_invalidation_underlying": ("Acceptance through the faded wall on rising volume (for 755 fade: 5-15m hold above 755.50; "
                                         "for 750 fade: 5-15m hold below 749.00)"),
        "time_stop": "30-45m if no follow-through after wall rejection; no new risk after 14:30 CT; flat 0DTE by 14:45 CT",
        "cio_premarket_gate": "D is dormant unless SPY first trades back into 754-755 and rejects. No entry before 09:00 CT.",
        "no_entry_before_ct": "09:00",
    }


def T_LEGACY():
    return {
        "ticket_id": "TKT-TEST-LEGACY", "status": "conditional_preapproved", "instrument": "option_put",
        "direction": "long", "entry": 1.85, "stop": 1.25, "target1": 3.65, "target2": 4.85,
        "underlying_trigger": {
            "or_break": "break + 5-min hold below 30m OR_low AND below prior RTH low",
            "or_rejection": "rally into 735.00–740.00 put-wall / OR_high that FAILS with RVOL > 1.5x",
            "prior_rth_low": 732.71, "put_wall_zone": [735.0, 740.0]},
        "hard_invalidation_underlying": "5-min close above OR_high, OR sustained reclaim/hold above 740.00 (whichever closer)",
        "time_stop": "45-60m if < -0.5R progress; no new risk after 14:30 CT; flat 0DTE by 14:45 CT",
    }


class LongBreakout(unittest.TestCase):
    def test_parse(self):
        r = rth.parse_ticket_rules(T_BREAKOUT())
        self.assertTrue(r["ok"], r["warnings"])
        self.assertEqual((r["mode"], r["side"], r["level"], r["hold_bars"], r["rvol_min"]), ("acceptance", "above", 755.42, 1, 1.2))
        self.assertEqual(r["inv"]["side"], "below"); self.assertEqual(r["inv"]["level"], 755.0)
        self.assertEqual(r["entry_start"], (9, 0)); self.assertEqual(r["entry_end"], (14, 30)); self.assertEqual(r["eod_flat"], (14, 45))
        self.assertEqual(r["time_stop"]["minutes"], 45); self.assertEqual(r["time_stop"]["min_progress_r"], 0.5)

    def test_fire_and_wait(self):
        base = or_bars(752.0, 754.0)
        d, _ = run(T_BREAKOUT(), base + [bar(9, 0, 754, 755.3, 753.9, 755.2, 1500)], at(9, 5))
        self.assertEqual(d["action"], "wait")
        d, _ = run(T_BREAKOUT(), base + [bar(9, 0, 755, 755.9, 754.9, 755.60, 1500)], at(9, 5))
        self.assertEqual(d["action"], "fire", d["reason"])
        d, _ = run(T_BREAKOUT(), base + [bar(9, 0, 755, 755.9, 754.9, 755.60, 1000)], at(9, 5))  # RVOL 1.0
        self.assertEqual(d["action"], "wait")
        d, _ = run(T_BREAKOUT(), base + [bar(8, 55, 755, 755.9, 754.9, 755.60, 1500)], at(8, 58))  # before 09:00
        self.assertNotEqual(d["action"], "fire")

    def test_in_trade_invalidation(self):
        r = rth.parse_ticket_rules(T_BREAKOUT())
        bars = or_bars(752.0, 754.0) + [bar(9, 0, 755.5, 755.8, 754.8, 754.90)]
        hit, why, lvl = rth.in_trade_invalidation(r, rth.compute_or(bars, at(9, 5)), bars)
        self.assertTrue(hit); self.assertEqual(lvl, 755.0)


class ShortBreakdown(unittest.TestCase):
    def test_parse(self):
        r = rth.parse_ticket_rules(T_BREAKDOWN())
        self.assertTrue(r["ok"], r["warnings"])
        self.assertEqual((r["mode"], r["side"], r["level"]), ("acceptance", "below", 750.0))
        self.assertEqual(r["retest"], [749.5, 750.0])
        self.assertEqual(r["fill_guard"], {"side": "below", "level": 749.0})
        self.assertEqual((r["inv"]["side"], r["inv"]["level"], r["inv"]["bars"]), ("above", 752.7, 2))

    def test_fire_guard_retest(self):
        base = or_bars(750.5, 753.0)
        d, _ = run(T_BREAKDOWN(), base + [bar(9, 10, 749.6, 749.9, 749.1, 749.30, 1500)], at(9, 15))
        self.assertEqual(d["action"], "fire", d["reason"])
        d, _ = run(T_BREAKDOWN(), base + [bar(9, 10, 749.6, 749.9, 748.6, 748.80, 1500)], at(9, 15))  # below 749.00 guard
        self.assertEqual(d["action"], "wait"); self.assertIn("fill guard", d["reason"])
        d, _ = run(T_BREAKDOWN(), base + [bar(9, 10, 749.3, 749.4, 749.1, 749.20, 1500)], at(9, 15))  # no retest
        self.assertEqual(d["action"], "wait"); self.assertIn("retest", d["reason"])

    def test_in_trade_invalidation_needs_two_closes(self):
        r = rth.parse_ticket_rules(T_BREAKDOWN())
        bars = or_bars(750.5, 753.0) + [bar(9, 10, 752, 753, 752, 752.80)]
        oi = rth.compute_or(bars, at(9, 15))
        self.assertFalse(rth.in_trade_invalidation(r, oi, bars)[0])
        bars.append(bar(9, 15, 752.8, 753, 752.7, 752.90))
        self.assertTrue(rth.in_trade_invalidation(r, oi, bars)[0])


class WallFade(unittest.TestCase):
    def test_parse(self):
        r = rth.parse_ticket_rules(T_FADE())
        self.assertTrue(r["ok"], r["warnings"])
        self.assertEqual((r["mode"], r["zone"], r["confirm_side"], r["confirm_level"]), ("wall_fade", [755.0, 755.5], "below", 754.5))
        self.assertEqual(r["retest"], [754.0, 755.0])
        self.assertEqual((r["inv"]["side"], r["inv"]["level"]), ("above", 755.5))
        self.assertEqual(r["time_stop"]["minutes"], 45)
        self.assertTrue(any("alternate" in w for w in r["warnings"]))

    def test_fire_and_wait(self):
        base = or_bars(751.0, 754.0)
        d, _ = run(T_FADE(), base + [bar(9, 20, 754.6, 755.20, 754.2, 754.30, 1300)], at(9, 25))
        self.assertEqual(d["action"], "fire", d["reason"])
        d, _ = run(T_FADE(), base + [bar(9, 20, 754.6, 755.20, 754.6, 754.80, 1300)], at(9, 25))  # no close back below
        self.assertEqual(d["action"], "wait")
        d, _ = run(T_FADE(), base + [bar(9, 20, 754.0, 754.90, 753.9, 754.10, 1300)], at(9, 25))  # never tested wall
        self.assertEqual(d["action"], "wait")


class LegacyFriday740(unittest.TestCase):
    def test_two_closes_above_740_invalidate(self):
        bars = or_bars(733.0, 739.0) + [bar(9, 0, 739, 741.3, 738.9, 741.05), bar(9, 5, 741, 741.5, 740.8, 741.25)]
        d, _ = run(T_LEGACY(), bars, at(9, 10))
        self.assertEqual(d["action"], "invalidate")
        self.assertEqual(d["reason"], "two consecutive 5m closes above 740 (741.05, 741.25)")

    def test_one_close_above_740_not_invalidated(self):
        bars = or_bars(733.0, 739.0) + [bar(9, 0, 739, 740.5, 738.9, 739.50), bar(9, 5, 739.5, 741.5, 739.4, 741.25)]
        d, _ = run(T_LEGACY(), bars, at(9, 10))
        self.assertNotEqual(d["action"], "invalidate")

    def test_or_breakdown_fires(self):
        bars = or_bars(733.0, 739.0) + [bar(9, 0, 733.5, 733.6, 732.4, 732.60), bar(9, 5, 732.6, 732.7, 732.0, 732.10)]
        d, _ = run(T_LEGACY(), bars, at(9, 10))
        self.assertEqual(d["action"], "fire"); self.assertEqual(d["meta"]["mode"], "or_break")

    def test_put_wall_rejection_fires(self):
        bars = or_bars(733.0, 739.0) + [bar(9, 0, 737.0, 739.8, 735.4, 735.60, 1600)]
        d, _ = run(T_LEGACY(), bars, at(9, 5))
        self.assertEqual(d["action"], "fire"); self.assertEqual(d["meta"]["mode"], "or_rejection")

    def test_in_trade_inv_min_or_high_740(self):
        r = rth.parse_ticket_rules(T_LEGACY())
        bars = or_bars(733.0, 739.0) + [bar(9, 0, 738, 739.5, 738, 739.20)]
        hit, _, lvl = rth.in_trade_invalidation(r, rth.compute_or(bars, at(9, 5)), bars)
        self.assertTrue(hit); self.assertEqual(lvl, 739.0)
        self.assertEqual(r["time_stop"], {"legacy": True})


class Unparseable(unittest.TestCase):
    def ticket(self):
        return {"ticket_id": "TKT-TEST-JUNK", "status": "conditional_preapproved", "instrument": "stock",
                "direction": "short", "entry": 700.0, "stop": 701.0, "target1": 698.0,
                "underlying_trigger": {"primary": "use judgement around the wall"},
                "hard_invalidation_underlying": "whenever it feels wrong"}

    def test_pending_never_invalidated_or_fired(self):
        bars = or_bars(699.0, 701.0) + [bar(9, 0, 760, 761, 759, 760.0, 5000), bar(9, 5, 760, 761, 759, 760.5, 5000)]
        d, _ = run(self.ticket(), bars, at(9, 10))
        self.assertEqual(d["action"], "unparsed"); self.assertIn("unparseable", d["reason"])
        r = rth.parse_ticket_rules(self.ticket())
        self.assertEqual(rth.pre_trigger_invalidated(rth.compute_or(bars, at(9, 10)), bars, at(9, 10), r), (False, ""))
        fired, _, _ = rth.evaluate_entry(self.ticket(), rth.compute_or(bars, at(9, 10)), bars, at(9, 10))
        self.assertFalse(fired)

    def test_missing_trigger_and_bad_direction(self):
        t = self.ticket(); t["underlying_trigger"] = {}
        self.assertFalse(rth.parse_ticket_rules(t)["ok"])
        t = T_BREAKOUT(); t["direction"] = "short"  # acceptance above vs short: conflict
        self.assertFalse(rth.parse_ticket_rules(t)["ok"])


class Regression751(unittest.TestCase):
    """Closes 751.71 / 751.83 (Nov 10 incident pattern) must not invalidate tickets whose own rules weren't hit."""
    def bars(self):
        return or_bars(749.8, 752.5) + [bar(9, 0, 751.5, 751.9, 751.3, 751.71, 1200), bar(9, 5, 751.7, 752.0, 751.6, 751.83, 1200)]

    def test_no_invalidation(self):
        for mk in (T_BREAKOUT, T_BREAKDOWN, T_FADE):
            d, _ = run(mk(), self.bars(), at(9, 6))
            self.assertNotEqual(d["action"], "invalidate", mk().get("ticket_id"))
            self.assertEqual(d["action"], "wait", (mk().get("ticket_id"), d["reason"]))

    def test_old_signature_never_invalidates(self):
        b = self.bars()
        self.assertEqual(rth.pre_trigger_invalidated(rth.compute_or(b, at(9, 6)), b, at(9, 6)), (False, ""))


class TickIntegration(unittest.TestCase):
    """alpaca_rth_tick with every I/O path stubbed: the unparsed ticket stays pending and the
    regression tickets are neither invalidated nor submitted."""
    def test_tick(self):
        tickets = [T_BREAKOUT(), T_BREAKDOWN(), T_FADE(), Unparseable().ticket()]
        for t in tickets:
            t["session_target"] = "2030-01-08"
        rewrites, submits = [], []
        bars = Regression751().bars()
        saved = {}
        patches = {
            "_now_ct": lambda: at(9, 6),
            "armed_tickets": lambda: tickets,
            "load_events": lambda: {"events": []},
            "save_events": lambda doc: None,
            "rewrite_blotter_ticket": lambda tid, fn: rewrites.append(tid),
            "fetch_alpaca_bars": lambda pe, tf, start: [dict(b) for b in bars],
            "manage_open_positions": lambda *a, **k: [],
            "push_vault_if_changed": lambda pe, c, p: {},
            "already_submitted": lambda pe, t, d: (False, "clear"),
            "submit_ticket": lambda *a, **k: submits.append(a[1]["ticket_id"]),
        }

        class PE:
            ALPACA_PAPER_HOST = "https://paper-api.alpaca.markets"
            @staticmethod
            def assert_paper_host(h): assert "paper-api" in h
            @staticmethod
            def alpaca_status(): return {"status": "ok"}
            @staticmethod
            def alpaca_request(*a, **k): return {"is_open": True}

        for k, v in patches.items():
            saved[k] = getattr(rth, k); setattr(rth, k, v)
        try:
            res = rth.alpaca_rth_tick(PE)
        finally:
            for k, v in saved.items():
                setattr(rth, k, v)
        self.assertEqual(rewrites, []); self.assertEqual(submits, [])
        self.assertIn("pending_unparsed:TKT-TEST-JUNK", res["actions"])
        self.assertFalse(any(str(a).startswith("invalidated:") for a in res["actions"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)

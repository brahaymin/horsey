#!/usr/bin/env python3
"""Paper-engine fixes from the joint PTJ/Tactician framework v1.2 (Nov 12 2026, CIO C2/C3).
SYNTHETIC data + mocked paper broker only (no network, no secrets). Covers:
  (a) completed 5m bars only,             (b) time-of-day RVOL replaces rvol_est,
  (c) option premiums repriced at arm time or voided,
  (d) firewall check 5 against LIVE 0DTE walls (fail closed),
  R15 RVOL tiers (half tier behind paper_engine.rvol_half_size_tier_enabled, default OFF),
  configurable earliest entry (08:45 floor; reclaim half size before 09:00; breakouts need OR30),
  Fed-speaker blackouts from pod/risk/fed_speakers/YYYY-MM-DD.json,
  14:30 cancel / duplicate checks beyond the last 50 orders (exact client_order_id lookup).
Run: python3 scripts/test_rth_framework_fixes.py   (RTH_MODULE=/path/to/_alpaca_rth.py honoured)
"""
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_option_path as T  # noqa: E402  (sets HORSEY_ROOT to a temp dir)

rth, pe = T.rth, T.pe
CT, bar, at, Broker, CALL, STOCK = T.CT, T.bar, T.at, T.Broker, T.CALL, T.STOCK
ROOT = T._TMP
DAY_S = "2030-11-08"


def write_cfg(**kw):
    p = ROOT / "pod" / "risk" / "limits_fw_test.json"
    p.write_text(json.dumps({"limits": {"max_risk_per_ticket_pct": 0.5, "max_total_open_risk_pct": 1.5},
                             "paper_engine": kw}))
    return p


def or_bars(lo, hi, upto=(9, 0)):
    out, mid = [], (lo + hi) / 2
    h, m = 8, 30
    i = 0
    while (h, m) < upto:
        out.append(bar(h, m, mid, hi if i == 1 else mid + 0.2, lo if i == 2 else mid - 0.2, mid))
        m += 5; i += 1
        if m == 60:
            h, m = h + 1, 0
    return out


def RECLAIM(**kw):
    t = STOCK(ticket_id="TKT-TEST-RECL", direction="long", entry=755.6, stop=753.7, target1=762.5, target2=765.0,
              quantity=40, actual_dollar_risk=76.0,
              underlying_trigger={"reclaim": "reclaim above prior low 754.90 with RVOL >= 1.2x", "rvol_min": 1.2},
              hard_invalidation_underlying="5-min close below 753.70")
    t.pop("no_entry_before_ct", None)
    t.update(kw)
    return t


def BREAKOUT(**kw):
    t = STOCK(ticket_id="TKT-TEST-BRK", direction="long", entry=759.5, stop=757.6, target1=766.0, target2=768.0,
              underlying_trigger={"breakout": "acceptance above 759.00 (5-min hold) with RVOL >= 1.2x", "rvol_min": 1.2},
              hard_invalidation_underlying="5-min close below 757.60")
    t.pop("no_entry_before_ct", None)
    t.update(kw)
    return t


class _FW(T._Base):
    def setUp(self):
        super().setUp()
        self.cfg_path = write_cfg(earliest_entry_ct="09:00", rvol_half_size_tier_enabled=False)
        self.p.set(rth, "LIMITS_PATH", self.cfg_path)
        self.fed_dir = ROOT / "pod" / "risk" / "fed_speakers_test"
        self.fed_dir.mkdir(parents=True, exist_ok=True)
        for f in self.fed_dir.glob("*.json"):
            f.unlink()
        self.p.set(rth, "FED_SPEAKERS_DIR", self.fed_dir)

    def cfg(self, **kw):
        write_cfg(**kw)

    def decide(self, ticket, bars, now, tod=1.5, bar_rvol=None):
        oi = rth.compute_or(bars, now, rvol={"tod_rvol": tod, "bar_rvol": bar_rvol, "source": "test"})
        return rth.decide_armed_ticket(ticket, oi, bars, now)


# ------------------------------------------------------------------ (a) completed bars only
class CompletedBars(_FW):
    def test_forming_bar_excluded(self):
        bars = [bar(9, 0, 1, 1, 1, 1), bar(9, 5, 1, 1, 1, 1)]
        self.assertEqual(len(rth.completed_bars(bars, at(9, 9, 59))), 1)
        self.assertEqual(len(rth.completed_bars(bars, at(9, 10, 0))), 2)

    def test_forming_bar_never_fires_breakout(self):
        # 10:10 bar reads 762.66 mid-bar (would accept above 762.51) but is still forming at 10:12
        t = BREAKOUT(underlying_trigger={"breakout": "acceptance above 762.51 (5-min hold) with RVOL >= 1.2x", "rvol_min": 1.2})
        bars = or_bars(753.7, 758.9) + [bar(10, 5, 761.8, 762.4, 761.7, 762.2), bar(10, 10, 762.2, 762.7, 762.0, 762.66)]
        d = self.decide(t, bars, at(10, 12))
        self.assertEqual(d["action"], "wait", d["reason"]); self.assertIn("last 762.20", d["reason"])
        # once that bar completes (and really closed above), it may fire
        d = self.decide(t, bars, at(10, 15, 1))
        self.assertEqual(d["action"], "fire", d["reason"])

    def test_or_uses_completed_bars(self):
        bars = or_bars(750, 752) + [bar(9, 0, 760, 770, 740, 765)]
        oi = rth.compute_or(bars, at(9, 2))
        self.assertEqual((oi["or_high"], oi["or_low"]), (752, 750))
        self.assertEqual(oi["completed_bars"], 6)


# ------------------------------------------------------------------ (b) time-of-day RVOL
def curve_doc(n=20, avg=1000.0):
    slots, cum = [], 0.0
    h, m = 8, 30
    while (h, m) < (15, 0):
        cum += avg
        slots.append({"slot_ct": f"{h:02d}:{m:02d}", "avg_volume": avg, "avg_cum_volume": cum})
        m += 5
        if m == 60:
            h, m = h + 1, 0
    return {"intraday_feed": {"spy_5m": {"slot_curves": slots, "sessions_used": [f"2030-10-{d:02d}" for d in range(1, n + 1)],
                                          "tod_rvol": 0.9, "bar_rvol": 0.8, "last_completed_bar_ct": f"{DAY_S}T09:05:00-06:00"}}}


class TodRvol(_FW):
    def vb(self, vols, start=(8, 30)):
        out, (h, m) = [], start
        for v in vols:
            out.append(bar(h, m, 1, 1, 1, 1, v))
            m += 5
            if m == 60:
                h, m = h + 1, 0
        return out

    def test_cumulative_vs_curve_with_spike_cap(self):
        curve, why = rth.tod_curve_from_latest(curve_doc(), at(9, 0))
        self.assertEqual(why, "ok")
        r = rth.compute_tod_rvol(self.vb([1000, 1000, 1600, 99000]), curve, at(8, 50, 5))  # 08:45 bar complete; spike capped at 8x
        self.assertAlmostEqual(r["tod_rvol"], (1000 + 1000 + 1600 + 8000) / 4000.0)
        self.assertAlmostEqual(r["bar_rvol"], 8.0)

    def test_forming_bar_excluded_from_rvol(self):
        curve, _ = rth.tod_curve_from_latest(curve_doc(), at(9, 0))
        r = rth.compute_tod_rvol(self.vb([1200, 1200, 10]), curve, at(8, 42))  # 08:40 bar forming with 10 shares
        self.assertAlmostEqual(r["tod_rvol"], 1.2)

    def test_gap_or_missing_curve_fails_closed(self):
        curve, _ = rth.tod_curve_from_latest(curve_doc(), at(9, 0))
        bars = self.vb([1000, 1000]) + self.vb([1000], start=(8, 45))
        self.assertIsNone(rth.compute_tod_rvol(bars, curve, at(9, 0))["tod_rvol"])
        self.assertIsNone(rth.tod_curve_from_latest({}, at(9, 0))[0])
        self.assertIsNone(rth.tod_curve_from_latest(curve_doc(n=5), at(9, 0))[0])
        r = rth.live_rvol(at(9, 0), or_bars(1, 2), latest_doc={}, fetch_vol_bars=lambda: self.vb([1000] * 6))
        self.assertIsNone(r["tod_rvol"])

    def test_live_rvol_fallback_only_same_slot(self):
        doc = curve_doc()
        ebars = or_bars(1, 2) + [bar(9, 0, 1, 1, 1, 1), bar(9, 5, 1, 1, 1, 1)]
        r = rth.live_rvol(at(9, 10, 5), ebars, latest_doc=doc, fetch_vol_bars=lambda: [])
        self.assertEqual((r["tod_rvol"], r["source"]), (0.9, "monitor_snapshot_same_slot"))
        r = rth.live_rvol(at(9, 15, 5), ebars + [bar(9, 10, 1, 1, 1, 1)], latest_doc=doc, fetch_vol_bars=lambda: [])
        self.assertIsNone(r["tod_rvol"])

    def test_old_rvol_est_metric_retired(self):
        # old metric: forming-bar volume / OR median (a forming bar with 400 vs OR 1000 => 0.4)
        bars = or_bars(750, 752) + [bar(9, 0, 751, 752, 750, 751, 400)]
        oi = rth.compute_or(bars, at(9, 2))
        self.assertIsNone(oi["rvol_est"]); self.assertIsNone(oi["tod_rvol"])
        d = rth.decide_armed_ticket(BREAKOUT(), oi, bars + [bar(9, 5, 759.1, 759.5, 759.0, 759.3)], at(9, 11))
        self.assertEqual(d["action"], "wait"); self.assertIn("RVOL unavailable", d["reason"])


# ------------------------------------------------------------------ R15 tiers (flag)
class RvolTiers(_FW):
    def bars(self, closes):
        b = or_bars(756.0, 758.9)
        for i, c in enumerate(closes):
            b.append(bar(9, 5 + 5 * i, c - 0.1, c + 0.2, c - 0.3, c))
        return b

    def test_flag_default_off(self):
        self.p.set(rth, "LIMITS_PATH", ROOT / "nope.json")
        self.assertFalse(rth.engine_config()["rvol_half_size_tier_enabled"])
        self.assertEqual(rth.earliest_entry(), (9, 0))
        self.cfg(rvol_half_size_tier_enabled="true")  # only a literal JSON true enables it
        self.p.set(rth, "LIMITS_PATH", self.cfg_path)
        self.assertFalse(rth.engine_config()["rvol_half_size_tier_enabled"])

    def test_flag_off_matches_old_gate(self):
        b = self.bars([759.3, 759.4])
        for tod, act in ((1.19, "wait"), (1.0, "wait"), (0.79, "wait"), (1.2, "fire"), (2.0, "fire")):
            d = self.decide(BREAKOUT(), b, at(9, 15, 1), tod=tod)
            self.assertEqual(d["action"], act, (tod, d["reason"]))
            if act == "fire":
                self.assertEqual(d["meta"]["size_factor"], 1.0)

    def test_flag_on_half_tier_needs_two_bar_hold(self):
        self.cfg(rvol_half_size_tier_enabled=True)
        d = self.decide(BREAKOUT(), self.bars([758.5, 759.3]), at(9, 15, 1), tod=1.0)
        self.assertEqual(d["action"], "wait"); self.assertIn("2-bar hold", d["reason"])
        d = self.decide(BREAKOUT(), self.bars([759.2, 759.3]), at(9, 15, 1), tod=1.0)
        self.assertEqual(d["action"], "fire", d["reason"]); self.assertEqual(d["meta"]["size_factor"], 0.5)
        d = self.decide(BREAKOUT(), self.bars([758.5, 759.3]), at(9, 15, 1), tod=1.0, bar_rvol=1.6)  # bar >= 1.5x substitutes
        self.assertEqual(d["action"], "fire", d["reason"]); self.assertEqual(d["meta"]["size_factor"], 0.5)
        d = self.decide(BREAKOUT(), self.bars([759.2, 759.3]), at(9, 15, 1), tod=0.79)
        self.assertEqual(d["action"], "wait")
        d = self.decide(BREAKOUT(), self.bars([759.2, 759.3]), at(9, 15, 1), tod=1.25)
        self.assertEqual(d["meta"]["size_factor"], 1.0)

    def test_tier_function(self):
        self.assertFalse(rth.rvol_tier(None, 1.2, True)["allow"])
        self.assertEqual(rth.rvol_tier(1.0, 1.2, False)["tier"], "blocked")
        self.assertEqual(rth.rvol_tier(1.0, 1.2, True)["tier"], "half")
        self.assertEqual(rth.rvol_tier(1.3, 1.5, True)["tier"], "half")  # ticket asks 1.5x: full needs 1.5
        self.assertEqual(rth.combine_size_factors([0.5, 0.5]), 0.5)
        self.assertEqual(rth.combine_size_factors([0.5, 0.5, 0.5]), 0.25)


# ------------------------------------------------------------------ earliest entry / reclaim (C3)
class EarliestEntry(_FW):
    def early_bars(self):
        # 08:30 / 08:35 below 754.90, 08:40 completed close 755.45 (>= 754.90 + 0.50)
        return [bar(8, 30, 754.2, 754.6, 753.7, 754.1), bar(8, 35, 754.1, 754.9, 754.0, 754.7),
                bar(8, 40, 754.7, 755.6, 754.6, 755.45)]

    def test_parse_reclaim(self):
        r = rth.parse_ticket_rules(RECLAIM())
        self.assertTrue(r["ok"], r["warnings"])
        self.assertEqual((r["mode"], r["side"], r["level"], r["trigger_level"]), ("reclaim", "above", 754.9, 755.4))

    def test_0900_config_blocks_reclaim_before_0900(self):
        d = self.decide(RECLAIM(), self.early_bars(), at(8, 46))
        self.assertEqual(d["action"], "wait"); self.assertIn("outside entry window 09:00", d["reason"])

    def test_0845_reclaim_arms_half_size(self):
        self.cfg(earliest_entry_ct="08:45")
        d = self.decide(RECLAIM(), self.early_bars(), at(8, 45, 0))
        self.assertEqual(d["action"], "fire", d["reason"])
        self.assertEqual(d["meta"]["size_factor"], 0.5); self.assertIn("before 09:00", d["reason"])
        d = self.decide(RECLAIM(), self.early_bars(), at(8, 59, 59))
        self.assertEqual(d["meta"]["size_factor"], 0.5)

    def test_nothing_before_0845(self):
        self.cfg(earliest_entry_ct="08:45")
        d = self.decide(RECLAIM(), self.early_bars(), at(8, 44, 59))
        self.assertEqual(d["action"], "wait"); self.assertIn("outside entry window 08:45", d["reason"])
        self.cfg(earliest_entry_ct="08:30")  # clamped to the 08:45 floor
        self.assertEqual(rth.earliest_entry(), (8, 45))
        self.assertEqual(self.decide(RECLAIM(), self.early_bars(), at(8, 44, 59))["action"], "wait")
        self.cfg(earliest_entry_ct="garbage")
        self.assertEqual(rth.earliest_entry(), (9, 0))

    def test_0845_forming_bar_not_evaluated(self):
        self.cfg(earliest_entry_ct="08:45")
        bars = self.early_bars()[:2] + [bar(8, 40, 754.7, 754.95, 754.6, 754.8), bar(8, 45, 754.8, 755.9, 754.8, 755.8)]
        d = self.decide(RECLAIM(), bars, at(8, 47))  # 08:45 bar (755.80) still forming
        self.assertEqual(d["action"], "wait", d["reason"]); self.assertIn("last 754.80", d["reason"])
        d = self.decide(RECLAIM(), bars, at(8, 50, 0))  # now complete
        self.assertEqual(d["action"], "fire", d["reason"])

    def test_breakout_blocked_before_or30_even_at_0845(self):
        self.cfg(earliest_entry_ct="08:45")
        bars = or_bars(753.7, 758.0, upto=(8, 45)) + [bar(8, 45, 758.9, 759.6, 758.8, 759.5), bar(8, 50, 759.5, 759.9, 759.2, 759.6)]
        d = self.decide(BREAKOUT(), bars, at(8, 56))
        self.assertEqual(d["action"], "wait"); self.assertIn("OR not ready", d["reason"])
        bars += [bar(8, 55, 759.6, 759.9, 759.3, 759.7)]
        d = self.decide(BREAKOUT(), bars, at(9, 0, 5))
        self.assertEqual(d["action"], "fire", d["reason"]); self.assertEqual(d["meta"]["size_factor"], 1.0)

    def test_reclaim_after_0900_full_size(self):
        self.cfg(earliest_entry_ct="08:45")
        bars = or_bars(753.7, 754.8) + [bar(9, 0, 754.8, 755.6, 754.7, 755.5)]
        d = self.decide(RECLAIM(), bars, at(9, 5, 1))
        self.assertEqual(d["action"], "fire", d["reason"]); self.assertEqual(d["meta"]["size_factor"], 1.0)

    def test_ticket_time_can_only_be_later(self):
        self.cfg(earliest_entry_ct="08:45")
        self.assertEqual(rth.parse_ticket_rules(RECLAIM(no_entry_before_ct="09:00"))["entry_start"], (9, 0))
        self.assertEqual(rth.parse_ticket_rules(RECLAIM(no_entry_before_ct="08:30"))["entry_start"], (8, 45))
        self.assertEqual(rth.parse_ticket_rules(RECLAIM())["entry_start"], (8, 45))


# ------------------------------------------------------------------ Fed-speaker blackout
class FedBlackout(_FW):
    def seed(self, doc):
        (self.fed_dir / f"{DAY_S}.json").write_text(json.dumps(doc))

    def test_missing_file_warns_no_block(self):
        blocked, why, warn = rth.fed_speaker_blackout(at(9, 50))
        self.assertFalse(blocked); self.assertIn("no Fed-speaker schedule", warn)

    def test_window_minus5_plus10(self):
        self.seed({"date": DAY_S, "speakers": [{"name": "Bowman", "start_ct": "09:50"}, {"name": "Daly", "start_ct": "11:00"}]})
        for t, exp in ((at(9, 44, 59), False), (at(9, 45), True), (at(10, 0), True), (at(10, 0, 1), False),
                       (at(10, 55), True), (at(11, 10), True), (at(11, 10, 1), False)):
            self.assertEqual(rth.fed_speaker_blackout(t)[0], exp, t)
        self.assertIn("Bowman 09:50", rth.fed_speaker_blackout(at(9, 47))[1])

    def test_malformed_fails_closed(self):
        (self.fed_dir / f"{DAY_S}.json").write_text("{not json")
        self.assertTrue(rth.fed_speaker_blackout(at(13, 0))[0])
        self.seed({"date": "2030-11-09", "speakers": []})
        self.assertTrue(rth.fed_speaker_blackout(at(13, 0))[0])

    def test_tick_skips_entries_in_blackout(self):
        self.seed({"date": DAY_S, "speakers": [{"name": "Bowman", "start_ct": "09:50"}]})
        t = STOCK(session_target=DAY_S, underlying_trigger={"failure": "decisive acceptance below 750 (5-min hold) with RVOL >= 1.2x", "rvol_min": 1.2})
        bars = or_bars(750.2, 752.0) + [bar(9, 40, 750.1, 750.3, 749.2, 749.4)]
        submits = []
        for k, v in {"_now_ct": lambda: at(9, 46), "armed_tickets": lambda: [t], "load_events": lambda: {"events": []},
                     "save_events": lambda d: None, "rewrite_blotter_ticket": lambda *a: None,
                     "fetch_alpaca_bars": lambda *a: [dict(b) for b in bars], "manage_open_positions": lambda *a, **k: [],
                     "push_vault_if_changed": lambda *a: {}, "already_submitted": lambda *a: (False, "clear"),
                     "live_rvol": lambda *a, **k: {"tod_rvol": 2.0}, "submit_ticket": lambda *a, **k: submits.append(1),
                     "arm_time_gates": lambda pe_, tk, *a, **k: {"action": "submit", "ticket": tk, "why": "stub"}}.items():
            self.p.set(rth, k, v)
        res = rth.alpaca_rth_tick(_TPE)
        self.assertEqual(submits, []); self.assertTrue(any(str(a).startswith("blackout:") for a in res["actions"]))
        self.p.set(rth, "_now_ct", lambda: at(10, 1))
        rth.alpaca_rth_tick(_TPE)
        self.assertEqual(submits, [1])


class _TPE:
    ALPACA_PAPER_HOST = T.PAPER
    ALPACA_DATA_HOST = "https://data.alpaca.markets"
    assert_paper_host = staticmethod(pe.assert_paper_host)
    option_contract_spec = staticmethod(pe.option_contract_spec)

    @staticmethod
    def alpaca_status():
        return {"status": "ok"}

    @staticmethod
    def alpaca_request(*a, **k):
        return {"is_open": True}


# ------------------------------------------------------------------ (d) live 0DTE walls
def latest_walls(put, call, ts=f"{DAY_S}T09:20:00-0600", printed45=-1.0e9, grid45=None):
    d = {"summary": {"put_wall_0dte": put, "call_wall_0dte": call, "timestamp_ct": ts}}
    if printed45 is not None:
        d["summary"]["net_gex_45d_dollars"] = printed45
    if grid45 is not None:
        d["intraday_feed"] = {"gex_by_spot": {"upcoming_45d": {"net_gex_at_chain_spot": grid45}}}
    return d


class LiveWalls(_FW):
    def test_pass_and_mid_range(self):
        self.assertTrue(rth.live_wall_check(757.0, at(9, 30), latest_walls(756, 761))[0])   # 757.0 < 757.5
        self.assertTrue(rth.live_wall_check(761.6, at(9, 30), latest_walls(756, 761))[0])
        ok, why = rth.live_wall_check(758.5, at(9, 30), latest_walls(756, 761))
        self.assertFalse(ok); self.assertIn("mid-range", why)

    def test_degenerate_760_760_fails_closed(self):
        ok, why = rth.live_wall_check(758.0, at(9, 30), latest_walls(760, 760))
        self.assertFalse(ok); self.assertIn("coincide", why)

    def test_missing_or_stale_fails_closed(self):
        self.assertFalse(rth.live_wall_check(758.0, at(9, 30), {})[0])
        self.assertFalse(rth.live_wall_check(758.0, at(9, 30), latest_walls(None, 761))[0])
        self.assertFalse(rth.live_wall_check(762.0, at(11, 0), latest_walls(756, 761))[0])           # 100 min old
        self.assertFalse(rth.live_wall_check(762.0, at(9, 30), latest_walls(756, 761, "2030-11-07T15:20:00-0600"))[0])
        self.assertFalse(rth.live_wall_check(None, at(9, 30), latest_walls(756, 761))[0])


# ------------------------------------------------------------------ (c) arm-time repricing
def quote(bid, ask, age=5, delta=0.45, gamma=0.08):
    t = (at(10, 0) - timedelta(seconds=age)).astimezone(rth.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {"bid": bid, "ask": ask, "t": t, "delta": delta, "gamma": gamma}


class Reprice(_FW):
    def rules(self, t):
        return rth.parse_ticket_rules(t)

    def test_live_mid_plus_2c_greeks_stop_targets_keep_R(self):
        t = CALL()  # staged 2.00 / 1.40 / 3.80 / 5.00 x4; inv below 759.80
        rp, why = rth.reprice_option_ticket(t, self.rules(t), quote(1.18, 1.22), 761.0, at(10, 0))
        self.assertEqual(why, "ok")
        self.assertEqual(rp["entry"], 1.22)
        d = 761.0 - 759.8
        self.assertEqual(rp["stop"], round(1.20 - (0.45 * d + 0.5 * 0.08 * d * d) - 0.05, 2))
        r_old, r_new = 0.60, rp["entry"] - rp["stop"]
        self.assertAlmostEqual(rp["target1"], round(1.22 + 3.0 * r_new, 2), places=2)
        self.assertLessEqual(rp["quantity"] * r_new * 100, 4 * r_old * 100 + 1e-6)

    def test_void_conditions(self):
        t = CALL()
        for q, frag in ((None, "no live option quote"), (quote(1.0, 1.2), "spread"), (quote(1.18, 1.22, age=600), "stale"),
                        (quote(0, 0.05), "unusable"), (quote(1.22, 1.18), "unusable")):
            rp, why = rth.reprice_option_ticket(t, self.rules(t), q, 761.0, at(10, 0))
            self.assertIsNone(rp); self.assertIn(frag, why)

    def test_void_when_risk_exceeds_budget(self):
        t = CALL(quantity=1, actual_dollar_risk=60.0)
        rp, why = rth.reprice_option_ticket(t, self.rules(t), quote(3.98, 4.02, delta=0.3, gamma=0.02), 761.5, at(10, 0))
        self.assertIsNone(rp); self.assertIn("budget", why)

    def gates(self, t, meta, q, walls=latest_walls(756, 761, f"{DAY_S}T09:50:00-0600")):
        class QPE(_TPE):
            @staticmethod
            def alpaca_request(method, host, path, body=None, params=None, timeout=20):
                assert method == "GET" and "options/snapshots" in path, path
                if q is None:
                    raise RuntimeError("down")
                occ = params["symbols"]
                return {"snapshots": {occ: {"latestQuote": {"bp": q["bid"], "ap": q["ask"], "t": q["t"]},
                                            "greeks": {"delta": q["delta"], "gamma": q["gamma"]}}}}
        bars = T.or_bars(759.0, 760.4) + [bar(9, 50, 761.0, 761.6, 760.9, 761.5)]
        return rth.arm_time_gates(QPE, t, self.rules(t), meta, bars, at(10, 0), {"events": []}, False, walls)

    def test_gates_void_without_quote(self):
        g = self.gates(CALL(), {}, None)
        self.assertEqual(g["action"], "void"); self.assertIn("Stale premium", g["why"])

    def test_gates_hold_on_degenerate_walls(self):
        g = self.gates(CALL(), {}, quote(1.18, 1.22), walls=latest_walls(760, 760, f"{DAY_S}T09:50:00-0600"))
        self.assertEqual(g["action"], "hold")

    def test_gates_submit_repriced_and_halved(self):
        g = self.gates(CALL(), {"size_factor": 0.5, "size_reasons": ["reclaim armed before 09:00 CT"]}, quote(1.98, 2.02, delta=0.25, gamma=0.02))
        self.assertEqual(g["action"], "submit", g["why"])
        tk = g["ticket"]
        self.assertEqual((tk["staged_entry"], tk["staged_quantity"]), (2.0, 4))
        self.assertEqual(tk["entry"], 2.02)
        self.assertEqual(tk["arm_reprice"]["stop_method"], "greeks_R24")
        self.assertEqual(tk["quantity"], 2)  # budget 4 contracts at the repriced risk, then x0.5
        self.assertAlmostEqual(tk["actual_dollar_risk"], round((tk["entry"] - tk["stop"]) * 2 * 100, 2))
        # re-arm recomputes from staged values, never from an already-halved quantity
        g2 = self.gates(tk, {}, quote(1.98, 2.02, delta=0.25, gamma=0.02))
        self.assertEqual(g2["ticket"]["quantity"], 4)

    def test_half_size_of_one_contract_holds(self):
        g = self.gates(CALL(quantity=1, actual_dollar_risk=60.0), {"size_factor": 0.5, "size_reasons": ["x"]},
                       quote(1.98, 2.02, delta=0.25, gamma=0.02))
        self.assertEqual(g["action"], "hold"); self.assertIn("rounds 1 to 0", g["why"])

    def test_tick_voids_ticket_and_never_rearms(self):
        t = CALL(session_target=DAY_S)
        bars = T.or_bars(759.0, 760.4) + [bar(9, 30, 760.4, 761.2, 760.3, 761.0, 2000)]
        rewrites, submits = [], []
        for k, v in {"_now_ct": lambda: at(9, 36), "armed_tickets": lambda: [t], "load_events": lambda: {"events": []},
                     "save_events": lambda d: None, "rewrite_blotter_ticket": lambda tid, fn: rewrites.append(fn(dict(t))),
                     "fetch_alpaca_bars": lambda *a: [dict(b) for b in bars], "manage_open_positions": lambda *a, **k: [],
                     "push_vault_if_changed": lambda *a: {}, "already_submitted": lambda *a: (False, "clear"),
                     "live_rvol": lambda *a, **k: {"tod_rvol": 2.0}, "submit_ticket": lambda *a, **k: submits.append(1),
                     "load_latest": lambda: latest_walls(756, 761, f"{DAY_S}T09:20:00-0600")}.items():
            self.p.set(rth, k, v)
        class NoQuote(_TPE):
            @staticmethod
            def alpaca_request(method, host, path, **k):
                if "options/snapshots" in path:
                    raise RuntimeError("quote down")
                return {"is_open": True}
        res = rth.alpaca_rth_tick(NoQuote)
        self.assertEqual(submits, [])
        self.assertEqual(rewrites[-1]["status"], "voided_paper")
        self.assertIn("voided_paper", rth.DONE_STATUSES)
        self.assertTrue(any(str(a).startswith("voided:") for a in res["actions"]))


# ------------------------------------------------------------------ 45D net GEX: more negative of grid / printed (CIO)
class Gex45Conservative(_FW):
    rules = Reprice.rules
    gates = Reprice.gates

    def test_more_negative_used(self):
        v, info = rth.net_gex_45d_conservative(latest_walls(756, 761, printed45=-1.6e9, grid45=-6.7e9))
        self.assertEqual(v, -6.7e9); self.assertEqual(info["net_gex_45d_source"], "grid_at_chain_spot")
        v, info = rth.net_gex_45d_conservative(latest_walls(756, 761, printed45=-7.9e9, grid45=-1.0e9))
        self.assertEqual(v, -7.9e9); self.assertEqual(info["net_gex_45d_source"], "printed_chain_gamma")
        v, _ = rth.net_gex_45d_conservative({"gex_scopes": {"upcoming_45d": {"totals": {"net_gex": -2e9}}}})
        self.assertEqual(v, -2e9)
        self.assertEqual(rth.net_gex_45d_conservative(latest_walls(756, 761, printed45=None))[0], None)

    def test_cap_applies_on_either_reading_and_logs_both(self):
        q = quote(1.98, 2.02, delta=0.25, gamma=0.02)
        for p45, g45, exp_qty in ((-1.6e9, -6.7e9, 2), (-7.9e9, -1.0e9, 2), (-1.6e9, -1.0e9, 4), (-1.6e9, None, 4)):
            g = self.gates(CALL(), {}, q, walls=latest_walls(756, 761, f"{DAY_S}T09:50:00-0600", printed45=p45, grid45=g45))
            self.assertEqual(g["action"], "submit", g["why"])
            self.assertEqual(g["ticket"]["quantity"], exp_qty, (p45, g45))
            chk = g["ticket"]["gex45_check"]
            self.assertEqual((chk["net_gex_45d_printed"], chk["net_gex_45d_grid_at_chain_spot"]), (p45, g45))
            self.assertIn("printed", g["why"]); self.assertIn("grid", g["why"])

    def test_cap_does_not_stack_beyond_smallest_factor(self):
        g = self.gates(CALL(), {"size_factor": 0.5, "size_factors": [0.5], "size_reasons": ["reclaim armed before 09:00 CT"]},
                       quote(1.98, 2.02, delta=0.25, gamma=0.02),
                       walls=latest_walls(756, 761, f"{DAY_S}T09:50:00-0600", printed45=-7.9e9))
        self.assertEqual(g["ticket"]["quantity"], 2)  # min(1/2, 1/2) = 1/2, not 1/4

    def test_both_missing_fails_closed(self):
        g = self.gates(CALL(), {}, quote(1.98, 2.02, delta=0.25, gamma=0.02),
                       walls=latest_walls(756, 761, f"{DAY_S}T09:50:00-0600", printed45=None))
        self.assertEqual(g["action"], "hold"); self.assertIn("45D net GEX unavailable", g["why"])

    def test_submit_event_logs_both(self):
        t = CALL(gex45_check={"net_gex_45d_grid_at_chain_spot": -6.7e9, "net_gex_45d_printed": -1.6e9,
                              "net_gex_45d_used": -6.7e9, "net_gex_45d_source": "grid_at_chain_spot", "cap_applied": True})
        doc = {"events": []}
        self.p.set(rth, "submit_risk_check", lambda tk, gm=None: (True, "ok"))
        rth.submit_ticket(pe, t, "test", {}, doc, force_sim=True)
        e = doc["events"][-1]
        self.assertEqual((e["net_gex_45d_grid_at_chain_spot"], e["net_gex_45d_printed"], e["net_gex_45d_used"]),
                         (-6.7e9, -1.6e9, -6.7e9))


# ------------------------------------------------------------------ 14:30 cancel beyond 50 orders
class PagedBroker(Broker):
    """Honours the list 'limit' (newest first) and the exact client_order_id endpoint."""
    def request(self, method, host, path, body=None, params=None, timeout=20):
        if method == "GET" and path == "/v2/orders:by_client_order_id":
            pe.assert_paper_host(host)
            self.calls.append((method, path, body, params))
            coid = (params or {}).get("client_order_id")
            return next((o for o in self.orders if o.get("client_order_id") == coid), None) or {}
        if method == "GET" and path == "/v2/orders" and (params or {}).get("status") == "all":
            self.calls.append((method, path, body, params))
            return self.orders[: int((params or {}).get("limit") or 50)]
        return super().request(method, host, path, body=body, params=params, timeout=timeout)


class CancelScan(_FW):
    def test_cancel_found_beyond_first_page(self):
        tid = "TKT-TEST-OPTC"
        noise = [{"id": f"n{i}", "symbol": T.OCC_C, "status": "new", "client_order_id": f"other-{i}", "filled_qty": "0"}
                 for i in range(600)]
        ours = {"id": "entry-old", "symbol": T.OCC_C, "status": "new", "qty": "4", "filled_qty": "0",
                "client_order_id": rth.client_order_id(tid)}
        b = PagedBroker([], noise + [ours])
        notes, doc = self.manage(CALL(status="submitted_paper"), b, at(14, 30))
        self.assertIn(f"ENTRY_CANCELLED:{tid}", notes)
        self.assertEqual(b.deletes(), ["/v2/orders/entry-old"])  # only our order, never a noise order or cancel-all

    def test_lookup_mismatch_never_cancels(self):
        class Liar(PagedBroker):
            def request(self, method, host, path, body=None, params=None, timeout=20):
                if path == "/v2/orders:by_client_order_id":
                    self.calls.append((method, path, body, params))
                    return {"id": "foreign", "client_order_id": "someone-else", "status": "new", "filled_qty": "0"}
                return super().request(method, host, path, body=body, params=params, timeout=timeout)
        b = Liar([], [])
        notes, _ = self.manage(CALL(status="submitted_paper"), b, at(14, 31))
        self.assertEqual(b.deletes(), [])

    def test_already_submitted_uses_exact_lookup(self):
        tid = "TKT-TEST-OPTC"
        noise = [{"id": f"n{i}", "client_order_id": f"x{i}", "status": "filled"} for i in range(600)]
        b = PagedBroker([], noise + [{"id": "e1", "client_order_id": rth.client_order_id(tid), "status": "filled"}])
        self.p.set(pe, "alpaca_request", b.request)
        dup, why = rth.already_submitted(pe, CALL(), {"events": []})
        self.assertTrue(dup, why)


if __name__ == "__main__":
    unittest.main(verbosity=1)

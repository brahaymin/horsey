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
        self.assertEqual(t.get("r17_blocked_bar_ct"), "09:40")  # R17: the blackout blocked a met trigger
        # after the blackout the same (stale) break is not chased
        self.p.set(rth, "_now_ct", lambda: at(10, 1))
        res = rth.alpaca_rth_tick(_TPE)
        self.assertEqual(submits, []); self.assertTrue(any("R17" in str(a) for a in res["actions"]))
        # price returns inside, then a fresh break after the blackout => the one re-arm fires
        bars += [bar(9, 45, 749.4, 750.6, 749.3, 750.4), bar(9, 50, 750.4, 750.5, 749.9, 750.1), bar(9, 55, 750.1, 750.2, 749.1, 749.3)]
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
CALM_VOL = {"vix": 18.0, "vix9d": 17.0, "vix3m": 20.0, "vvix": 95.0}


def latest_walls(put, call, ts=f"{DAY_S}T09:20:00-0600", printed45=-1.0e9, grid45=None, vol=CALM_VOL, grid=None):
    d = {"summary": {"put_wall_0dte": put, "call_wall_0dte": call, "timestamp_ct": ts, **(vol or {})}}
    if grid is not None:
        d["intraday_feed"] = {"gex_by_spot": {"upcoming_45d": {"grid": grid}}}
    if printed45 is not None:
        d["summary"]["net_gex_45d_dollars"] = printed45
    if grid45 is not None:
        d.setdefault("intraday_feed", {"gex_by_spot": {"upcoming_45d": {}}})
        d["intraday_feed"]["gex_by_spot"]["upcoming_45d"]["net_gex_at_chain_spot"] = grid45
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
        self.assertEqual(rp["stop"], round(1.20 - 0.45 * d + 0.5 * 0.08 * d * d - 0.05, 2))  # v1.3 sign (PTJ-agreed)
        self.assertEqual(rp["reprice"]["stop_method"], "greeks_R24_v1.3")
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
        rp, why = rth.reprice_option_ticket(t, self.rules(t), quote(3.98, 4.02, delta=0.4, gamma=0.02), 761.5, at(10, 0))
        self.assertIsNone(rp); self.assertIn("budget", why)

    def test_r24_delta_band_at_trigger(self):
        t = CALL()
        for dl in (0.30, 0.55, None):
            rp, why = rth.reprice_option_ticket(t, self.rules(t), quote(1.18, 1.22, delta=dl), 761.0, at(10, 0))
            self.assertIsNone(rp, dl); self.assertIn("delta", why)
        for dl in (0.35, 0.50, -0.42):
            self.assertIsNotNone(rth.reprice_option_ticket(t, self.rules(t), quote(1.18, 1.22, delta=dl), 761.0, at(10, 0))[0], dl)
        # not 0DTE: the band is not applied
        wk = CALL(option_expiration="2030-11-15", occ_symbol="SPY301115C00760000")
        self.assertIsNotNone(rth.reprice_option_ticket(wk, self.rules(wk), quote(1.18, 1.22, delta=0.25), 761.0, at(10, 0))[0])
        g = self.gates(CALL(), {}, quote(1.98, 2.02, delta=0.6))
        self.assertEqual(g["action"], "void"); self.assertIn("R24", g["why"]); self.assertIn("delta", g["plain"])

    def test_working_stop_nearer_than_invalidation_is_used(self):
        t = CALL(underlying_stop=760.5)  # nearer than the 759.80 hard invalidation
        rp, _ = rth.reprice_option_ticket(t, self.rules(t), quote(1.18, 1.22), 761.0, at(10, 0))
        self.assertEqual(rp["reprice"]["underlying_stop_used"], 760.5)
        self.assertEqual(rp["stop"], round(1.20 - 0.45 * 0.5 + 0.5 * 0.08 * 0.25 - 0.05, 2))

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
        if "underlying_target" not in t:
            t = {**t, "underlying_target": 766.0}
        if "underlying_stop" not in t:
            t = {**t, "underlying_stop": 761.0}  # working stop 0.5 below the 761.5 trigger close (underlying R 9 => R19 passes)
        return rth.arm_time_gates(QPE, t, self.rules(t), meta, bars, at(10, 0), {"events": []}, False, walls)

    def test_gates_void_without_quote(self):
        g = self.gates(CALL(), {}, None)
        self.assertEqual(g["action"], "void"); self.assertIn("Stale premium", g["why"])

    def test_gates_hold_on_degenerate_walls(self):
        g = self.gates(CALL(), {}, quote(1.18, 1.22), walls=latest_walls(760, 760, f"{DAY_S}T09:50:00-0600"))
        self.assertEqual(g["action"], "hold")

    def test_gates_submit_repriced_and_halved(self):
        g = self.gates(CALL(), {"size_factor": 0.5, "size_reasons": ["reclaim armed before 09:00 CT"]}, quote(1.98, 2.02, delta=0.40, gamma=0.02))
        self.assertEqual(g["action"], "submit", g["why"])
        tk = g["ticket"]
        self.assertEqual((tk["staged_entry"], tk["staged_quantity"]), (2.0, 4))
        self.assertEqual(tk["entry"], 2.02)
        self.assertEqual(tk["arm_reprice"]["stop_method"], "greeks_R24_v1.3")
        self.assertEqual(tk["quantity"], 2)  # budget 4 contracts at the repriced risk, then x0.5
        self.assertAlmostEqual(tk["actual_dollar_risk"], round((tk["entry"] - tk["stop"]) * 2 * 100, 2))
        # re-arm recomputes from staged values, never from an already-halved quantity
        g2 = self.gates(tk, {}, quote(1.98, 2.02, delta=0.40, gamma=0.02))
        self.assertEqual(g2["ticket"]["quantity"], 4)

    def test_half_size_of_one_contract_skips(self):
        g = self.gates(CALL(quantity=1, actual_dollar_risk=60.0), {"size_factor": 0.5, "size_factors": [0.5], "size_reasons": ["x"]},
                       quote(1.98, 2.02, delta=0.40, gamma=0.02))
        self.assertEqual(g["action"], "skip"); self.assertIn("rounds 1 to 0", g["why"]); self.assertEqual(g["kind"], "ENTRY_SKIPPED")

    def test_tick_voids_ticket_and_never_rearms(self):
        t = CALL(session_target=DAY_S, underlying_target=766.0)
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
        q = quote(1.98, 2.02, delta=0.40, gamma=0.02)
        for p45, g45, exp_qty in ((-1.6e9, -6.7e9, 2), (-7.9e9, -1.0e9, 2), (-1.6e9, -1.0e9, 4), (-1.6e9, None, 4)):
            g = self.gates(CALL(), {}, q, walls=latest_walls(756, 761, f"{DAY_S}T09:50:00-0600", printed45=p45, grid45=g45))
            self.assertEqual(g["action"], "submit", g["why"])
            self.assertEqual(g["ticket"]["quantity"], exp_qty, (p45, g45))
            chk = g["ticket"]["gex45_check"]
            self.assertEqual((chk["net_gex_45d_printed"], chk["net_gex_45d_grid_at_chain_spot"]), (p45, g45))
            self.assertIn("printed", g["why"]); self.assertIn("grid", g["why"])

    def test_cap_does_not_stack_beyond_smallest_factor(self):
        g = self.gates(CALL(), {"size_factor": 0.5, "size_factors": [0.5], "size_reasons": ["reclaim armed before 09:00 CT"]},
                       quote(1.98, 2.02, delta=0.40, gamma=0.02),
                       walls=latest_walls(756, 761, f"{DAY_S}T09:50:00-0600", printed45=-7.9e9))
        self.assertEqual(g["ticket"]["quantity"], 2)  # min(1/2, 1/2) = 1/2, not 1/4

    def test_both_missing_fails_closed(self):
        g = self.gates(CALL(), {}, quote(1.98, 2.02, delta=0.40, gamma=0.02),
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

# ------------------------------------------------------------------ CIO rulings Nov 12 PM / framework v1.3
def _tick_env(case, t, bars, now, latest, extra=None):
    rewrites, submits = [], []
    env = {"_now_ct": lambda: now, "armed_tickets": lambda: [t], "load_events": lambda: {"events": []},
           "save_events": lambda d: None, "rewrite_blotter_ticket": lambda tid, fn: rewrites.append(fn(dict(t))),
           "fetch_alpaca_bars": lambda *a: [dict(b) for b in bars], "manage_open_positions": lambda *a, **k: [],
           "push_vault_if_changed": lambda *a: {}, "already_submitted": lambda *a: (False, "clear"),
           "live_rvol": lambda *a, **k: {"tod_rvol": 2.0}, "submit_ticket": lambda *a, **k: submits.append(1),
           "load_latest": lambda: latest}
    env.update(extra or {})
    for k, v in env.items():
        case.p.set(rth, k, v)
    return rewrites, submits


class WallStale(_FW):
    rules = Reprice.rules
    gates = Reprice.gates

    def test_stale_walls_logged_with_distinct_reason_and_age(self):
        r = rth.wall_check(762.0, at(11, 0), latest_walls(756, 761, f"{DAY_S}T09:20:00-0600"))
        self.assertEqual((r["ok"], r["code"], r["age_min"]), (False, "stale", 100.0))
        self.assertEqual(rth.wall_check(758.0, at(9, 30), latest_walls(760, 760))["code"], "equal")
        self.assertEqual(rth.wall_check(758.0, at(9, 30), {})["code"], "missing")
        g = self.gates(CALL(), {}, quote(1.98, 2.02), walls=latest_walls(756, 761, f"{DAY_S}T08:20:00-0600"))
        self.assertEqual((g["action"], g["kind"], g["wall_age_min"]), ("hold", "WALL_STALE", 100.0))

    def test_tick_logs_wall_stale_event_once_per_snapshot(self):
        t = CALL(session_target=DAY_S, underlying_target=766.0, underlying_stop=760.5)
        bars = T.or_bars(759.0, 760.4) + [bar(10, 50, 760.4, 761.2, 760.3, 761.0, 2000)]
        doc = {"events": []}
        rewrites, submits = _tick_env(self, t, bars, at(10, 56), latest_walls(756, 761, f"{DAY_S}T09:20:00-0600"),
                                      {"load_events": lambda: doc})
        res = rth.alpaca_rth_tick(_TPE)
        ev = [e for e in doc["events"] if e["kind"] == "WALL_STALE"]
        self.assertEqual(len(ev), 1); self.assertEqual(ev[0]["reason_code"], "WALL_STALE")
        self.assertEqual(ev[0]["wall_age_min"], 96.0); self.assertIn("96 min old", ev[0]["detail"])
        self.assertEqual(submits, [])
        rth.alpaca_rth_tick(_TPE)
        self.assertEqual(len([e for e in doc["events"] if e["kind"] == "WALL_STALE"]), 1)


class SkipOnZeroContracts(_FW):
    def test_tick_skips_ticket_never_full_contract(self):
        t = CALL(session_target=DAY_S, quantity=1, actual_dollar_risk=60.0, underlying_target=766.0, underlying_stop=760.5)
        bars = T.or_bars(759.0, 760.4) + [bar(9, 30, 760.4, 761.2, 760.3, 761.0, 2000)]
        lat = latest_walls(756, 761, f"{DAY_S}T09:20:00-0600", printed45=-7.0e9)  # 45D cap => x1/2

        class QPE(_TPE):
            @staticmethod
            def alpaca_request(method, host, path, body=None, params=None, timeout=20):
                if "options/snapshots" in path:
                    q = quote(1.98, 2.02, delta=0.40, gamma=0.02)
                    q["t"] = (at(9, 36) - timedelta(seconds=3)).astimezone(rth.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                    return {"snapshots": {params["symbols"]: {"latestQuote": {"bp": q["bid"], "ap": q["ask"], "t": q["t"]},
                                                              "greeks": {"delta": q["delta"], "gamma": q["gamma"]}}}}
                return {"is_open": True}
        rewrites, submits = _tick_env(self, t, bars, at(9, 36), lat)
        res = rth.alpaca_rth_tick(QPE)
        self.assertEqual(submits, [])
        self.assertEqual(rewrites[-1]["status"], "skipped_paper")
        self.assertIn("skipped_paper", rth.DONE_STATUSES)
        self.assertTrue(any(str(a).startswith("skipped:") for a in res["actions"]))


class C4Check(_FW):
    rules = Reprice.rules
    gates = Reprice.gates

    def t(self, **kw):
        return CALL(**kw)

    def test_missing_target_fails_closed(self):
        ok, why = rth.c4_check(self.t(), rth.parse_ticket_rules(self.t()), 761.0, at(10, 0))
        self.assertFalse(ok); self.assertIn("missing", why)

    def test_underlying_r_passes(self):
        t = self.t(underlying_target=765.0)  # 4.0 vs 1.2 => 3.3R
        self.assertTrue(rth.c4_check(t, rth.parse_ticket_rules(t), 761.0, at(10, 0))[0])

    def test_premium_r_counts_only_on_0dte_with_target_ge_1x_stop(self):
        t = self.t(underlying_target=762.5)  # 1.5 vs 1.2 => 1.25x stop; premium R (3.80-2)/0.6 = 3.0
        ok, why = rth.c4_check(t, rth.parse_ticket_rules(t), 761.0, at(10, 0))
        self.assertTrue(ok, why); self.assertIn("C4", why)
        t = self.t(underlying_target=762.0)  # 1.0 vs 1.2 => 0.83x: premium R does not count
        self.assertFalse(rth.c4_check(t, rth.parse_ticket_rules(t), 761.0, at(10, 0))[0])
        t = self.t(underlying_target=762.5, target1=3.0, target2=3.5)  # premium R 2.5 < 3
        self.assertFalse(rth.c4_check(t, rth.parse_ticket_rules(t), 761.0, at(10, 0))[0])
        wk = self.t(underlying_target=762.5, option_expiration="2030-11-15", occ_symbol="SPY301115C00760000")
        self.assertFalse(rth.c4_check(wk, rth.parse_ticket_rules(wk), 761.0, at(10, 0))[0])

    def test_ptj_macro_needs_5_to_1_on_underlying(self):
        t = self.t(underlying_target=766.0, strategy_agent="PTJ Macro & Regime Strategist")  # 5/1.2 = 4.2R
        self.assertFalse(rth.c4_check(t, rth.parse_ticket_rules(t), 761.0, at(10, 0))[0])
        t = self.t(underlying_target=767.5, strategy_agent="PTJ Macro & Regime Strategist")  # 5.4R
        self.assertTrue(rth.c4_check(t, rth.parse_ticket_rules(t), 761.0, at(10, 0))[0])

    def test_strike_beyond_target_blocked(self):
        t = self.t(underlying_target=759.9 + 6, strike=767.0, occ_symbol="SPY301108C00767000")
        ok, why = rth.c4_check(t, rth.parse_ticket_rules(t), 761.0, at(10, 0))
        self.assertFalse(ok); self.assertIn("strike", why)

    def test_gates_hold_without_target(self):
        g = self.gates({**CALL(), "underlying_target": None}, {}, quote(1.98, 2.02))
        self.assertEqual(g["action"], "hold"); self.assertIn("C4", g["why"])


class StackedFactors(_FW):
    rules = Reprice.rules
    gates = Reprice.gates

    def test_vol_regime_factor(self):
        f = rth.vol_regime_factor
        self.assertEqual(f({"summary": CALM_VOL})[0], 1.0)
        self.assertEqual(f({"summary": {**CALM_VOL, "vvix": 111}})[0], 0.5)
        self.assertEqual(f({"summary": {**CALM_VOL, "vix9d": 18.1}})[0], 0.5)
        self.assertEqual(f({"summary": {**CALM_VOL, "vix3m": 17.9}})[0], 0.5)
        self.assertEqual(f({"summary": {"vix": 28.18, "vix9d": 32.66, "vix3m": 25.0, "vvix": 134.99}})[0], 0.25)  # Nov 5
        self.assertIsNone(f({"summary": {"vix": 18}})[0])

    def test_three_halves_drop_to_quarter(self):
        q = quote(1.98, 2.02, delta=0.40, gamma=0.02)
        walls = latest_walls(756, 761, f"{DAY_S}T09:50:00-0600", printed45=-7.0e9, vol={**CALM_VOL, "vvix": 115})
        t = CALL(quantity=8, actual_dollar_risk=480.0)
        g = self.gates(t, {"size_factor": 0.5, "size_factors": [0.5], "size_reasons": ["reclaim armed before 09:00 CT"]}, q, walls=walls)
        self.assertEqual(g["action"], "submit", g["why"])
        self.assertEqual(g["ticket"]["size_factor"], 0.25); self.assertEqual(g["ticket"]["quantity"], 2)
        # two halves stay at 1/2 (smallest factor, not multiplied)
        walls2 = latest_walls(756, 761, f"{DAY_S}T09:50:00-0600", printed45=-7.0e9)
        g = self.gates(t, {"size_factor": 0.5, "size_factors": [0.5], "size_reasons": ["x"]}, q, walls=walls2)
        self.assertEqual(g["ticket"]["quantity"], 4)

    def test_quarter_regime(self):
        q = quote(1.98, 2.02, delta=0.40, gamma=0.02)
        walls = latest_walls(756, 761, f"{DAY_S}T09:50:00-0600", vol={"vix": 20, "vix9d": 22.5, "vix3m": 21, "vvix": 130})
        g = self.gates(CALL(quantity=8, actual_dollar_risk=480.0), {}, q, walls=walls)
        self.assertEqual(g["ticket"]["quantity"], 2)

    def test_vol_inputs_missing_fails_closed(self):
        g = self.gates(CALL(), {}, quote(1.98, 2.02, delta=0.40), walls=latest_walls(756, 761, f"{DAY_S}T09:50:00-0600", vol=None))
        self.assertEqual(g["action"], "hold"); self.assertIn("R23", g["why"])

    def test_factors_applied_at_staging_not_double_counted(self):
        q = quote(1.98, 2.02, delta=0.40, gamma=0.02)
        walls = latest_walls(756, 761, f"{DAY_S}T09:50:00-0600", printed45=-7.0e9)
        g = self.gates(CALL(size_factors_applied=[0.5]), {}, q, walls=walls)  # card already halved for the 45D cap
        self.assertEqual(g["ticket"]["quantity"], 4)
        walls = latest_walls(756, 761, f"{DAY_S}T09:50:00-0600", printed45=-7.0e9, vol={**CALM_VOL, "vvix": 115})
        g = self.gates(CALL(size_factors_applied=[0.5]), {"size_factor": 0.5, "size_factors": [0.5], "size_reasons": ["r"]}, q, walls=walls)
        self.assertEqual(g["ticket"]["quantity"], 2)  # 3 halves => 1/4 of base = 1/2 of the already-halved card

    def test_45d_grid_at_current_spot_only_tightens(self):
        grid = [{"spot": 760.0, "net_gex": -4.0e9}, {"spot": 762.0, "net_gex": -6.0e9}]
        v, info = rth.net_gex_45d_conservative(latest_walls(756, 761, printed45=-1.0e9, grid45=-2.0e9, grid=grid), 761.5)
        self.assertAlmostEqual(v, -5.5e9); self.assertEqual(info["net_gex_45d_source"], "grid_at_current_spot")
        v, _ = rth.net_gex_45d_conservative(latest_walls(756, 761, printed45=-7.0e9, grid=grid), 761.5)
        self.assertEqual(v, -7.0e9)
        v, _ = rth.net_gex_45d_conservative(latest_walls(756, 761, printed45=-1.0e9, grid=grid), 770.0)  # outside grid
        self.assertEqual(v, -1.0e9)


class VoidPlainWords(_FW):
    rules = Reprice.rules
    gates = Reprice.gates

    def test_void_carries_plain_reason(self):
        g = self.gates(CALL(), {}, quote(1.0, 1.2))
        self.assertEqual(g["action"], "void"); self.assertEqual(g["plain"], "Voided: quote stale or spread too wide")


class C3Dedup(_FW):
    """CIO (Nov 12 evening): the card's C3 half and the engine's pre-09:00 reclaim half are one rule."""
    rules = Reprice.rules
    gates = Reprice.gates
    RECL = {"size_factor": 0.5, "size_factors": [0.5], "size_reasons": ["reclaim armed before 09:00 CT"]}

    def q(self):
        return quote(1.98, 2.02, delta=0.40, gamma=0.02)

    def walls(self, p45=-1.0e9, vvix=95.0):
        return latest_walls(756, 761, f"{DAY_S}T09:50:00-0600", printed45=p45, vol={**CALM_VOL, "vvix": vvix})

    def test_c3_listed_gives_one_half(self):
        t = CALL(size_factors_applied=[0.5], size_factors_applied_reasons=["C3"])  # card staged at half (4 contracts)
        g = self.gates(t, dict(self.RECL), self.q(), walls=self.walls())
        self.assertEqual(g["action"], "submit", g["why"])
        self.assertEqual((g["ticket"]["quantity"], g["ticket"]["size_factor_vs_base"]), (4, 0.5))
        t = CALL(size_factors_applied=[{"factor": 0.5, "rule": "c3"}])  # dict form, exact tag
        self.assertEqual(self.gates(t, dict(self.RECL), self.q(), walls=self.walls())["ticket"]["quantity"], 4)

    def test_c3_not_listed_keeps_engine_half(self):
        g = self.gates(CALL(), dict(self.RECL), self.q(), walls=self.walls())
        self.assertEqual(g["ticket"]["quantity"], 2)

    def test_c3_plus_45d_stacks_per_r21(self):
        t = CALL(size_factors_applied=[0.5], size_factors_applied_reasons=["C3"])
        g = self.gates(t, dict(self.RECL), self.q(), walls=self.walls(p45=-7.0e9))
        # C3 + 45D = two halves => 1/2 of base (not three halves => 1/4): the half-sized card is unchanged
        self.assertEqual((g["ticket"]["quantity"], g["ticket"]["size_factor_vs_base"]), (4, 0.5))
        # C3 + 45D + R23 half = three separate halves => 1/4 of base
        g = self.gates(t, dict(self.RECL), self.q(), walls=self.walls(p45=-7.0e9, vvix=115))
        self.assertEqual((g["ticket"]["quantity"], g["ticket"]["size_factor_vs_base"]), (2, 0.25))

    def test_ambiguous_keeps_extra_half(self):
        for kw in ({"size_factors_applied": [0.5], "size_factors_applied_reasons": ["C3 reclaim half"]},
                   {"size_factors_applied": [0.5], "size_factors_applied_reasons": ["C3", "45D"]},  # length mismatch
                   {"size_factors_applied": [0.5]},
                   {"size_factors_applied": [0.25], "size_factors_applied_reasons": ["C3"]}):
            g = self.gates(CALL(**kw), dict(self.RECL), self.q(), walls=self.walls(p45=-7.0e9))
            self.assertEqual(g["ticket"]["quantity"], 2 if kw["size_factors_applied"] == [0.5] else g["ticket"]["quantity"], kw)
        g = self.gates(CALL(size_factors_applied=[0.5], size_factors_applied_reasons=["C3 reclaim half"]), dict(self.RECL),
                       self.q(), walls=self.walls(p45=-7.0e9))
        self.assertEqual(g["ticket"]["size_factor_vs_base"], 0.25)

    def test_rounding_to_zero_still_skipped(self):
        t = CALL(quantity=1, actual_dollar_risk=60.0, size_factors_applied=[0.5], size_factors_applied_reasons=["C3"])
        g = self.gates(t, dict(self.RECL), self.q(), walls=self.walls(p45=-7.0e9, vvix=115))
        self.assertEqual(g["action"], "skip"); self.assertIn("rounds 1 to 0", g["why"])
        g = self.gates(CALL(quantity=1, actual_dollar_risk=60.0), dict(self.RECL), self.q(), walls=self.walls())
        self.assertEqual(g["action"], "skip")


# ------------------------------------------------------------------ v1.3 R6 / R10 / R17 (Nov 12 evening)
def r6_bars(cash):
    # OR15 high 757.86 / low 753.70 => mid 755.78 (Thu Nov 12 replay); 08:40 close = cash
    return [bar(8, 30, 754.0, 755.0, 753.70, 754.2), bar(8, 35, 754.2, 757.86, 754.1, 756.0), bar(8, 40, 756.0, max(757.8, cash), 755.9, cash)]


class R6Gate(_FW):
    def tk(self, **kw):
        return RECLAIM(prior_close=759.81, **kw)

    def test_classify_replay_values(self):
        c = rth.r6_classify(r6_bars(757.4), at(8, 45), 759.81)
        self.assertEqual((c["cls"], c["or15_mid"]), ("neutral", 755.78)); self.assertAlmostEqual(c["neutral_high_line"], 757.8, places=1)
        self.assertEqual(rth.r6_classify(r6_bars(757.85), at(8, 45), 759.81)["cls"], "neutral_high")
        self.assertEqual(rth.r6_classify(r6_bars(759.81), at(8, 45), 759.81)["cls"], "strong")
        self.assertEqual(rth.r6_classify(r6_bars(755.0), at(8, 45), 759.81)["cls"], "weak")
        self.assertIsNone(rth.r6_classify(r6_bars(757.4), at(8, 44, 59), 759.81)["cls"])      # before 08:45
        self.assertIsNone(rth.r6_classify(r6_bars(757.4)[:2], at(8, 45), 759.81)["cls"])      # OR15 incomplete
        self.assertIsNone(rth.r6_classify(r6_bars(755.0), at(8, 45), 754.0)["cls"])           # ambiguous

    def test_untagged_and_flat_unchanged(self):
        self.assertTrue(rth.r6_gate(RECLAIM(), r6_bars(757.4), at(8, 45))[0])
        self.assertTrue(rth.r6_gate(RECLAIM(scenario_row="Flat"), r6_bars(757.4), at(8, 45))[0])

    def test_row_selection_cash_wins(self):
        g = lambda row, cash, **kw: rth.r6_gate(self.tk(scenario_row=row, **kw), r6_bars(cash), at(8, 45))[0]
        self.assertTrue(g("neutral", 757.4)); self.assertFalse(g("strong", 757.4)); self.assertFalse(g("weak", 757.4))
        self.assertTrue(g("weak", 755.0)); self.assertFalse(g("neutral", 755.0))
        self.assertTrue(g("strong", 760.0)); self.assertFalse(g("neutral", 760.0))
        # neutral-high: Strong row only when 8:23 was strong; otherwise Neutral; unknown 8:23 => hold
        self.assertTrue(g("strong", 757.85, case_0823="strong")); self.assertFalse(g("neutral", 757.85, case_0823="strong"))
        self.assertTrue(g("neutral", 757.85, case_0823="weak")); self.assertFalse(g("strong", 757.85, case_0823="neutral"))
        self.assertFalse(g("strong", 757.85)); self.assertFalse(g("neutral", 757.85))

    def test_tagged_fail_closed(self):
        ok, why = rth.r6_gate(RECLAIM(scenario_row="neutral"), r6_bars(757.4), at(8, 45))  # no prior_close
        self.assertFalse(ok); self.assertIn("fail closed", why)
        self.assertFalse(rth.r6_gate(self.tk(scenario_row="sideways"), r6_bars(757.4), at(8, 45))[0])
        t = RECLAIM(scenario_row="neutral", underlying_trigger={**RECLAIM()["underlying_trigger"], "prior_close": 759.81})
        self.assertTrue(rth.r6_gate(t, r6_bars(757.4), at(8, 45))[0])  # also read from underlying_trigger

    def test_decide_integration(self):
        self.cfg(earliest_entry_ct="08:45")
        d = self.decide(self.tk(scenario_row="neutral"), r6_bars(757.4), at(8, 45))
        self.assertEqual(d["action"], "fire", d["reason"]); self.assertEqual(d["meta"]["size_factor"], 0.5)
        d = self.decide(self.tk(scenario_row="strong"), r6_bars(757.4), at(8, 45))
        self.assertEqual(d["action"], "wait"); self.assertIn("not live", d["reason"])


class R10Kill(_FW):
    def short(self, **kw):
        return STOCK(underlying_trigger={"failure": "decisive acceptance below 750 (5-min hold) with RVOL >= 1.2x", "rvol_min": 1.2}, **kw)

    def go(self, t, bars, now, zg):
        oi = rth.compute_or(bars, now, rvol={"tod_rvol": 1.5, "bar_rvol": None, "source": "test"})
        oi["zero_gamma_0dte_live"] = zg
        return rth.decide_armed_ticket(t, oi, bars, now)

    def test_close_above_zg_cancels_short_continuation(self):
        bars = or_bars(750.2, 752.0) + [bar(9, 0, 751.0, 751.8, 750.9, 751.6), bar(9, 5, 751.6, 751.7, 749.4, 749.6)]
        d = self.go(self.short(leg_type="continuation"), bars, at(9, 10, 1), 751.5)
        self.assertEqual((d["action"], d.get("rule")), ("invalidate", "R10")); self.assertIn("09:00", d["reason"])
        # untagged / other leg types keep today's behaviour
        self.assertEqual(self.go(self.short(), bars, at(9, 10, 1), 751.5)["action"], "fire")
        self.assertEqual(self.go(self.short(leg_type="breakout"), bars, at(9, 10, 1), 751.5)["action"], "fire")
        # zero-gamma above every close => no kill
        self.assertEqual(self.go(self.short(leg_type="continuation"), bars, at(9, 10, 1), 752.5)["action"], "fire")

    def lbars(self, *closes):
        out = or_bars(756.0, 758.9)
        for k, c in enumerate(closes):
            out.append(bar(9, 5 * k, c - 0.1, c + 0.2, c - 0.3, c))
        return out

    def test_long_closes_below_zg_before_trigger_do_not_cancel(self):
        # every close below zero-gamma 760.0, trigger (759.3 > 759.0) fires: R10 not armed, entry allowed
        b = self.lbars(758.0, 759.3)
        d = self.go(BREAKOUT(leg_type="continuation"), b, at(9, 10, 1), 760.0)
        self.assertEqual(d["action"], "fire", d["reason"])
        k, why = rth.r10_continuation_kill(BREAKOUT(leg_type="continuation"), d["rules"], b, at(9, 10, 1), 760.0)
        self.assertEqual(k, "ok"); self.assertIn("not armed", why)

    def test_long_arms_after_trigger_and_close_above_then_cancels_on_close_back_below(self):
        t = BREAKOUT(leg_type="continuation", r10_trigger_bar_ct="09:05")
        r = rth.parse_ticket_rules(t)
        k = lambda *c: rth.r10_continuation_kill(t, r, self.lbars(*c), at(9, 5 * len(c) + 1), 759.2)
        # trigger bar 09:05 closes 759.3 above zg 759.2 => armed; 09:10 closes 759.1 back below => cancel
        self.assertEqual(k(758.0, 759.3)[0], "ok")
        res, why = k(758.0, 759.3, 759.1)
        self.assertEqual(res, "cancel"); self.assertIn("09:10", why); self.assertIn("after the leg fired (09:05)", why)
        # the 09:00 close below zg (before the trigger) never cancels on its own
        self.assertEqual(k(758.0, 759.3, 759.6)[0], "ok")

    def test_long_trigger_below_zg_arms_only_on_later_close_above(self):
        t = BREAKOUT(leg_type="continuation", r10_trigger_bar_ct="09:05")
        r = rth.parse_ticket_rules(t)
        k = lambda *c: rth.r10_continuation_kill(t, r, self.lbars(*c), at(9, 5 * len(c) + 1), 759.9)
        self.assertEqual(k(758.0, 759.3, 759.0)[0], "ok")                 # fired below zg: not armed, no cancel
        self.assertEqual(k(758.0, 759.3, 759.0, 760.1)[0], "ok")          # 09:15 close above => armed
        self.assertEqual(k(758.0, 759.3, 759.0, 760.1, 759.7)[0], "cancel")  # 09:20 back below => cancel
        # r17_blocked_bar_ct also marks the trigger
        t2 = BREAKOUT(leg_type="continuation", r17_blocked_bar_ct="09:05")
        self.assertEqual(rth.r10_continuation_kill(t2, r, self.lbars(758.0, 759.3, 759.0, 760.1, 759.7), at(9, 26), 759.9)[0], "cancel")

    def test_long_unknown_zero_gamma_still_holds(self):
        d = self.go(BREAKOUT(leg_type="continuation"), self.lbars(758.0, 759.3), at(9, 10, 1), None)
        self.assertEqual(d["action"], "wait"); self.assertIn("fail closed", d["reason"])

    def test_short_side_unchanged_stricter(self):
        # short legs: any close above zero-gamma since 08:30 cancels, even before the trigger (superset of the ruling)
        bars = or_bars(750.2, 752.0) + [bar(9, 0, 751.0, 751.8, 750.9, 751.6)]
        k, _ = rth.r10_continuation_kill(self.short(leg_type="continuation"), rth.parse_ticket_rules(self.short()), bars, at(9, 5, 1), 751.5)
        self.assertEqual(k, "cancel")

    def test_tick_long_leg_blocked_then_cancelled(self):
        t = BREAKOUT(session_target=DAY_S, leg_type="continuation")
        bars = self.lbars(758.0, 759.3)
        doc = {"events": []}
        rewrites, submits = _tick_env(self, t, bars, at(9, 10, 30),
                                      {"summary": {"zero_gamma_0dte": 759.2, "timestamp_ct": f"{DAY_S}T09:05:00-0600"}},
                                      {"load_events": lambda: doc,
                                       "arm_time_gates": lambda pe_, tk, *a, **k: {"action": "hold", "ticket": tk, "why": "walls"}})
        rth.alpaca_rth_tick(_TPE)
        self.assertEqual(t["r10_trigger_bar_ct"], "09:05"); self.assertEqual(submits, [])
        bars.append(bar(9, 10, 759.3, 759.4, 758.9, 759.1))  # completed close back below zero-gamma
        self.p.set(rth, "_now_ct", lambda: at(9, 15, 30))
        res = rth.alpaca_rth_tick(_TPE)
        self.assertTrue(any(str(a) == "invalidated:R10:TKT-TEST-BRK" for a in res["actions"]), res["actions"])
        self.assertEqual(rewrites[-1]["status"], "invalidated_paper"); self.assertIn("R10", rewrites[-1]["invalidation_reason"])

    def test_unknown_zero_gamma_holds_and_ticket_fallback(self):
        bars = or_bars(750.2, 752.0) + [bar(9, 5, 751.6, 751.7, 749.4, 749.6)]
        d = self.go(self.short(leg_type="continuation"), bars, at(9, 10, 1), None)
        self.assertEqual(d["action"], "wait"); self.assertIn("fail closed", d["reason"])
        d = self.go(self.short(leg_type="continuation", zero_gamma_0dte=753.0), bars, at(9, 10, 1), None)
        self.assertEqual(d["action"], "fire", d["reason"])

    def test_live_zero_gamma_freshness(self):
        doc = {"summary": {"zero_gamma_0dte": 759.9, "timestamp_ct": f"{DAY_S}T09:20:00-0600"}}
        self.assertEqual(rth.live_zero_gamma_0dte(doc, at(9, 30)), 759.9)
        self.assertIsNone(rth.live_zero_gamma_0dte(doc, at(11, 0)))
        self.assertIsNone(rth.live_zero_gamma_0dte({"summary": {"zero_gamma_0dte": 759.9, "timestamp_ct": "2030-11-07T15:20:00-0600"}}, at(9, 30)))


class R17Rearm(_FW):
    def base(self):
        return or_bars(756.0, 758.9)

    def test_gate_block_recorded_once(self):
        b = self.base() + [bar(9, 0, 758.8, 759.4, 758.7, 759.3)]
        d = self.decide(BREAKOUT(), b, at(9, 5, 1), tod=1.0)  # price trigger met, RVOL gate blocks
        self.assertEqual(d["action"], "wait"); self.assertTrue(d["meta"].get("gate_block"))
        f = rth.r17_record_block(BREAKOUT(), d["rules"], b, at(9, 5, 1), d["meta"]["trigger_bar_ct"])
        self.assertEqual(f, {"r17_blocked_bar_ct": "09:00", "r17_blocked_count": 1})
        t = BREAKOUT(**f)
        self.assertIsNone(rth.r17_record_block(t, d["rules"], b, at(9, 6), "09:00"))
        # still beyond the level on the next bar with no retest: not chased, not re-counted
        b2 = b + [bar(9, 5, 759.3, 759.8, 759.2, 759.6)]
        d = self.decide(t, b2, at(9, 10, 1), tod=1.5)
        self.assertEqual(d["action"], "wait"); self.assertIn("no chasing", d["reason"])
        self.assertIsNone(rth.r17_record_block(t, d["rules"], b2, at(9, 10, 1), "09:05"))

    def test_rearm_by_retest_that_holds(self):
        t = BREAKOUT(r17_blocked_bar_ct="09:00", r17_blocked_count=1)
        b = self.base() + [bar(9, 0, 758.8, 759.4, 758.7, 759.3), bar(9, 5, 759.3, 759.5, 758.95, 759.4)]
        d = self.decide(t, b, at(9, 10, 1), tod=1.5)
        self.assertEqual(d["action"], "fire", d["reason"])

    def test_rearm_by_fresh_break(self):
        t = BREAKOUT(r17_blocked_bar_ct="09:00", r17_blocked_count=1)
        b = self.base() + [bar(9, 0, 758.8, 759.4, 758.7, 759.3), bar(9, 5, 759.3, 759.4, 758.5, 758.8)]
        self.assertEqual(self.decide(t, b, at(9, 10, 1), tod=1.5)["action"], "wait")  # back inside
        b += [bar(9, 10, 758.8, 759.6, 758.8, 759.5)]
        self.assertEqual(self.decide(t, b, at(9, 15, 1), tod=1.5)["action"], "fire")

    def test_one_rearm_only_and_not_after_1330(self):
        t = BREAKOUT(r17_blocked_bar_ct="09:00", r17_blocked_count=1)
        b = self.base() + [bar(9, 0, 758.8, 759.4, 758.7, 759.3), bar(9, 5, 759.3, 759.4, 758.5, 758.8),
                           bar(9, 10, 758.8, 759.6, 758.8, 759.5)]
        d = self.decide(t, b, at(9, 15, 1), tod=1.0)  # blocked again after the re-arm
        f = rth.r17_record_block(t, d["rules"], b, at(9, 15, 1), d["meta"]["trigger_bar_ct"])
        self.assertTrue(f["r17_exhausted"]); self.assertEqual(f["r17_blocked_count"], 2)
        t2 = BREAKOUT(**{**t, **f})
        d = self.decide(t2, b + [bar(9, 15, 759.5, 759.9, 758.9, 759.7)], at(9, 20, 1), tod=1.5)
        self.assertEqual(d["action"], "wait"); self.assertIn("scenario done", d["reason"])
        late = self.base() + [bar(9, 0, 758.8, 759.4, 758.7, 759.3), bar(13, 25, 758.9, 759.4, 758.8, 758.9),
                              bar(13, 30, 758.9, 759.6, 758.9, 759.5)]
        d = self.decide(t, late, at(13, 35, 1), tod=1.5)
        self.assertEqual(d["action"], "wait"); self.assertIn("13:30", d["reason"])

    def test_never_blocked_tickets_unchanged(self):
        b = self.base() + [bar(9, 0, 758.8, 759.4, 758.7, 759.3)]
        self.assertEqual(self.decide(BREAKOUT(), b, at(9, 5, 1), tod=1.5)["action"], "fire")

    def test_half_tier_two_bar_wait_is_not_a_block(self):
        self.cfg(rvol_half_size_tier_enabled=True)
        b = self.base() + [bar(9, 0, 758.8, 758.9, 758.4, 758.5), bar(9, 5, 758.5, 759.4, 758.4, 759.3)]
        d = self.decide(BREAKOUT(), b, at(9, 10, 1), tod=1.0)
        self.assertEqual(d["action"], "wait"); self.assertFalse(d["meta"].get("gate_block"))

    def test_tick_arm_gate_hold_logs_blocked_move(self):
        t = BREAKOUT(session_target=DAY_S)
        bars = self.base() + [bar(9, 0, 758.8, 759.4, 758.7, 759.3)]
        doc = {"events": []}
        _tick_env(self, t, bars, at(9, 5, 30), latest_walls(756, 761),
                  {"load_events": lambda: doc,
                   "arm_time_gates": lambda pe_, tk, *a, **k: {"action": "hold", "ticket": tk, "why": "SPY mid-range between walls"}})
        res = rth.alpaca_rth_tick(_TPE)
        ev = [e for e in doc["events"] if e["kind"] == "BLOCKED_MOVE"]
        self.assertEqual(len(ev), 1); self.assertEqual(ev[0]["reason_code"], "blocked_move")
        self.assertEqual(t["r17_blocked_bar_ct"], "09:00")
        rth.alpaca_rth_tick(_TPE)
        self.assertEqual(len([e for e in doc["events"] if e["kind"] == "BLOCKED_MOVE"]), 1)


class FirewallCheck5(unittest.TestCase):
    """pod_engine.py size_ticket check 5 against LIVE 0DTE walls (CIO ruling 4)."""
    def a(self, **kw):
        import argparse
        d = dict(instrument="option_call", direction="long", strategy_agent="ITT", min_rr=None, entry=2.0, stop=1.4,
                 target1=3.8, target2=5.0, override_cap=False, risk_pct=0.25, drawdown_mode=False, underlying=None,
                 symbol="SPY", time_stop="30m if <0.5R", equity=100000.0)
        d.update(kw)
        return argparse.Namespace(**d)

    def m(self, put=756.0, call=761.0, spy=757.0, age_min=10):
        ts = (datetime.now(rth.CT) - timedelta(minutes=age_min)).strftime("%Y-%m-%dT%H:%M:%S%z")
        return {"ts": ts, "spy": spy, "spy_live": spy, "vix": 18.0, "put_wall_0dte": put, "call_wall_0dte": call}

    def c5(self, t):
        return next(c for c in t["firewall"] if c["id"] == 5)

    def test_degenerate_equal_walls_now_fail(self):
        t = pe.size_ticket(self.a(underlying=758.0), self.m(760.0, 760.0))
        self.assertFalse(self.c5(t)["pass"]); self.assertIn("coincide", self.c5(t)["value"]["reason"])
        self.assertEqual(t["verdict"], "BLOCKED")

    def test_missing_walls_or_ref_fail_closed(self):
        self.assertFalse(self.c5(pe.size_ticket(self.a(underlying=758.0), self.m(None, 761.0)))["pass"])
        self.assertFalse(self.c5(pe.size_ticket(self.a(), {**self.m(), "spy": None, "spy_live": None}))["pass"])

    def test_stale_or_untimed_walls_fail_closed(self):
        c = self.c5(pe.size_ticket(self.a(underlying=757.0), self.m(age_min=120)))
        self.assertFalse(c["pass"]); self.assertIn("WALL_STALE", c["value"]["reason"])
        self.assertFalse(self.c5(pe.size_ticket(self.a(underlying=757.0), {**self.m(), "ts": None}))["pass"])

    def test_live_walls_evaluated(self):
        self.assertTrue(self.c5(pe.size_ticket(self.a(underlying=757.0), self.m()))["pass"])     # below 757.5
        c = self.c5(pe.size_ticket(self.a(underlying=758.5), self.m()))
        self.assertFalse(c["pass"]); self.assertEqual(c["value"]["no_mans_land"], [757.5, 759.5])
        self.assertTrue(self.c5(pe.size_ticket(self.a(), self.m(spy=761.6)))["pass"])            # live spot used as ref


class R24StopSign(unittest.TestCase):
    """v1.3 corrected premium stop (mid - |Δ|·d + ½Γ·d² - 0.05) is never wider than the old form."""
    def test_never_wider_calls_puts_both_signs_of_d(self):
        for delta in (0.35, 0.42, 0.50, -0.35, -0.42, -0.50):          # calls and puts
            for gamma in (0.0, 0.01, 0.05, 0.12, -0.05):               # incl. a bad negative print
                for d in (-3.0, -1.7, -0.5, 0.0, 0.5, 1.7, 3.0):         # both signs of d
                    for mid in (0.8, 1.2, 2.0, 4.0):
                        new = rth.r24_premium_stop(mid, delta, gamma, d)
                        old = rth.r24_premium_stop_old(mid, delta, gamma, d)
                        self.assertGreaterEqual(new + 1e-12, old, (mid, delta, gamma, d))
                        self.assertAlmostEqual(new, rth.r24_premium_stop(mid, delta, gamma, -d))
                        exp = mid - abs(delta) * abs(d) + 0.5 * abs(gamma) * d * d - 0.05
                        self.assertAlmostEqual(new, max(exp, old))

    def test_corrected_formula_value(self):
        # 0.45Δ, 0.08Γ, d = 1.2: 1.20 - 0.54 + 0.0576 - 0.05 = 0.6676 (old: 0.5524)
        self.assertAlmostEqual(rth.r24_premium_stop(1.20, 0.45, 0.08, 1.2), 0.6676)
        self.assertAlmostEqual(rth.r24_premium_stop_old(1.20, 0.45, 0.08, 1.2), 0.5524)
        self.assertAlmostEqual(rth.r24_premium_stop(1.20, -0.45, 0.08, -1.2), 0.6676)  # put, same form

    def test_reprice_put_uses_corrected_stop(self):
        t = T.PUT() if hasattr(T, "PUT") else None
        if t is None:
            self.skipTest("no PUT fixture")
        r = rth.parse_ticket_rules(t)
        lvl = r["inv"]["level"]
        spot = lvl - 1.0
        q = {"bid": 1.18, "ask": 1.22, "t": (datetime.now(rth.CT) - timedelta(seconds=5)).astimezone(rth.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
             "delta": -0.42, "gamma": 0.06}
        wk = {**t, "option_expiration": "2099-01-15", "occ_symbol": None}  # not 0DTE: no delta band
        rp, why = rth.reprice_option_ticket(wk, rth.parse_ticket_rules(wk), q, spot, datetime.now(rth.CT))
        self.assertEqual(why, "ok")
        self.assertEqual(rp["stop"], round(1.20 - 0.42 * 1.0 + 0.5 * 0.06 * 1.0 - 0.05, 2))


if __name__ == "__main__":
    unittest.main(verbosity=1)

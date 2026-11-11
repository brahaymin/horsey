#!/usr/bin/env python3
"""Option paper-execution path tests (Nov 11 2026). SYNTHETIC data only: fake tickets,
fake bars, and a mocked Alpaca client that records calls. No network, no secrets.

Env overrides for pre-install testing: RTH_MODULE, POD_ENGINE_MODULE, GOV_MODULE.
Run: python3 scripts/test_option_path.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
_TMP = Path(tempfile.mkdtemp(prefix="opt_path_test_"))
os.environ["HORSEY_ROOT"] = str(_TMP)  # every module writes under a throwaway root
(_TMP / "pod" / "ledger").mkdir(parents=True)
(_TMP / "pod" / "risk").mkdir(parents=True)


def _load(env, default, name):
    p = os.environ.get(env) or str(HERE / default)
    sp = importlib.util.spec_from_file_location(name, p)
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


rth = _load("RTH_MODULE", "_alpaca_rth.py", "_rth_opt_test")
pe = _load("POD_ENGINE_MODULE", "pod_engine.py", "_pe_opt_test")
gov = _load("GOV_MODULE", "_pod_governance.py", "_gov_opt_test")
CT = rth.CT
PAPER = "https://paper-api.alpaca.markets"
DAY = (2030, 11, 8)  # synthetic session (a Friday)
EXP0 = "2030-11-08"
OCC_C = "SPY301108C00760000"


def at(h, m, s=0):
    return datetime(*DAY, h, m, s, tzinfo=CT)


def bar(h, m, o, hi, lo, c, v=1000.0):
    dt = datetime(*DAY, h, m, tzinfo=CT)
    return {"t": dt.isoformat(), "o": o, "h": hi, "l": lo, "c": c, "v": v, "_dt": dt}


def or_bars(lo, hi):
    mid = (lo + hi) / 2
    return [bar(8, 30 + 5 * i, mid, hi if i == 1 else mid + 0.2, lo if i == 3 else mid - 0.2, mid) for i in range(6)]


FW = [{"id": i, "name": f"check {i}", "pass": True} for i in range(1, 7)]


def CALL(**kw):
    t = {
        "ticket_id": "TKT-TEST-OPTC", "status": "conditional_preapproved", "instrument": "option_call",
        "direction": "long", "symbol": "SPY", "occ_symbol": OCC_C, "option_expiration": EXP0, "strike": 760.0,
        "quantity": 4, "multiplier": 100, "entry": 2.00, "stop": 1.40, "target1": 3.80, "target2": 5.00,
        "underlying_trigger": {"breakout": "acceptance above 760.50 (5-min hold) with RVOL >= 1.2x", "rvol_min": 1.2},
        "hard_invalidation_underlying": "5-15 minute reclaim/hold back below 759.80 / 758.00",
        "time_stop": "45m if <0.5R progress; no new risk after 14:30 CT; flat 0DTE by 14:45 CT",
        "no_entry_before_ct": "09:00", "session_target": EXP0, "firewall": FW, "verdict": "APPROVED_FOR_STAGING",
    }
    t.update(kw)
    return t


def PUT(**kw):
    t = CALL(ticket_id="TKT-TEST-OPTP", instrument="option_put", occ_symbol="SPY301108P00755000", strike=755.0,
             underlying_trigger={"failure": "decisive acceptance below 755.00 (5-min hold) with RVOL >= 1.2x", "rvol_min": 1.2},
             hard_invalidation_underlying="sustained reclaim above 756.20 / 758.00")
    t.update(kw)
    return t


def STOCK(**kw):
    t = {"ticket_id": "TKT-TEST-STK", "status": "conditional_preapproved", "instrument": "stock", "direction": "short",
         "symbol": "SPY", "quantity": 125, "entry": 749.5, "stop": 751.0, "target1": 745.0, "target2": 741.0,
         "firewall": FW, "verdict": "APPROVED_FOR_STAGING",
         "underlying_trigger": {"failure": "decisive acceptance below 750 (5-min hold) with RVOL >= 1.2x", "rvol_min": 1.2},
         "hard_invalidation_underlying": "sustained reclaim above 752.70 / 755.00",
         "time_stop": "45m if <0.5R progress; no new risk after 14:30 CT; flat 0DTE by 14:45 CT"}
    t.update(kw)
    return t


class Broker:
    """Mocked Alpaca client. Records every call and refuses any non-paper host."""
    def __init__(self, positions=None, orders=None):
        self.positions = positions or []
        self.orders = orders or []
        self.calls = []

    def request(self, method, host, path, body=None, params=None, timeout=20):
        pe.assert_paper_host(host)
        assert host == PAPER, host
        self.calls.append((method, path, body, params))
        if method == "GET" and path == "/v2/positions":
            return self.positions
        if method == "GET" and path == "/v2/orders":
            if (params or {}).get("status") == "open":
                return [o for o in self.orders if o.get("status") in ("new", "accepted", "partially_filled")]
            return self.orders
        if method == "POST" and path == "/v2/orders":
            return {"id": f"ord-{len(self.calls)}", "status": "accepted", "symbol": body.get("symbol")}
        return {}

    def posts(self):
        return [c[2] for c in self.calls if c[0] == "POST"]

    def deletes(self):
        return [c[1] for c in self.calls if c[0] == "DELETE"]


class _Patch:
    def __init__(self):
        self.saved = []

    def set(self, obj, name, val):
        self.saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)

    def undo(self):
        for obj, name, val in reversed(self.saved):
            setattr(obj, name, val)


class _Base(unittest.TestCase):
    def setUp(self):
        self.p = _Patch()
        self.rewrites = []

    def tearDown(self):
        self.p.undo()

    def manage(self, ticket, broker, now, bars=None, events=None):
        self.p.set(rth, "blotter_rows", lambda: [dict(ticket)])
        self.p.set(rth, "rewrite_blotter_ticket", lambda tid, fn: self.rewrites.append(fn(dict(ticket))))
        self.p.set(rth, "_gov_close", lambda *a, **k: None)
        self.p.set(pe, "alpaca_request", broker.request)
        bars = bars or (or_bars(759.0, 761.0) + [bar(9, 30, 761, 762, 760.9, 761.5)])
        doc = {"events": list(events or [])}
        notes = rth.manage_open_positions(pe, doc, rth.compute_or(bars, now), bars, now, force_sim=False)
        return notes, doc


# ------------------------------------------------------------------ routing
class Routing(_Base):
    def test_option_builds_day_limit_on_occ_paper_host(self):
        pl = pe.build_alpaca_paper_order(CALL())
        o = pl["order"]
        self.assertEqual((o["symbol"], o["type"], o["time_in_force"], o["side"], o["qty"], o["limit_price"]),
                         (OCC_C, "limit", "day", "buy", "4", "2.00"))
        self.assertEqual(pl["host"], PAPER); self.assertEqual(pl["asset_class"], "us_option")
        self.assertNotIn("order_class", o)

    def test_stage_submit_posts_limit_to_paper_only(self):
        bl = _TMP / "pod" / "ledger" / "blotter_routing.jsonl"
        bl.write_text(json.dumps(CALL()) + "\n")
        b = Broker()
        self.p.set(pe, "BLOTTER", str(bl)); self.p.set(pe, "PAPER_ORDERS", str(_TMP / "orders.jsonl"))
        self.p.set(pe, "alpaca_request", b.request); self.p.set(pe, "alpaca_status", lambda *a, **k: {})
        res = pe.alpaca_stage_ticket("TKT-TEST-OPTC", dry_run=False, submit_paper=True, occ_symbol=OCC_C)
        self.assertEqual(res["mode"], "submitted_paper")
        posts = b.posts(); self.assertEqual(len(posts), 1)
        self.assertEqual((posts[0]["type"], posts[0]["symbol"], posts[0]["limit_price"]), ("limit", OCC_C, "2.00"))

    def test_live_host_refused(self):
        for host in ("https://api.alpaca.markets", "api.alpaca.markets/v2/orders", "https://example.com"):
            with self.assertRaises(RuntimeError):
                pe.assert_paper_host(host)
        with self.assertRaises(RuntimeError):  # refused before any network or credential use
            pe.alpaca_request("POST", "https://api.alpaca.markets", "/v2/orders", body={})

    def test_tick_refuses_live_host(self):
        class LivePE:
            ALPACA_PAPER_HOST = "https://api.alpaca.markets"
            assert_paper_host = staticmethod(pe.assert_paper_host)
        with self.assertRaises(Exception):
            rth.alpaca_rth_tick(LivePE)


# ------------------------------------------------------------------ fail closed (no 732 default)
class FailClosed(_Base):
    def test_missing_contract_fields_error(self):
        t = CALL(); [t.pop(k) for k in ("occ_symbol", "option_expiration", "strike")]
        spec, err = pe.option_contract_spec(t)
        self.assertIsNone(spec); self.assertIn("missing", err)
        with self.assertRaises(RuntimeError):
            pe.build_alpaca_paper_order(t)
        with self.assertRaises(RuntimeError):
            rth.resolve_occ(pe, t)
        self.assertIsNotNone(rth.option_ticket_problem(pe, t))

    def test_missing_strike_only_and_expiry_only(self):
        for drop in ("strike", "option_expiration"):
            t = CALL(); t.pop("occ_symbol"); t.pop(drop)
            self.assertIsNotNone(pe.option_contract_spec(t)[1], drop)

    def test_stale_payload_732_symbol_is_ignored(self):
        t = CALL(); [t.pop(k) for k in ("occ_symbol", "option_expiration", "strike")]
        t["alpaca_paper_payload"] = {"order": {"symbol": "SPY301108C00732000"}}
        self.assertIsNotNone(pe.option_contract_spec(t)[1])

    def test_no_session_target_or_prior_low_fallback(self):
        t = CALL(underlying_trigger={"breakout": "acceptance above 760.50 (5-min hold) with RVOL >= 1.2x",
                                     "rvol_min": 1.2, "prior_rth_low": 732.71})
        t.pop("occ_symbol"); t.pop("strike")
        self.assertIn("strike", pe.option_contract_spec(t)[1])

    def test_inconsistent_occ_rejected(self):
        self.assertIn("strike", pe.option_contract_spec(CALL(strike=761.0))[1])
        self.assertIn("expiry", pe.option_contract_spec(CALL(option_expiration="2030-11-09"))[1])
        self.assertIn("right", pe.option_contract_spec(CALL(instrument="option_put"))[1])
        self.assertIn("bad OCC", pe.option_contract_spec(CALL(occ_symbol="SPY 11/8 760C"))[1])

    def test_expiry_plus_strike_builds_occ(self):
        t = CALL(); t.pop("occ_symbol")
        spec, err = pe.option_contract_spec(t)
        self.assertIsNone(err); self.assertEqual(spec["occ_symbol"], OCC_C)

    def test_bad_contracts_or_premium_stay_pending(self):
        self.assertIn("positive int", rth.option_ticket_problem(pe, CALL(quantity=0)))
        self.assertIn("positive int", rth.option_ticket_problem(pe, CALL(quantity=2.5)))
        self.assertIn("stop", rth.option_ticket_problem(pe, CALL(stop=None)))
        self.assertIsNone(rth.option_ticket_problem(pe, CALL()))
        self.assertIsNone(rth.option_ticket_problem(pe, STOCK()))

    def test_tick_keeps_unresolved_option_pending_never_submits(self):
        t = CALL(); [t.pop(k) for k in ("occ_symbol", "option_expiration", "strike")]
        rewrites, submits = [], []
        bars = or_bars(759.0, 760.4) + [bar(9, 30, 760.4, 761.2, 760.3, 761.0, 2000)]  # would fire
        class TPE:
            ALPACA_PAPER_HOST = PAPER
            assert_paper_host = staticmethod(pe.assert_paper_host)
            option_contract_spec = staticmethod(pe.option_contract_spec)
            @staticmethod
            def alpaca_status(): return {"status": "ok"}
            @staticmethod
            def alpaca_request(*a, **k): return {"is_open": True}
        for k, v in {"_now_ct": lambda: at(9, 36), "armed_tickets": lambda: [t], "load_events": lambda: {"events": []},
                     "save_events": lambda d: None, "rewrite_blotter_ticket": lambda tid, fn: rewrites.append(tid),
                     "fetch_alpaca_bars": lambda *a: [dict(b) for b in bars], "manage_open_positions": lambda *a, **k: [],
                     "push_vault_if_changed": lambda *a: {}, "already_submitted": lambda *a: (False, "clear"),
                     "submit_ticket": lambda *a, **k: submits.append(1)}.items():
            self.p.set(rth, k, v)
        res = rth.alpaca_rth_tick(TPE)
        self.assertEqual(submits, []); self.assertEqual(rewrites, [])
        self.assertIn("pending_invalid_option:TKT-TEST-OPTC", res["actions"])
        # control: the fully specified ticket would fire and be submitted
        t2 = CALL()
        self.p.set(rth, "armed_tickets", lambda: [t2])
        res = rth.alpaca_rth_tick(TPE)
        self.assertEqual(len(submits), 1, res["actions"])


# ------------------------------------------------------------------ entry rules
class EntryRules(_Base):
    def fire_bars(self):
        return or_bars(759.0, 760.4) + [bar(14, 25, 760.4, 761.2, 760.3, 761.0, 2000)]

    def test_call_fires_inside_window(self):
        d = rth.decide_armed_ticket(CALL(), rth.compute_or(self.fire_bars(), at(14, 29)), self.fire_bars(), at(14, 29))
        self.assertEqual(d["action"], "fire", d["reason"])

    def test_no_entry_after_1430_even_if_ticket_says_later(self):
        for t in (CALL(), CALL(time_stop="45m if <0.5R progress; no new risk after 14:50 CT; flat by 14:55 CT")):
            d = rth.decide_armed_ticket(t, rth.compute_or(self.fire_bars(), at(14, 31)), self.fire_bars(), at(14, 31))
            self.assertEqual(d["action"], "wait"); self.assertIn("outside entry window", d["reason"])

    def test_put_ticket_uses_bearish_underlying_view(self):
        r = rth.parse_ticket_rules(PUT())
        self.assertTrue(r["ok"], r["warnings"])
        self.assertEqual((r["direction"], r["side"], r["level"]), ("short", "below", 755.0))
        self.assertEqual((r["inv"]["side"], r["inv"]["level"], r["inv"]["bars"]), ("above", 756.2, 2))
        bars = or_bars(755.5, 757.0) + [bar(9, 30, 755.4, 755.5, 754.6, 754.8, 2000)]
        self.assertEqual(rth.decide_armed_ticket(PUT(), rth.compute_or(bars, at(9, 36)), bars, at(9, 36))["action"], "fire")

    def test_call_invalidation_level_from_underlying_not_premium(self):
        r = rth.parse_ticket_rules(CALL())
        self.assertEqual((r["inv"]["side"], r["inv"]["level"]), ("below", 759.8))

    def test_short_premium_option_not_supported(self):
        self.assertFalse(rth.parse_ticket_rules(CALL(direction="short"))["ok"])


# ------------------------------------------------------------------ management
def pos(qty, mark, sym=OCC_C, avg=2.00):
    return {"symbol": sym, "qty": str(qty), "side": "long", "current_price": str(mark), "avg_entry_price": str(avg)}


FILLED = dict(status="filled_paper", alpaca_filled_ct="2030-11-08T09:35:00-0600")


class Management(_Base):
    def other_order(self):
        return {"id": "other-1", "symbol": "SPY301108P00755000", "status": "new", "client_order_id": "horsey-OTHER"}

    def own_order(self, oid="own-1", status="new"):
        return {"id": oid, "symbol": OCC_C, "status": status, "client_order_id": "horsey-x"}

    def test_premium_stop_cancels_only_that_contract(self):
        b = Broker([pos(4, 1.35)], [self.other_order(), self.own_order()])
        notes, doc = self.manage(CALL(**FILLED), b, at(9, 50))
        self.assertIn("STOP_EXIT:TKT-TEST-OPTC", notes)
        self.assertEqual(b.deletes(), ["/v2/orders/own-1"])
        p = b.posts()[-1]
        self.assertEqual((p["symbol"], p["side"], p["qty"], p["type"]), (OCC_C, "sell", "4", "limit"))
        self.assertEqual(self.rewrites[-1]["status"], "closed_paper")

    def test_underlying_invalidation_exits(self):
        bars = or_bars(759.0, 761.0) + [bar(9, 40, 760.2, 760.3, 759.4, 759.50)]
        b = Broker([pos(4, 2.10)])
        notes, doc = self.manage(CALL(**FILLED), b, at(9, 45), bars=bars)
        self.assertIn("STOP_EXIT:TKT-TEST-OPTC", notes)
        self.assertIn("inv 759.80", doc["events"][-1]["detail"])

    def test_t1_scale_out_then_t2_remainder(self):
        b = Broker([pos(4, 3.85)])
        notes, doc = self.manage(CALL(**FILLED), b, at(9, 50))
        self.assertIn("T1:TKT-TEST-OPTC", notes)
        p = b.posts()[-1]
        self.assertEqual((p["qty"], p["side"], p["type"]), ("2", "sell", "limit"))
        self.assertGreaterEqual(float(p["limit_price"]), 3.80)
        self.assertEqual([e["kind"] for e in doc["events"]], ["TARGET1_SCALED"])
        # second tick: T1 done, 2 left, mark through T2
        b2 = Broker([pos(2, 5.05)], [self.own_order("t1-rest")])
        notes2, doc2 = self.manage(CALL(**FILLED), b2, at(10, 5), events=doc["events"])
        self.assertIn("TARGET2_CLOSED:TKT-TEST-OPTC", notes2)
        self.assertEqual(b2.deletes(), ["/v2/orders/t1-rest"])
        self.assertEqual(b2.posts()[-1]["qty"], "2")

    def test_t1_not_repeated(self):
        b = Broker([pos(2, 3.90)])
        notes, _ = self.manage(CALL(**FILLED), b, at(9, 55), events=[{"ticket_id": "TKT-TEST-OPTC", "kind": "TARGET1_SCALED"}])
        self.assertEqual(b.posts(), [])

    def test_time_stop(self):
        b = Broker([pos(4, 2.10)])  # +0.17R < 0.5R
        notes, _ = self.manage(CALL(**FILLED), b, at(10, 15))  # 40m
        self.assertEqual(b.posts(), [])
        b = Broker([pos(4, 2.10)])
        notes, _ = self.manage(CALL(**FILLED), b, at(10, 21))  # 46m >= 45m
        self.assertIn("TIME_STOP_EXIT:TKT-TEST-OPTC", notes)

    def test_time_stop_spared_when_progress(self):
        b = Broker([pos(4, 2.40)])  # +0.67R
        notes, _ = self.manage(CALL(**FILLED), b, at(10, 30))
        self.assertEqual(b.posts(), [])

    def test_0dte_flat_by_1445(self):
        b = Broker([pos(4, 2.40)])
        filled_late = dict(status="filled_paper", alpaca_filled_ct="2030-11-08T14:20:00-0600")
        self.manage(CALL(**filled_late), b, at(14, 44))
        self.assertEqual(b.posts(), [])
        b = Broker([pos(4, 2.40)])
        notes, _ = self.manage(CALL(**filled_late), b, at(14, 45))
        self.assertIn("EOD_0DTE_FLAT:TKT-TEST-OPTC", notes)

    def test_0dte_flat_capped_even_if_ticket_says_later(self):
        t = CALL(status="filled_paper", alpaca_filled_ct="2030-11-08T14:20:00-0600",
                 time_stop="45m if <0.5R progress; no new risk after 14:30 CT; flat by 15:00 CT")
        b = Broker([pos(4, 2.40)])
        notes, _ = self.manage(t, b, at(14, 46))
        self.assertIn("EOD_0DTE_FLAT:TKT-TEST-OPTC", notes)

    def test_partial_fill_managed_on_filled_qty_and_rest_cancelled(self):
        entry = {"id": "entry-1", "symbol": OCC_C, "status": "partially_filled", "filled_qty": "2", "qty": "4",
                 "client_order_id": "horsey-TKT-TEST-OPTC", "filled_at": None}
        b = Broker([pos(2, 1.30)], [entry])
        t = CALL(status="submitted_paper")
        notes, _ = self.manage(t, b, at(9, 50))
        self.assertIn("STOP_EXIT:TKT-TEST-OPTC", notes)
        self.assertIn("/v2/orders/entry-1", b.deletes())  # resting remainder cancelled
        self.assertEqual(b.posts()[-1]["qty"], "2")

    def test_partial_fill_starts_time_stop_clock(self):
        entry = {"id": "entry-1", "symbol": OCC_C, "status": "partially_filled", "filled_qty": "2", "qty": "4",
                 "client_order_id": "horsey-TKT-TEST-OPTC", "filled_at": None,
                 "updated_at": "2030-11-08T15:35:00Z"}
        b = Broker([pos(2, 2.05)], [entry])
        notes, doc = self.manage(CALL(status="submitted_paper"), b, at(10, 25))  # 50m after 09:35 CT
        self.assertIn("TIME_STOP_EXIT:TKT-TEST-OPTC", notes)

    def test_inactive_ticket_never_touches_position(self):
        b = Broker([pos(4, 1.0)])
        self.manage(CALL(status="closed_paper"), b, at(9, 50))
        self.assertEqual(b.posts(), []); self.assertEqual(b.deletes(), [])


# ------------------------------------------------------------------ stock regression
class StockRegression(_Base):
    def test_stock_bracket_order_unchanged(self):
        o = pe.build_alpaca_paper_order(STOCK())["order"]
        self.assertEqual((o["symbol"], o["side"], o["type"], o["order_class"], o["qty"]), ("SPY", "sell", "limit", "bracket", "125"))
        self.assertEqual((o["take_profit"]["limit_price"], o["stop_loss"]["stop_price"], o["limit_price"]), ("745.00", "751.00", "749.50"))

    def test_stock_rules_unchanged(self):
        r = rth.parse_ticket_rules(STOCK())
        self.assertEqual((r["mode"], r["direction"], r["level"], r["inv"]["level"]), ("acceptance", "short", 750.0, 752.7))
        self.assertNotIn("is_option", r)

    def test_short_stock_stop_covers(self):
        t = STOCK(status="filled_paper", alpaca_filled_ct="2030-11-08T09:35:00-0600")
        b = Broker([{"symbol": "SPY", "qty": "-125", "side": "short", "current_price": "751.10", "avg_entry_price": "749.50"}])
        notes, _ = self.manage(t, b, at(9, 40), bars=or_bars(750, 752) + [bar(9, 35, 750, 751, 749.9, 750.5)])
        self.assertIn("STOP_EXIT:TKT-TEST-STK", notes)
        self.assertEqual((b.posts()[-1]["side"], b.posts()[-1]["qty"]), ("buy", "125"))


# ------------------------------------------------------------------ risk: max loss + equity flag
class RiskSizing(_Base):
    def setUp(self):
        super().setUp()
        self.root = _TMP
        self.limits = {"equity_reference": 50000.0, "alpaca_paper_equity": 100000.0,
                       "limits": {"max_risk_per_ticket_pct": 0.5, "max_total_open_risk_pct": 1.5,
                                  "max_theme_concentration_pct": 0.75, "max_daily_realized_loss_pct": 1.0,
                                  "max_weekly_realized_loss_pct": 2.5},
                       "current_usage": {}, "kill_switch": {"breached": False, "breached_limits": []}}
        (self.root / "pod" / "risk" / "limits.json").write_text(json.dumps(self.limits))
        (self.root / "pod" / "paper_account_state.json").write_text(json.dumps({"equity": 100000.0, "positions": []}))
        (self.root / "pod" / "ledger" / "paper_blotter.jsonl").write_text("")

    def test_option_max_loss_formula(self):
        t = CALL(); t.pop("multiplier")
        self.assertAlmostEqual(gov.ticket_dollar_risk(t), (2.00 - 1.40) * 100 * 4)
        self.assertAlmostEqual(gov.ticket_dollar_risk(CALL(quantity=1, entry=3.10, stop=2.05)), 105.0)

    def test_default_flag_off_equals_original_expression(self):
        for lim in ({"equity_reference": 50000.0}, {"equity_reference": 73000}, {}, {"equity_reference": None},
                    {"equity_reference": 50000.0, "equity_reference_use_paper_account": False},
                    {"equity_reference": 50000.0, "equity_reference_use_paper_account": "true"}):
            self.assertEqual(gov.equity_reference_value(lim, {"equity": 100000.0}),
                             float(lim.get("equity_reference") or 50000.0), lim)

    def test_flag_on_uses_paper_account(self):
        lim = {"equity_reference": 50000.0, "equity_reference_use_paper_account": True, "alpaca_paper_equity": 98000.0}
        self.assertEqual(gov.equity_reference_value(lim, {"equity": 100000.0}), 100000.0)
        self.assertEqual(gov.equity_reference_value(lim, {}), 98000.0)
        self.assertEqual(gov.equity_reference_value({"equity_reference_use_paper_account": True}, {}), 100000.0)

    def test_limit_enforcement_default_vs_flag(self):
        # $400 max loss: 0.8% of $50k (> 0.5% per-ticket limit) vs 0.4% of $100k
        t = CALL(quantity=4, entry=2.00, stop=1.00, risk_per_unit=1.0)
        g = gov.apply_portfolio_gate_to_ticket(dict(t), auto_reduce=False, strict_block=True)
        self.assertEqual(g["verdict"], "BLOCKED_PORTFOLIO_LIMIT")
        lim = dict(self.limits, equity_reference_use_paper_account=True)
        (self.root / "pod" / "risk" / "limits.json").write_text(json.dumps(lim))
        g2 = gov.apply_portfolio_gate_to_ticket(dict(t), auto_reduce=False, strict_block=True)
        self.assertTrue(g2["portfolio_gate"]["pass"], g2.get("errors"))
        # limits themselves untouched
        self.assertEqual(json.loads((self.root / "pod/risk/limits.json").read_text())["limits"], self.limits["limits"])

    def test_snapshot_pct_default_vs_flag(self):
        (self.root / "pod" / "ledger" / "paper_blotter.jsonl").write_text(json.dumps(CALL()) + "\n")  # $240 staged
        snap = gov.compute_portfolio_snapshot()
        self.assertEqual(snap["equity_sizing_base"], 50000.0)
        self.assertAlmostEqual(snap["capital_at_risk"]["staged_risk_pct"], 0.48)
        lim = json.loads((self.root / "pod/risk/limits.json").read_text()); lim["equity_reference_use_paper_account"] = True
        (self.root / "pod" / "risk" / "limits.json").write_text(json.dumps(lim))
        snap2 = gov.compute_portfolio_snapshot()
        self.assertEqual(snap2["equity_sizing_base"], 100000.0)
        self.assertAlmostEqual(snap2["capital_at_risk"]["staged_risk_pct"], 0.24)


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""CIO risk tightenings (Nov 11 2026), SYNTHETIC data + mocked paper broker only:
 (a) 14:30 CT auto-cancel of unfilled ENTRY orders (that order only; never cancel-all),
 (b) submit-time kill-switch / open-risk / per-ticket checks for stock (and option) entries,
 (c) breakeven stop for the remaining contracts after a T1 fill.
Reuses fixtures from test_option_path.py (RTH_MODULE / POD_ENGINE_MODULE / GOV_MODULE honored).
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_option_path as T  # noqa: E402  (sets HORSEY_ROOT to a temp dir)

rth, pe, gov = T.rth, T.pe, T.gov
CALL, STOCK, FILLED, pos, at, Broker = T.CALL, T.STOCK, T.FILLED, T.pos, T.at, T.Broker
OCC_C = T.OCC_C
SUBMITTED = dict(status="submitted_paper")


def entry_order(tid, status="new", filled=0, sym=OCC_C, oid="entry-1", qty=4):
    return {"id": oid, "symbol": sym, "status": status, "qty": str(qty), "filled_qty": str(filled),
            "client_order_id": rth.client_order_id(tid), "filled_avg_price": "2.00" if filled else None}


class Cancel1430(T._Base):
    def _no_cancel_all(self, b):
        self.assertNotIn("/v2/orders", b.deletes())  # bulk cancel endpoint never used

    def test_unfilled_option_entry_cancelled_at_1430(self):
        other = {"id": "other-9", "symbol": "SPY301108P00755000", "status": "new", "client_order_id": "horsey-OTHER"}
        b = Broker([], [entry_order("TKT-TEST-OPTC"), other])
        notes, doc = self.manage(CALL(**SUBMITTED), b, at(14, 30))
        self.assertIn("ENTRY_CANCELLED:TKT-TEST-OPTC", notes)
        self.assertEqual(b.deletes(), ["/v2/orders/entry-1"])  # only that order
        self._no_cancel_all(b)
        self.assertEqual(b.posts(), [])
        self.assertEqual(doc["events"][-1]["kind"], "ENTRY_CANCELLED")
        self.assertEqual(self.rewrites[-1]["status"], "cancelled_paper")

    def test_unfilled_stock_entry_cancelled_at_1430(self):
        t = STOCK(**SUBMITTED)
        b = Broker([], [entry_order(t["ticket_id"], sym="SPY", oid="stk-entry", qty=125)])
        notes, _ = self.manage(t, b, at(14, 31))
        self.assertEqual(b.deletes(), ["/v2/orders/stk-entry"])
        self.assertEqual(self.rewrites[-1]["status"], "cancelled_paper")

    def test_not_cancelled_before_1430(self):
        b = Broker([], [entry_order("TKT-TEST-OPTC")])
        notes, _ = self.manage(CALL(**SUBMITTED), b, at(14, 29, 59))
        self.assertEqual(b.deletes(), [])
        self.assertFalse([n for n in notes if "CANCELLED" in n])

    def test_partially_filled_entry_not_cancelled(self):
        b = Broker([pos(2, 2.10)], [entry_order("TKT-TEST-OPTC", status="partially_filled", filled=2)])
        notes, doc = self.manage(CALL(**SUBMITTED), b, at(14, 35))
        self.assertNotIn("/v2/orders/entry-1", b.deletes())
        self.assertFalse([e for e in doc["events"] if e["kind"] == "ENTRY_CANCELLED"])

    def test_filled_entry_not_cancelled_position_left_alone(self):
        b = Broker([pos(4, 2.10)], [entry_order("TKT-TEST-OPTC", status="filled", filled=4)])
        notes, doc = self.manage(CALL(**SUBMITTED, session_target="2030-11-12", option_expiration="2030-11-12",
                                      occ_symbol="SPY301112C00760000"), b, at(14, 35))
        self.assertEqual(b.deletes(), [])
        self.assertEqual(b.posts(), [])
        self.assertFalse([e for e in doc["events"] if e["kind"] == "ENTRY_CANCELLED"])

    def test_cancelled_ticket_never_rearmed(self):
        self.assertIn("cancelled_paper", rth.DONE_STATUSES)


class SubmitGate(T._Base):
    def setUp(self):
        super().setUp()
        self.root = T._TMP
        (self.root / "pod" / "risk" / "limits.json").write_text(json.dumps({
            "equity_reference": 50000.0, "alpaca_paper_equity": 100000.0,
            "limits": {"max_risk_per_ticket_pct": 0.5, "max_total_open_risk_pct": 1.5}}))
        self.blotter = self.root / "pod" / "ledger" / "paper_blotter.jsonl"
        self.staged = []
        self.p.set(rth, "_governance", lambda: gov)
        self.p.set(gov, "kill_switch_blocks_new_entries", lambda: (False, "ARMED_CLEAR"))
        self.p.set(pe, "alpaca_stage_ticket", lambda tid, **k: self.staged.append((tid, k)) or {"response": {"id": "x"}})

    def tearDown(self):
        super().tearDown()
        self.blotter.unlink(missing_ok=True)

    def open_book(self, *risks):
        rows = [{"ticket_id": f"TKT-OPEN-{i}", "status": "filled_paper", "instrument": "stock",
                 "actual_dollar_risk": r} for i, r in enumerate(risks)]
        self.blotter.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def submit(self, t):
        doc = {"events": []}
        res = rth.submit_ticket(pe, t, "trigger", {"or_low": 1, "or_high": 2}, doc, force_sim=False)
        return res, doc

    def test_stock_blocked_by_kill_switch(self):
        self.open_book()
        self.p.set(gov, "kill_switch_blocks_new_entries", lambda: (True, "REFUSED_KILL_SWITCH_BREACH: daily loss"))
        res, doc = self.submit(STOCK())
        self.assertFalse(res["submitted"]); self.assertEqual(res["mode"], "blocked")
        self.assertEqual(self.staged, [])
        self.assertEqual(doc["events"][-1]["kind"], "SUBMIT_BLOCKED")
        self.assertIn("Kill switch breached", doc["events"][-1]["detail"])

    def test_stock_blocked_by_open_risk(self):
        self.open_book(300.0, 300.0)  # 1.2% open; +187.50 (0.375%) = 1.575% > 1.5%
        res, doc = self.submit(STOCK())
        self.assertFalse(res["submitted"]); self.assertEqual(self.staged, [])
        self.assertIn("total open-risk limit", res["reason"])
        self.assertEqual(doc["events"][-1]["kind"], "SUBMIT_BLOCKED")

    def test_stock_blocked_by_per_ticket_limit(self):
        self.open_book()
        res, _ = self.submit(STOCK(stop=752.0))  # 125 x 2.50 = $312.50 = 0.625% > 0.5%
        self.assertFalse(res["submitted"]); self.assertIn("per-ticket limit", res["reason"])

    def test_stock_allowed_under_limits(self):
        self.open_book(300.0)  # 0.6% + 0.375% = 0.975% <= 1.5%
        res, doc = self.submit(STOCK())
        self.assertTrue(res["submitted"])
        self.assertEqual([s[0] for s in self.staged], ["TKT-TEST-STK"])
        self.assertEqual(doc["events"][-1]["kind"], "ORDER_SUBMITTED")

    def test_blocked_reason_logged_once(self):
        self.open_book(300.0, 300.0)
        doc = {"events": []}
        for _ in range(3):
            rth.submit_ticket(pe, STOCK(), "trigger", {}, doc, force_sim=False)
        self.assertEqual(len([e for e in doc["events"] if e["kind"] == "SUBMIT_BLOCKED"]), 1)

    def test_governance_error_fails_closed(self):
        self.open_book()
        def boom():
            raise RuntimeError("synthetic failure")
        self.p.set(rth, "_governance", boom)
        res, _ = self.submit(STOCK())
        self.assertFalse(res["submitted"]); self.assertEqual(self.staged, [])

    def test_option_uses_same_gate_and_contract_risk(self):
        self.open_book(500.0)  # 1.0% open; option 4 x (2.00-1.40) x 100 = $240 = 0.48% -> 1.48% ok
        res, _ = self.submit(CALL())
        self.assertTrue(res["submitted"])
        self.open_book(500.0, 20.0)  # 1.04% + 0.48% = 1.52% > 1.5%
        res2, _ = self.submit(CALL())
        self.assertFalse(res2["submitted"])

    def test_limits_and_flag_untouched(self):
        self.open_book()
        before = (self.root / "pod" / "risk" / "limits.json").read_text()
        self.submit(STOCK())
        self.assertEqual(json.loads(before), json.loads((self.root / "pod" / "risk" / "limits.json").read_text()))
        self.assertEqual(gov.equity_reference_value(json.loads(before)), 50000.0)


class EventTextIsHuman(T._Base):
    """New event headlines/details are shown raw in the dashboard: no snake_case codes."""
    SNAKE = __import__("re").compile(r"\b[a-z]+_[a-z0-9_]+\b|\b[A-Z]+_[A-Z0-9_]+\b")

    def test_new_event_text_has_no_codes(self):
        texts = []
        b = Broker([], [entry_order("TKT-TEST-OPTC")])
        _, doc = self.manage(CALL(**SUBMITTED), b, at(14, 30)); texts += doc["events"]
        b = Broker([pos(2, 2.50)], [{"id": "t1", "symbol": OCC_C, "status": "filled", "filled_qty": "2",
                                      "client_order_id": "horsey-t1-TKT-TEST-OPTC"}])
        _, doc = self.manage(CALL(**FILLED), b, at(10, 0), events=[{"ticket_id": "TKT-TEST-OPTC", "kind": "TARGET1_SCALED"}])
        texts += doc["events"]
        self.p.set(rth, "_governance", lambda: gov)
        for ks in [(True, "REFUSED_KILL_SWITCH_BREACH: max_daily_realized_loss_pct"), (False, "ARMED_CLEAR")]:
            self.p.set(gov, "kill_switch_blocks_new_entries", lambda ks=ks: ks)
            d = {"events": []}
            rth.submit_ticket(pe, STOCK(stop=752.0), "t", {}, d, force_sim=False); texts += d["events"]
        def boom():
            raise RuntimeError("x_y_z")
        self.p.set(rth, "_governance", boom)
        d = {"events": []}; rth.submit_ticket(pe, STOCK(), "t", {}, d, force_sim=False); texts += d["events"]
        new = [e for e in texts if e["kind"] in ("ENTRY_CANCELLED", "STOP_TO_BREAKEVEN", "SUBMIT_BLOCKED")]
        self.assertGreaterEqual(len(new), 5)
        for e in new:
            for field in ("headline", "detail"):
                txt = e[field].replace("TKT-TEST-OPTC", "").replace("TKT-TEST-STK", "")
                self.assertFalse(self.SNAKE.search(txt), (e["kind"], field, txt))


class Breakeven(T._Base):
    T1_COID = "horsey-t1-TKT-TEST-OPTC"

    def t1_order(self, filled):
        return {"id": "t1-ord", "symbol": OCC_C, "status": "filled" if filled else "new",
                "filled_qty": str(filled), "client_order_id": self.T1_COID}

    def test_stop_moves_to_breakeven_after_t1_fill(self):
        b = Broker([pos(4, 3.85)])
        notes, doc = self.manage(CALL(**FILLED), b, at(9, 50))
        t1_ev = [e for e in doc["events"] if e["kind"] == "TARGET1_SCALED"][0]
        self.assertIn("once T1 fills", t1_ev["detail"])
        # T1 filled 2 of 4; mark 2.50 (above entry) -> stop moved to entry premium, no exit
        b2 = Broker([pos(2, 2.50)], [self.t1_order(2)])
        notes2, doc2 = self.manage(CALL(**FILLED), b2, at(10, 0), events=doc["events"])
        self.assertIn("BREAKEVEN:TKT-TEST-OPTC", notes2)
        self.assertEqual(self.rewrites[-1]["alpaca_breakeven_stop"], 2.0)
        be_ev = [e for e in doc2["events"] if e["kind"] == "STOP_TO_BREAKEVEN"][0]
        self.assertIn("2× " + OCC_C, be_ev["headline"]); self.assertIn("$1.40 → $2.00 entry premium", be_ev["detail"])
        self.assertEqual(b2.posts(), [])
        # mark 1.95: below breakeven (but above original 1.40 stop) -> remaining 2 exit
        b3 = Broker([pos(2, 1.95)], [self.t1_order(2)])
        notes3, doc3 = self.manage(CALL(**FILLED, alpaca_breakeven_stop=2.0), b3, at(10, 10), events=doc2["events"])
        self.assertIn("STOP_EXIT:TKT-TEST-OPTC", notes3)
        self.assertEqual(b3.posts()[-1]["qty"], "2")
        self.assertIn("BREAKEVEN STOP", doc3["events"][-1]["headline"])
        self.assertEqual(len([e for e in doc3["events"] if e["kind"] == "STOP_TO_BREAKEVEN"]), 1)

    def test_no_breakeven_until_t1_fills(self):
        b = Broker([pos(4, 1.95)], [self.t1_order(0)])
        notes, doc = self.manage(CALL(**FILLED), b, at(10, 0),
                                 events=[{"ticket_id": "TKT-TEST-OPTC", "kind": "TARGET1_SCALED"}])
        self.assertNotIn("BREAKEVEN:TKT-TEST-OPTC", notes)
        self.assertNotIn("STOP_EXIT:TKT-TEST-OPTC", notes)

    def test_single_contract_unaffected(self):
        one = CALL(**FILLED, quantity=1)
        b = Broker([pos(1, 3.90)])
        notes, doc = self.manage(one, b, at(9, 50))
        self.assertEqual(b.posts(), [])  # no T1 scale with 1 contract
        self.assertFalse([e for e in doc["events"] if e["kind"] in ("TARGET1_SCALED", "STOP_TO_BREAKEVEN")])
        b2 = Broker([pos(1, 1.95)])
        notes2, _ = self.manage(one, b2, at(10, 0))
        self.assertNotIn("STOP_EXIT:TKT-TEST-OPTC", notes2)  # original 1.40 stop still applies
        b3 = Broker([pos(1, 1.35)])
        notes3, _ = self.manage(one, b3, at(10, 5))
        self.assertIn("STOP_EXIT:TKT-TEST-OPTC", notes3)


if __name__ == "__main__":
    unittest.main(verbosity=1)

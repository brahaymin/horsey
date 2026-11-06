#!/usr/bin/env python3
"""Tempdir integration tests for CEO upgrades 1–4. Production pod/ untouched."""
from __future__ import annotations
import json, os, shutil, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

class TestPodUpgrades(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        # copy pod skeleton
        shutil.copytree(ROOT / "pod", self.td / "pod")
        # minimal latest.json
        latest = ROOT / "latest.json"
        if latest.exists():
            shutil.copy2(latest, self.td / "latest.json")
        else:
            (self.td / "latest.json").write_text(json.dumps({"summary": {"spy_price": 733.0, "vix": 28.18, "spy_sma_200": 730.15}}))
        (self.td / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts" / "_pod_governance.py", self.td / "scripts" / "_pod_governance.py")
        os.environ["HORSEY_ROOT"] = str(self.td)
        # reload module fresh
        import importlib
        if "_pod_governance" in sys.modules:
            del sys.modules["_pod_governance"]
        import importlib.util
        spec = importlib.util.spec_from_file_location("_pod_governance", self.td / "scripts" / "_pod_governance.py")
        self.g = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.g)
        # reset paths already bound at import — rebind
        for name in ("ROOT","POD","REVIEWS","RISK","TRACK","BLOTTER","SCORE","ROSTER","STATE","PAPER_STATE","LATEST","LIMITS","PORTFOLIO","REVIEWS_INDEX","SCHEMA","TEMPLATE","RULES","TRADES","ARMED_Q","WEEKLY_JSON","WEEKLY_MD","CEO_Q","CEO_D","CIO_DIR","MEMOS_IDX"):
            if hasattr(self.g, name):
                # recompute from ROOT
                pass
        # Force rebind critical paths
        R = self.td
        self.g.ROOT = R
        self.g.POD = R / "pod"
        self.g.REVIEWS = R / "pod" / "reviews"
        self.g.RISK = R / "pod" / "risk"
        self.g.TRACK = R / "pod" / "track_record"
        self.g.BLOTTER = R / "pod" / "ledger" / "paper_blotter.jsonl"
        self.g.SCORE = R / "pod" / "scorecard.json"
        self.g.ROSTER = R / "pod" / "roster.json"
        self.g.STATE = R / "pod" / "signals" / "latest_pod_state.json"
        self.g.PAPER_STATE = R / "pod" / "paper_account_state.json"
        self.g.LATEST = R / "latest.json"
        self.g.LIMITS = R / "pod" / "risk" / "limits.json"
        self.g.PORTFOLIO = R / "pod" / "risk" / "portfolio_snapshot.json"
        self.g.REVIEWS_INDEX = R / "pod" / "reviews" / "reviews_index.json"
        self.g.SCHEMA = R / "pod" / "reviews" / "schema.json"
        self.g.TEMPLATE = R / "pod" / "reviews" / "TEMPLATE.md"
        self.g.RULES = R / "pod" / "track_record" / "rules_registry.json"
        self.g.TRADES = R / "pod" / "track_record" / "trades.jsonl"
        self.g.ARMED_Q = R / "pod" / "track_record" / "armed_rules_queue.json"
        self.g.WEEKLY_JSON = R / "pod" / "track_record" / "weekly_summary.json"
        self.g.WEEKLY_MD = R / "pod" / "track_record" / "weekly_summary.md"
        self.g.CEO_Q = R / "pod" / "ceo_questions.json"
        self.g.CEO_D = R / "pod" / "ceo_decisions" / "latest_decisions.json"
        self.g.CIO_DIR = R / "pod" / "cio_briefings"
        self.g.MEMOS_IDX = R / "pod" / "memos_index.json"
        self.g.bootstrap_production_state()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("HORSEY_ROOT", None)

    def _ticket_id(self):
        rows = self.g.rjl(self.g.BLOTTER)
        for t in rows:
            if t.get("ticket_id") == "TKT-ITT-2026-11-06-A":
                return "TKT-ITT-2026-11-06-A"
        return rows[-1]["ticket_id"]

    def test_upgrade1_close_and_complete_review(self):
        tid = self._ticket_id()
        rev = self.g.close_ticket(tid, exit_price=3.65, exit_reason="target1_target2")
        self.assertEqual(rev["review_id"], f"REV-{tid}")
        self.assertEqual(rev["status"], "stub")
        day = (self.g.parse_ct(rev["closed_ct"]) or self.g.now_ct()).strftime("%Y-%m-%d")
        jp = self.g.REVIEWS / f"{day}_{tid}.json"
        mp = self.g.REVIEWS / f"{day}_{tid}.md"
        self.assertTrue(jp.exists())
        self.assertTrue(mp.exists())
        schema = json.loads(self.g.SCHEMA.read_text())
        for req in schema["required"]:
            self.assertIn(req, rev)
        idx = json.loads(self.g.REVIEWS_INDEX.read_text())
        self.assertGreaterEqual(idx["count"], 1)
        # complete-review applies weight
        sc_before = json.loads(self.g.SCORE.read_text())
        sid = rev["originating_specialist_id"]
        before = sc_before["agents"][sid]["regime_weights"]["short_gamma_backwardation"]
        done = self.g.complete_review(tid, "delta", "Scale winners faster after OR breakdown confirmation.", 0.05, "short_gamma_backwardation")
        self.assertEqual(done["status"], "completed")
        self.assertTrue(done["system_learning"]["applied_to_scorecard"])
        sc = json.loads(self.g.SCORE.read_text())
        after = sc["agents"][sid]["regime_weights"]["short_gamma_backwardation"]
        self.assertAlmostEqual(after, max(0.10, min(0.60, before + 0.05)), places=4)
        self.assertTrue(any(x.get("review_id") == done["review_id"] for x in sc.get("attribution_log") or []))
        # track record appended
        trades = self.g.rjl(self.g.TRADES)
        self.assertTrue(any(t.get("ticket_id") == tid for t in trades))

    def test_upgrade2_portfolio_and_auto_reduce(self):
        snap = self.g.compute_portfolio_snapshot()
        self.assertIn("delta_exposure", snap)
        self.assertEqual(snap["equity_sizing_base"], 50000.0)
        self.assertGreaterEqual(snap["staged_tickets_count"], 1)
        inc = snap["delta_exposure"]["including_staged"]
        self.assertLess(inc["net_delta_shares"], 0)  # long puts → negative delta
        # auto-reduce: craft oversized ticket
        t = {
            "ticket_id": "TKT-TEST-OVER",
            "symbol": "SPY",
            "instrument": "option_put",
            "direction": "long",
            "quantity": 50,
            "entry": 1.85,
            "stop": 1.25,
            "risk_per_unit": 0.6,
            "multiplier": 100,
            "actual_dollar_risk": 50 * 0.6 * 100,
            "strategy_agent": "ee33b632-6c42-4d43-bf17-665bc939db17",
            "regime_tags": ["short_gamma_backwardation"],
            "verdict": "APPROVED_FOR_STAGING",
            "errors": [],
        }
        # set very tight open limit
        lim = json.loads(self.g.LIMITS.read_text())
        lim["limits"]["max_total_open_risk_pct"] = 0.30  # only 0.06% room beyond 0.24 staged
        self.g.wj(self.g.LIMITS, lim)
        self.g.compute_portfolio_snapshot()
        out = self.g.apply_portfolio_gate_to_ticket(dict(t), auto_reduce=True)
        self.assertTrue(out.get("size_auto_reduced") or out.get("verdict") == "BLOCKED_PORTFOLIO_LIMIT")
        if out.get("size_auto_reduced"):
            self.assertLess(out["quantity"], 50)
            self.assertGreaterEqual(out["quantity"], 1)
        # strict block with no room for even 1
        lim["limits"]["max_total_open_risk_pct"] = 0.24
        self.g.wj(self.g.LIMITS, lim)
        self.g.compute_portfolio_snapshot()
        out2 = self.g.apply_portfolio_gate_to_ticket(dict(t), auto_reduce=True)
        self.assertEqual(out2.get("verdict"), "BLOCKED_PORTFOLIO_LIMIT")

    def test_upgrade3_track_record_metrics(self):
        # empty metrics nulls
        w = self.g.rebuild_weekly_summary()
        self.assertEqual(w["cumulative"]["closed_trades"], 0)
        self.assertIsNone(w["cumulative"]["win_rate"])
        self.assertIsNone(w["cumulative"]["avg_r"])
        self.assertIsNone(w["cumulative"]["profit_factor"])
        # close one win
        tid = self._ticket_id()
        self.g.close_ticket(tid, exit_price=3.65, exit_reason="target1_target2")
        w2 = self.g.rebuild_weekly_summary()
        self.assertEqual(w2["cumulative"]["closed_trades"], 1)
        self.assertEqual(w2["cumulative"]["wins"], 1)
        self.assertEqual(w2["cumulative"]["win_rate"], 1.0)
        self.assertIsNotNone(w2["cumulative"]["avg_r"])
        self.assertEqual(w2["cumulative"]["profit_factor"], "∞")
        self.assertGreaterEqual(w2["armed_count"], 0)

    def test_upgrade4_kill_switch(self):
        lim = json.loads(self.g.LIMITS.read_text())
        # simulate -1.10% daily loss via a closed blotter row
        tid = self._ticket_id()
        # write a synthetic closed sibling without disturbing review path too much
        rows = self.g.rjl(self.g.BLOTTER)
        synth = dict(rows[0])
        synth["ticket_id"] = "TKT-LOSS-SIM"
        synth["status"] = "closed_paper"
        synth["closed_ct"] = self.g.iso_ct()
        synth["realized_pnl_dollars"] = -550.0  # 1.10% of 50k
        with self.g.BLOTTER.open("a") as f:
            f.write(json.dumps(synth) + "\n")
        out = self.g.check_limits()
        self.assertTrue(out["kill_switch"]["breached"])
        self.assertEqual(out["kill_switch"]["status"], "BREACHED")
        blocked, why = self.g.kill_switch_blocks_new_entries()
        self.assertTrue(blocked)
        self.assertIn("REFUSED_KILL_SWITCH_BREACH", why)
        # memo + ceo question
        memo = out["kill_switch"].get("breach_memo_path")
        self.assertTrue(memo)
        self.assertTrue((self.td / memo).exists())
        q = json.loads(self.g.CEO_Q.read_text())
        self.assertTrue(any(qq.get("id") == "kill_switch_override" for qq in q.get("questions") or []))
        # staging gate
        t = {"ticket_id": "TKT-NEW", "symbol": "SPY", "instrument": "option_put", "direction": "long",
             "quantity": 1, "entry": 1.85, "stop": 1.25, "risk_per_unit": 0.6, "multiplier": 100,
             "actual_dollar_risk": 60, "strategy_agent": "ee33b632-6c42-4d43-bf17-665bc939db17",
             "regime_tags": ["short_gamma_backwardation"], "verdict": "APPROVED_FOR_STAGING", "errors": []}
        gated = self.g.apply_portfolio_gate_to_ticket(t, auto_reduce=True)
        self.assertEqual(gated["verdict"], "REFUSED_KILL_SWITCH_BREACH")
        # CEO reset
        self.g.record_kill_switch_decision("reset_clear", "CEO reset after review")
        lim2 = json.loads(self.g.LIMITS.read_text())
        self.assertFalse(lim2["kill_switch"]["breached"])
        self.assertEqual(lim2["kill_switch"]["status"], "ARMED_CLEAR")
        self.assertTrue(any(e.get("action") == "reset_clear" for e in lim2.get("override_audit_log") or []))
        blocked2, _ = self.g.kill_switch_blocks_new_entries()
        # daily loss still there — check_limits will re-breach!
        # So raise daily limit instead for clean restore path
        self.g.record_kill_switch_decision("raise_daily_1_5", "temp raise")
        blocked3, status = self.g.kill_switch_blocks_new_entries()
        # with 1.5% daily cap, -1.10% should clear
        self.assertFalse(blocked3)

if __name__ == "__main__":
    # Ensure production blotter unchanged: run tests then compare production ticket status
    prod_blotter = (ROOT / "pod" / "ledger" / "paper_blotter.jsonl").read_text()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestPodUpgrades)
    res = unittest.TextTestRunner(verbosity=2).run(suite)
    after = (ROOT / "pod" / "ledger" / "paper_blotter.jsonl").read_text()
    assert prod_blotter == after, "PRODUCTION BLOTTER MUTATED"
    sys.exit(0 if res.wasSuccessful() else 1)

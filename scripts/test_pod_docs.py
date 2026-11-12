#!/usr/bin/env python3
"""Unit tests for the Pod Docs builder: display normalization, hygiene scan (fail-closed), template
rendering, and the dashboard reader wiring. Synthetic inputs only; never reads live desk data.
Run: /workspace/spy_gamma_venv/bin/python scripts/test_pod_docs.py
"""
import json, os, re, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_pod_docs as B  # noqa: E402

ROOT = HERE.parent


class Normalize(unittest.TestCase):
    def test_paths_snake_uuid_removed(self):
        t = B.normalize("See `pod/intraday_tape_gamma/2026-11-12_key_level_mechanics_proposal.md` and `rvol_20d`, owner "
                        "7ad52fe7-3d09-4438-bfa9-552a13aef8b6, file /workspace/horsey/latest.json and gex_scopes.")
        self.assertFalse(B.scan(t), t)
        self.assertIn("key level mechanics proposal", t)
        self.assertIn("old 20-day RVOL", t)

    def test_plain_text_kept(self):
        s = "Breakouts wait for the 30-minute range; reclaims arm at 8:45 AM CT at half size."
        self.assertEqual(B.normalize(s), s)


class Hygiene(unittest.TestCase):
    CASES = {
        "masked or full account number": "Brokerage ••••1234 updated",
        "balance or account value": "portfolio value $12,345 today",
        "positions or holdings": "holds 12 shares of ABCD at cost basis",
        "income sleeve / brokerage": "Income Sleeve memo",
        "family / personal": "pay down student debt",
        "email": "ping someone@example.com",
        "secret": "APCA_API_SECRET_KEY",
        "unfinished placeholder": "ENGINE PLACEHOLDER",
    }

    def test_each_category_flags(self):
        for cat, txt in self.CASES.items():
            self.assertIn(cat, B.scan(txt), cat)

    def test_clean_text_passes(self):
        self.assertEqual(B.scan("Max risk per ticket 0.5%. The 2:30 PM CT cancel scan voids stale entries."), {})


class BuildFailsClosed(unittest.TestCase):
    def _root(self, td, runbook):
        r = Path(td); (r / "docs" / "pod_docs").mkdir(parents=True); (r / "pod" / "playbooks").mkdir(parents=True)
        (r / "pod" / "intraday_tape_gamma").mkdir(parents=True)
        (r / "pod" / "intraday_tape_gamma" / "2026-11-12_joint_level_selection_framework.md").write_text("# Framework\nUse `tod_rvol`.\n")
        (r / "pod" / "playbooks" / "ceo_legacy_premarket_template.txt").write_text("• Run my gamma indicators:\n  - Call Wall\n\nOverview\n• Futures\n")
        for fn in ("architecture.md", "roster.md", "changelog.md"):
            (r / "docs" / "pod_docs" / fn).write_text(f"# {fn}\nPlain text.\n")
        (r / "docs" / "pod_docs" / "runbook.md").write_text(runbook)
        return r

    def _run(self, root):
        return subprocess.run([sys.executable, str(HERE / "build_pod_docs.py")], env=dict(os.environ, HORSEY_ROOT=str(root)), capture_output=True, text=True)

    def test_clean_build_writes_index(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._root(td, "# Runbook\nAll clear.\n"); p = self._run(r)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            idx = json.loads((r / "docs" / "pod_docs" / "index.json").read_text())
            self.assertEqual(len(idx["docs"]), 6); self.assertEqual(idx["hygiene"], "passed")
            for d in idx["docs"]:
                self.assertFalse(re.search(r"_|/|\.md", d["title"]), d["title"])
            tm = (r / "docs" / "pod_docs" / "premarket_template.md").read_text()
            self.assertIn("- Run my gamma indicators:", tm); self.assertIn("## Overview", tm)
            self.assertIn("time-of-day RVOL", (r / "docs" / "pod_docs" / "framework.md").read_text())

    def test_dirty_doc_blocks_index(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._root(td, "# Runbook\nCheck the Robinhood holdings ••••9876.\n"); p = self._run(r)
            self.assertEqual(p.returncode, 2)
            self.assertFalse((r / "docs" / "pod_docs" / "index.json").exists())
            self.assertNotIn("9876", p.stdout)  # only category counts are reported


class ReaderWiring(unittest.TestCase):
    def test_reader_in_dashboard(self):
        h = (ROOT / "app_unlocked.html").read_text()
        self.assertIn('id="pdCard"', h); self.assertIn("function loadPodDocs", h); self.assertIn("loadPlaybook(){loadPodDocs();", h)
        self.assertIn("Playbooks &amp; Pod Docs", h)

    def test_docs_gitignored(self):
        p = subprocess.run(["git", "check-ignore", "-q", "docs/pod_docs/runbook.md"], cwd=str(ROOT))
        self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)

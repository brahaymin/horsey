#!/usr/bin/env python3
"""Memo publication guard tests (synthetic data only, temp HORSEY_ROOT, synthetic password).

Income Sleeve memos publish only if listed in pod/income_sleeve/approved_memos.json; a missing
or malformed file means none are approved. Covers pod_engine build-memos-index, vault_crypto
build-state (index loop + pod/ walk + stale index sanitizing), and an hourly-sync-style chain
(build-memos-index -> build-state via subprocess, as sync_github_archive.py runs it).
Module paths can be overridden with VC_MODULE / PE_MODULE (defaults: scripts/*.py).
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
VC_PATH = Path(os.environ.get("VC_MODULE", HERE / "vault_crypto.py"))
PE_PATH = Path(os.environ.get("PE_MODULE", HERE / "pod_engine.py"))
PW = "synthetic-test-password-not-a-secret"

APPROVED = ["2030-01-02_0915-ct-baseline_income_sleeve.md", "2030-01-03_1535-ct-post-close_income_sleeve.md"]
UNAPPROVED = ["2030-01-04_1545-ct-post-close_income_sleeve.md", "2030-01-05_1543-ct-post-close_income_sleeve.md"]


def load(path, name, root):
    os.environ["HORSEY_ROOT"] = str(root)
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def make_root(approval="default"):
    root = Path(tempfile.mkdtemp(prefix="memo_guard_"))
    inc = root / "pod" / "income_sleeve"
    inc.mkdir(parents=True)
    for n in APPROVED + UNAPPROVED:
        (inc / n).write_text(f"# Income Sleeve memo {n[:10]}\n\nSynthetic body SECRET-{n[:10]}.\n")
    (inc / "holdings_snapshot.json").write_text(json.dumps({"synthetic": True, "positions": []}))
    if approval == "default":
        (inc / "approved_memos.json").write_text(json.dumps({"approved": APPROVED}))
    elif approval is not None:
        (inc / "approved_memos.json").write_text(approval)
    (root / "pod" / "cio_briefings").mkdir()
    (root / "pod" / "cio_briefings" / "2030-01-05_1600-ct-post-close_cio_briefing.md").write_text("# CIO synthetic\n\nBody.\n")
    (root / "pod" / "ledger").mkdir()
    (root / "pod" / "ledger" / "paper_blotter.jsonl").write_text(json.dumps({"ticket_id": "TKT-SYN-J", "instrument": "option_call"}) + "\n")
    (root / "pod" / "ceo_decisions").mkdir()
    (root / "pod" / "ceo_decisions" / "latest_decisions.json").write_text(json.dumps({"decision": "synthetic", "ts": "15:42"}))
    return root


def stale_unguarded_index(root):
    """An index written by an old/unguarded process that lists every memo."""
    memos = [{"id": f"income_sleeve:{n[:15]}", "role": "income_sleeve", "path": f"pod/income_sleeve/{n}"} for n in APPROVED + UNAPPROVED]
    idx = {"count": len(memos), "memos": memos,
           "by_role": {"income_sleeve": [m["id"] for m in memos]},
           "roles": [{"role": "income_sleeve", "count": len(memos)}],
           "by_run": [{"run_id": m["id"], "memos": {"income_sleeve": m["id"]}} for m in memos]}
    (root / "pod" / "memos_index.json").write_text(json.dumps(idx))


class GuardFunction(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.vc = load(VC_PATH, "vc_t", self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_approved_and_unapproved(self):
        for n in APPROVED:
            self.assertTrue(self.vc.memo_publishable(f"pod/income_sleeve/{n}", root=self.root))
        for n in UNAPPROVED:
            self.assertFalse(self.vc.memo_publishable(f"pod/income_sleeve/{n}", root=self.root))

    def test_other_paths_unaffected(self):
        for rel in ("pod/cio_briefings/x_cio_briefing.md", "pod/ledger/paper_blotter.jsonl", "latest.json",
                    "pod/income_sleeve/holdings_snapshot.json"):
            self.assertTrue(self.vc.memo_publishable(rel, root=self.root), rel)

    def test_approval_file_and_subdirs_never_published(self):
        self.assertFalse(self.vc.memo_publishable("pod/income_sleeve/approved_memos.json", root=self.root))
        self.assertFalse(self.vc.memo_publishable("pod/income_sleeve/sub/" + APPROVED[0], root=self.root))

    def test_missing_file_means_none_approved(self):
        os.remove(self.root / "pod/income_sleeve/approved_memos.json")
        self.assertEqual(self.vc.load_approved_memos(self.root), frozenset())
        for n in APPROVED:
            self.assertFalse(self.vc.memo_publishable(f"pod/income_sleeve/{n}", root=self.root))

    def test_malformed_file_means_none_approved(self):
        for bad in ("{not json", json.dumps({"approved": "x.md"}), json.dumps(42), ""):
            (self.root / "pod/income_sleeve/approved_memos.json").write_text(bad)
            self.assertEqual(self.vc.load_approved_memos(self.root), frozenset(), bad)

    def test_filter_memos_index(self):
        stale_unguarded_index(self.root)
        idx = json.loads((self.root / "pod/memos_index.json").read_text())
        out = self.vc.filter_memos_index(idx, root=self.root)
        paths = [m["path"] for m in out["memos"]]
        self.assertEqual(sorted(paths), sorted(f"pod/income_sleeve/{n}" for n in APPROVED))
        self.assertEqual(out["count"], 2)
        self.assertEqual(len(out["by_role"]["income_sleeve"]), 2)
        self.assertEqual(out["roles"][0]["count"], 2)
        self.assertEqual(len(out["by_run"]), 2)


class BuildState(unittest.TestCase):
    def _state(self, root):
        vc = load(VC_PATH, "vc_s", root)
        return vc.collect_state_files()

    def test_state_excludes_unapproved_from_walk_and_index(self):
        root = make_root(); stale_unguarded_index(root)
        try:
            files = self._state(root)
            blob = json.dumps(files)
            for n in APPROVED:
                self.assertIn(f"pod/income_sleeve/{n}", files)
            for n in UNAPPROVED:
                self.assertNotIn(f"pod/income_sleeve/{n}", files)
                self.assertNotIn(n, blob)
                self.assertNotIn("SECRET-" + n[:10], blob)
            self.assertNotIn("pod/income_sleeve/approved_memos.json", files)
            # other pod content intact
            for rel in ("pod/ledger/paper_blotter.jsonl", "pod/ceo_decisions/latest_decisions.json",
                        "pod/income_sleeve/holdings_snapshot.json", "pod/cio_briefings/2030-01-05_1600-ct-post-close_cio_briefing.md"):
                self.assertIn(rel, files)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_missing_approval_file_publishes_no_income_memos(self):
        root = make_root(approval=None); stale_unguarded_index(root)
        try:
            files = self._state(root)
            self.assertFalse([k for k in files if k.startswith("pod/income_sleeve/") and k.endswith(".md")])
            self.assertIn("pod/income_sleeve/holdings_snapshot.json", files)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class MemosIndex(unittest.TestCase):
    def test_build_memos_index_excludes_unapproved(self):
        root = make_root()
        try:
            pe = load(PE_PATH, "pe_i", root)
            idx = pe.build_memos_index()
            paths = [m["path"] for m in idx["memos"] if m["role"] == "income_sleeve"]
            self.assertEqual(sorted(paths), sorted(f"pod/income_sleeve/{n}" for n in APPROVED))
            self.assertTrue(any(m["role"] == "cio" for m in idx["memos"]))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_build_memos_index_missing_file_none(self):
        root = make_root(approval=None)
        try:
            pe = load(PE_PATH, "pe_j", root)
            idx = pe.build_memos_index()
            self.assertFalse([m for m in idx["memos"] if m["role"] == "income_sleeve"])
        finally:
            shutil.rmtree(root, ignore_errors=True)


class HourlySyncStyleChain(unittest.TestCase):
    """Same commands sync_github_archive.py runs: pod_engine build-memos-index (which also calls
    maybe_rebuild_vault_state) then vault_crypto build-state; decrypt the result."""

    def test_chain_publishes_nothing_unapproved(self):
        root = make_root(); stale_unguarded_index(root)
        try:
            sd = root / "scripts"; sd.mkdir()
            for f in HERE.glob("*.py"):
                if not f.name.startswith("test_"):
                    shutil.copy2(f, sd / f.name)
            shutil.copy2(VC_PATH, sd / "vault_crypto.py"); shutil.copy2(PE_PATH, sd / "pod_engine.py")
            env = dict(os.environ, HORSEY_ROOT=str(root), HORSEY_VAULT_PASSWORD=PW, HORSEY_REPO_LOCK_HELD="1")
            for cmd in (["pod_engine.py", "build-memos-index"], ["vault_crypto.py", "build-state"]):
                r = subprocess.run([sys.executable, str(sd / cmd[0]), cmd[1]], cwd=root, env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 0, r.stderr[-500:])
            vc = load(sd / "vault_crypto.py", "vc_c", root)
            raw = vc.decrypt_blob(PW, json.loads((root / "vault/state.enc.json").read_text()))
            if isinstance(raw, bytes):
                import gzip
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
                raw = raw.decode()
            files = json.loads(raw)["files"]
            blob = json.dumps(files)
            for n in UNAPPROVED:
                self.assertNotIn(n, blob)
                self.assertNotIn("SECRET-" + n[:10], blob)
            for n in APPROVED:
                self.assertIn(f"pod/income_sleeve/{n}", files)
            disk_idx = json.loads((root / "pod/memos_index.json").read_text())
            self.assertFalse([m for m in disk_idx["memos"] if any(n in m["path"] for n in UNAPPROVED)])
            self.assertIn("pod/ceo_decisions/latest_decisions.json", files)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)

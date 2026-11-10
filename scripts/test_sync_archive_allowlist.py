#!/usr/bin/env python3
"""Tests for sync_github_archive.stage_allowlist. Uses a temp git repo with a fake bare
origin and synthetic files only (no network, no real data, no secrets).
Run: python3 scripts/test_sync_archive_allowlist.py   (or SYNC_MODULE=/path/to/sync_github_archive.py)
"""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

MOD_PATH = os.environ.get("SYNC_MODULE") or str(Path(__file__).resolve().parent / "sync_github_archive.py")
_spec = importlib.util.spec_from_file_location("_sync_under_test", MOD_PATH)
sga = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sga)


def g(cwd, *a, check=True):
    r = subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {a}: {r.stderr}")
    return r


class StageAllowlist(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sga_test_"))
        self._env = {k: os.environ.get(k) for k in ("HOME", "GIT_CONFIG_NOSYSTEM", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                                                     "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")}
        os.environ.update({"HOME": str(self.tmp), "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
                           "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
                           "GIT_COMMITTER_EMAIL": "t@example.invalid"})
        self.origin = self.tmp / "origin.git"
        g(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = self.tmp / "repo"
        g(self.tmp, "clone", "-q", str(self.origin), str(self.repo))
        r = self.repo
        for d in ("vault/snapshots/2030/01", "scripts", "pod"):
            (r / d).mkdir(parents=True, exist_ok=True)
        files = {
            "index.html": "<html>gate</html>\n", "vault/state.enc.json": '{"ct":"s0"}\n',
            "vault/manifest.json": '{"v":0}\n', "vault/app.enc.json": '{"ct":"a0"}\n', "vault/vault_meta.json": '{"salt":"x"}\n',
            "vault/snapshots/2030/01/a.json.enc.json": '{"ct":"n0"}\n', "scripts/spy_gamma_monitor.py": "A = 1\n",
            "scripts/sync_github_archive.py": "B = 1\n", "scripts/other.py": "C = 1\n",
            ".gitignore": "pod/\nlatest.json\n/snapshots/\n",
        }
        for p, c in files.items():
            (r / p).write_text(c)
        g(r, "add", "-A"); g(r, "commit", "-q", "-m", "seed"); g(r, "push", "-q", "origin", "HEAD:main")

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stage(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok, staged = sga.stage_allowlist(str(self.repo))
        return ok, staged, buf.getvalue()

    def commit_push(self):
        g(self.repo, "commit", "-q", "-m", "SPY gamma snapshot 2030-01-08 09:21 CT (test)")
        g(self.repo, "push", "-q", "origin", "HEAD:main")

    def origin_files(self):
        return set(g(self.origin, "ls-tree", "-r", "--name-only", "main").stdout.split())

    def test_allowlisted_change_pushed(self):
        (self.repo / "vault/state.enc.json").write_text('{"ct":"s1"}\n')
        (self.repo / "vault/snapshots/2030/01/b.json.enc.json").write_text('{"ct":"n1"}\n')
        (self.repo / "scripts/spy_gamma_monitor.py").write_text("A = 2\n")
        ok, staged, _ = self.stage()
        self.assertTrue(ok)
        self.assertEqual(sorted(staged), ["scripts/spy_gamma_monitor.py", "vault/snapshots/2030/01/b.json.enc.json", "vault/state.enc.json"])
        self.commit_push()
        self.assertIn("vault/snapshots/2030/01/b.json.enc.json", self.origin_files())
        self.assertEqual(g(self.origin, "show", "main:vault/state.enc.json").stdout, '{"ct":"s1"}\n')

    def test_stray_files_not_staged_or_pushed(self):
        (self.repo / "vault/manifest.json").write_text('{"v":1}\n')
        (self.repo / "notes.txt").write_text("scratch\n")                       # untracked stray
        (self.repo / "scripts/._tmp_new.py").write_text("WIP = 1\n")            # the 926fa6d pattern
        (self.repo / "scripts/other.py").write_text("C = 2\n")                  # tracked, not allowlisted
        (self.repo / "pod/private.json").write_text("{}\n")                     # gitignored
        (self.repo / "latest.json").write_text("{}\n")                          # gitignored
        ok, staged, _ = self.stage()
        self.assertTrue(ok); self.assertEqual(staged, ["vault/manifest.json"])
        self.commit_push()
        of = self.origin_files()
        for p in ("notes.txt", "scripts/._tmp_new.py", "pod/private.json", "latest.json"):
            self.assertNotIn(p, of)
        self.assertEqual(g(self.origin, "show", "main:scripts/other.py").stdout, "C = 1\n")
        st = g(self.repo, "status", "--porcelain", "--untracked-files=all").stdout
        self.assertIn("?? notes.txt", st); self.assertIn(" M scripts/other.py", st)

    def test_forced_out_of_allowlist_refused(self):
        (self.repo / "vault/state.enc.json").write_text('{"ct":"s2"}\n')
        (self.repo / "pod/private.json").write_text("{}\n")
        g(self.repo, "add", "-f", "--", "pod/private.json")                     # someone forced it in
        (self.repo / "scripts/other.py").write_text("C = 3\n")
        g(self.repo, "add", "--", "scripts/other.py")
        head = g(self.repo, "rev-parse", "HEAD").stdout
        ok, staged, out = self.stage()
        self.assertFalse(ok); self.assertIn("REFUSED", out)
        self.assertIn("pod/private.json", out)
        self.assertEqual(g(self.repo, "diff", "--cached", "--name-only").stdout, "")  # index emptied
        self.assertEqual(g(self.repo, "rev-parse", "HEAD").stdout, head)
        self.assertNotIn("pod/private.json", self.origin_files())

    def test_no_changes_nothing_staged(self):
        ok, staged, _ = self.stage()
        self.assertTrue(ok); self.assertEqual(staged, [])

    def test_main_flow_has_no_add_all_or_force(self):
        src = Path(MOD_PATH).read_text()
        self.assertNotIn('"add", "-A"', src)
        self.assertNotIn('"add", "-f"', src)
        self.assertIn("stage_allowlist(REPO_DIR)", src)
        self.assertIn('msg = f"SPY gamma snapshot', src)

    def test_allowlist_contents(self):
        for p in ("index.html", "vault/state.enc.json", "vault/snapshots/2030/01/x.enc.json", "scripts/sync_github_archive.py"):
            self.assertTrue(sga.in_publish_allowlist(p), p)
        for p in ("pod/ceo_decisions/latest_decisions.json", "latest.json", "README.md", "snapshots/a.json",
                  "history/timeseries.csv", "docs/x", ".env.vault", "scripts/._alpaca_rth.new.py", "scripts/pod_engine.py"):
            self.assertFalse(sga.in_publish_allowlist(p), p)


if __name__ == "__main__":
    unittest.main(verbosity=2)

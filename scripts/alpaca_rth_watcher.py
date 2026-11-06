#!/usr/bin/env python3
"""Background RTH watcher — at most one instance (fcntl lock). Loops every 30s during RTH.

Repo lock hygiene: this loop itself never touches git or the vault. Each tick spawns a FRESH
`pod_engine.py alpaca-rth-tick` subprocess; that child takes /tmp/horsey_repo_sync.lock
(repo_sync_lock in pod_engine.py / _alpaca_rth.py) around build-state + git add/commit/push and the
CEO-decision pull. The watcher must NOT hold that lock while waiting on the child (it would deadlock),
so a running watcher picks up the lock automatically on its next tick — no restart required.
"""
from __future__ import annotations
import fcntl, os, subprocess, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")
except Exception:
    CT = timezone(timedelta(hours=-6))

ROOT = Path(os.environ.get("HORSEY_ROOT", Path(__file__).resolve().parents[1]))
LOCK = Path("/tmp/horsey_alpaca_rth_watcher.lock")
ENGINE = ROOT / "scripts" / "pod_engine.py"


def now_ct():
    return datetime.now(CT)


def hm(dt):
    return dt.hour, dt.minute


def main():
    now = now_ct()
    # Outside 08:25–15:05 CT weekdays → exit immediately
    if now.weekday() >= 5 or not ((8, 25) <= hm(now) <= (15, 5)):
        print(f"watcher: outside RTH window ({now.isoformat()}) — exit 0")
        return 0

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = open(LOCK, "a+")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("watcher: lock held — another instance running; exit 0")
        fd.close()
        return 0

    fd.seek(0); fd.truncate(); fd.write(f"pid={os.getpid()} started={now.isoformat()}\n"); fd.flush()
    print(f"watcher: acquired lock pid={os.getpid()}")

    try:
        while True:
            now = now_ct()
            if now.weekday() >= 5:
                break
            if hm(now) > (15, 5):
                break
            if hm(now) < (8, 29):
                time.sleep(10)
                continue
            # final tick at/after 15:05 handled below
            cmd = [sys.executable, str(ENGINE), "alpaca-rth-tick", "--push-if-changed"]
            try:
                subprocess.run(cmd, cwd=str(ROOT), check=False, timeout=120)
            except Exception as e:
                print("tick error:", e, file=sys.stderr)
            now = now_ct()
            if hm(now) >= (15, 5):
                break
            time.sleep(30)
        # final
        subprocess.run(
            [sys.executable, str(ENGINE), "alpaca-rth-tick", "--push-if-changed"],
            cwd=str(ROOT), check=False, timeout=120,
        )
        print("watcher: RTH complete — exit 0")
        return 0
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fd.close()


if __name__ == "__main__":
    sys.exit(main() or 0)

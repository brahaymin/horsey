#!/usr/bin/env python3
"""Horsey AES-256-GCM vault builder (CEO password gate).

Subcommands:
  build         Encrypt app (HTML+JS) + market/pod state into vault/*.enc.json
  build-state   Fast rebuild of vault/state.enc.json only
  activate      Copy vault_gate.html → index.html, gitignore plaintext, build vault
                (does NOT git push — caller decides)

Password resolution (never printed):
  1) HORSEY_VAULT_PASSWORD env
  2) /workspace/horsey/.env.vault  (HORSEY_VAULT_PASSWORD=..., mode 0600)

If password missing: build/build-state exit 0 with a skip notice.
"""
from __future__ import annotations

import argparse, base64, gzip, hashlib, json, os, re, shutil, sys, time
from datetime import datetime, timezone
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
except ImportError:
    print("ERROR: cryptography not installed. Run: /workspace/spy_gamma_venv/bin/pip install cryptography", file=sys.stderr)
    sys.exit(2)

ROOT = Path(os.environ.get("HORSEY_ROOT", Path(__file__).resolve().parents[1]))
VAULT_DIR = ROOT / "vault"
META_PATH = VAULT_DIR / "vault_meta.json"
APP_ENC = VAULT_DIR / "app.enc.json"
STATE_ENC = VAULT_DIR / "state.enc.json"
MANIFEST_PATH = VAULT_DIR / "manifest.json"
UNLOCKED = ROOT / "app_unlocked.html"
GATE_SRC = ROOT / "vault_gate.html"
ENV_VAULT = ROOT / ".env.vault"
ITERATIONS = 600_000
SALT_LEN = 16
IV_LEN = 12


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def load_password() -> str | None:
    pw = (os.environ.get("HORSEY_VAULT_PASSWORD") or "").strip()
    if pw:
        return pw
    if ENV_VAULT.exists():
        for line in ENV_VAULT.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "HORSEY_VAULT_PASSWORD":
                return v.strip().strip('"').strip("'") or None
    return None


def ensure_meta() -> dict:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    if META_PATH.exists():
        return json.loads(META_PATH.read_text())
    salt = os.urandom(SALT_LEN)
    meta = {
        "v": 1,
        "alg": "AES-256-GCM",
        "kdf": "PBKDF2-SHA256",
        "iter": ITERATIONS,
        "salt": b64e(salt),
        "created_epoch": int(time.time()),
    }
    META_PATH.write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def derive_key(password: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(password.encode("utf-8"))


def encrypt_blob(password: str, plaintext: bytes, meta: dict | None = None) -> dict:
    meta = meta or ensure_meta()
    salt = b64d(meta["salt"])
    key = derive_key(password, salt, int(meta.get("iter") or ITERATIONS))
    iv = os.urandom(IV_LEN)
    # Gzip plaintext BEFORE AES-GCM (comp:"gzip"); tag still authenticates compressed bytes
    compressed = gzip.compress(plaintext, compresslevel=9)
    ct = AESGCM(key).encrypt(iv, compressed, None)
    return {
        "v": 2,
        "comp": "gzip",
        "alg": "AES-256-GCM",
        "kdf": "PBKDF2-SHA256",
        "iter": int(meta.get("iter") or ITERATIONS),
        "salt": meta["salt"],
        "iv": b64e(iv),
        "ct": b64e(ct),
        "updated_epoch": int(time.time()),
    }


def decrypt_blob(password: str, envelope: dict) -> bytes:
    salt = b64d(envelope["salt"])
    key = derive_key(password, salt, int(envelope.get("iter") or ITERATIONS))
    iv = b64d(envelope["iv"])
    ct = b64d(envelope["ct"])
    plain = AESGCM(key).decrypt(iv, ct, None)
    if envelope.get("comp") == "gzip":
        plain = gzip.decompress(plain)
    return plain


def inline_local_scripts(html: str) -> str:
    """Inline same-origin script src tags so document.write unlock works without network script loads."""
    def repl(m):
        src = m.group(1)
        if src.startswith("http://") or src.startswith("https://") or src.startswith("//"):
            return m.group(0)
        path = (ROOT / src.lstrip("./")).resolve()
        if not str(path).startswith(str(ROOT.resolve())) or not path.exists():
            return m.group(0)
        body = path.read_text(encoding="utf-8")
        return f"<script>\n/* inlined from {src} */\n{body}\n</script>"

    return re.sub(
        r'<script\s+src=["\']([^"\']+)["\']\s*>\s*</script>',
        repl,
        html,
        flags=re.I,
    )


def collect_state_files() -> dict:
    """Build virtual file map for vault/state.enc.json."""
    files: dict = {}

    def add_text(rel: str, path: Path):
        if not path.exists() or not path.is_file():
            return
        try:
            files[rel] = path.read_text(encoding="utf-8")
        except Exception:
            pass

    def add_json(rel: str, path: Path):
        if not path.exists() or not path.is_file():
            return
        try:
            files[rel] = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            add_text(rel, path)

    # Market core
    add_json("latest.json", ROOT / "latest.json")
    add_text("latest.md", ROOT / "latest.md")
    # docs (manuals & playbooks — private inside encrypted vault)
    docs_dir = ROOT / "docs"
    if docs_dir.exists():
        for p in sorted(docs_dir.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(ROOT)).replace(os.sep, "/")
                if p.suffix == ".json":
                    add_json(rel, p)
                else:
                    add_text(rel, p)
    # history (tiny)
    hist = ROOT / "history"
    if hist.exists():
        for p in sorted(hist.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(ROOT)).replace(os.sep, "/")
                if p.suffix == ".json":
                    add_json(rel, p)
                else:
                    add_text(rel, p)
    # snapshots: index + newest ~20 json (+ matching md if small)
    snap_root = ROOT / "snapshots"
    if snap_root.exists():
        add_json("snapshots/index.json", snap_root / "index.json")
        jsons = sorted(snap_root.rglob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        # always keep index separate; take up to 20 content snapshots
        n = 0
        for p in jsons:
            rel = str(p.relative_to(ROOT)).replace(os.sep, "/")
            if rel == "snapshots/index.json":
                continue
            add_json(rel, p)
            md = p.with_suffix(".md")
            if md.exists() and md.stat().st_size < 200_000:
                add_text(str(md.relative_to(ROOT)).replace(os.sep, "/"), md)
            n += 1
            if n >= 20:
                break

    # Pod — everything briefings/pod need
    pod = ROOT / "pod"
    must = [
        "pod/memos_index.json",
        "pod/ceo_questions.json",
        "pod/ceo_decisions/latest_decisions.json",
        "pod/scorecard.json",
        "pod/roster.json",
        "pod/signals/latest_pod_state.json",
        "pod/ledger/paper_blotter.jsonl",
        "pod/ledger/signals_log.jsonl",
        "pod/ledger/signals.jsonl",
        "pod/paper_account_state.json",
        "pod/paper_execution_events.json",
        "pod/reviews/reviews_index.json",
        "pod/risk/limits.json",
        "pod/risk/portfolio_snapshot.json",
        "pod/track_record/weekly_summary.json",
        "pod/track_record/armed_rules_queue.json",
        "pod/track_record/rules_registry.json",
        "pod/alpaca_last_dry_run.json",
        "pod/income_sleeve/holdings_snapshot.json",
        "pod/state.json",  # optional alias some UIs expect
    ]
    for rel in must:
        p = ROOT / rel
        if rel.endswith(".json"):
            add_json(rel, p)
        else:
            add_text(rel, p)

    # Alias pod/state.json ← latest_pod_state if missing
    if "pod/state.json" not in files and "pod/signals/latest_pod_state.json" in files:
        files["pod/state.json"] = files["pod/signals/latest_pod_state.json"]

    # All memos referenced by index + all cio/specialist md/json under pod/
    mi = files.get("pod/memos_index.json")
    if isinstance(mi, dict):
        for m in mi.get("memos") or []:
            path = m.get("path")
            if path:
                add_text(path, ROOT / path) if str(path).endswith(".md") else add_json(path, ROOT / path)
        # latest_cio_briefing
        lcb = mi.get("latest_cio_briefing")
        if lcb:
            add_text(lcb, ROOT / lcb)

    if pod.exists():
        for p in pod.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix not in (".md", ".json", ".jsonl"):
                continue
            rel = str(p.relative_to(ROOT)).replace(os.sep, "/")
            if rel in files:
                continue
            if p.suffix == ".json":
                add_json(rel, p)
            else:
                add_text(rel, p)

    return files


def _content_fingerprint(val) -> str:
    if isinstance(val, (dict, list)):
        blob = json.dumps(val, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    else:
        blob = str(val)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def dedupe_files(files: dict) -> tuple[dict, dict]:
    """Keep one copy of identical payloads; return (canonical_files, aliases).

    aliases maps duplicate_path -> canonical_path. Prefer latest.* and shorter paths.
    """
    def score(path: str) -> tuple:
        prefer = 0
        if path in ("latest.json", "latest.md"):
            prefer -= 200
        if "/latest_" in path or path.endswith("/latest.json") or path.endswith("/latest.md"):
            prefer -= 50
        if path.startswith("pod/signals/latest"):
            prefer -= 40
        return (prefer, len(path), path)

    by_fp: dict[str, str] = {}
    out: dict = {}
    aliases: dict = {}
    for path, val in sorted(files.items(), key=lambda kv: score(kv[0])):
        fp = _content_fingerprint(val)
        if fp in by_fp:
            aliases[path] = by_fp[fp]
        else:
            by_fp[fp] = path
            out[path] = val
    return out, aliases


def write_manifest() -> dict:
    """Public cache-busting hashes only — zero sensitive payload."""
    def short(path: Path) -> str:
        if not path.exists():
            return ""
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]

    man = {
        "app_v": short(APP_ENC),
        "state_v": short(STATE_ENC),
        "updated_epoch": int(time.time()),
    }
    MANIFEST_PATH.write_text(json.dumps(man, indent=2) + "\n")
    return man


def build_app(password: str, meta: dict) -> dict:
    if not UNLOCKED.exists():
        # fallback to current index if unlock source missing
        src = ROOT / "index.html"
        if not src.exists():
            raise SystemExit("ERROR: app_unlocked.html (and index.html) missing")
        html = src.read_text(encoding="utf-8")
    else:
        html = UNLOCKED.read_text(encoding="utf-8")
    html = inline_local_scripts(html)
    # Inject lock button marker hook (gate also injects; this is a backup id slot)
    if 'id="vaultLockBtn"' not in html:
        html = html.replace(
            '<button class="bellbtn" id="bellBtn"',
            '<button type="button" class="bellbtn" id="vaultLockBtn" title="Lock vault" aria-label="Lock">🔒</button>\n <button class="bellbtn" id="bellBtn"',
            1,
        )
    feed = ""
    fa = ROOT / "js" / "feed-adapter.js"
    if fa.exists():
        feed = fa.read_text(encoding="utf-8")
    payload = {
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "html": html,
        "files": {"js/feed-adapter.js": feed},
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    env = encrypt_blob(password, raw, meta)
    APP_ENC.write_text(json.dumps(env, separators=(",", ":")) + "\n")
    return {"path": str(APP_ENC), "plain_bytes": len(raw), "enc_bytes": APP_ENC.stat().st_size}


def build_state(password: str, meta: dict) -> dict:
    files = collect_state_files()
    canonical, aliases = dedupe_files(files)
    payload = {
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": canonical,
        "aliases": aliases,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    env = encrypt_blob(password, raw, meta)
    STATE_ENC.write_text(json.dumps(env, separators=(",", ":")) + "\n")
    return {
        "path": str(STATE_ENC),
        "file_count": len(files),
        "canonical_count": len(canonical),
        "alias_count": len(aliases),
        "plain_bytes": len(raw),
        "enc_bytes": STATE_ENC.stat().st_size,
        "sample_keys": sorted(canonical.keys())[:12],
    }


def cmd_build(args):
    pw = load_password()
    if not pw:
        print("Vault password not configured yet; skipping vault encryption")
        return 0
    meta = ensure_meta()
    app = build_app(pw, meta)
    st = build_state(pw, meta)
    man = write_manifest()
    print(json.dumps({"ok": True, "app": app, "state": st, "manifest": man, "meta": {"salt": meta["salt"][:8] + "…", "iter": meta["iter"]}}, indent=2))
    return 0


def cmd_build_state(args):
    pw = load_password()
    if not pw:
        print("Vault password not configured yet; skipping vault encryption")
        return 0
    meta = ensure_meta()
    st = build_state(pw, meta)
    man = write_manifest()
    print(json.dumps({"ok": True, "state": st, "manifest": man}, indent=2))
    return 0


GITIGNORE_EXTRA = """
# Vault plaintext sources (public archive serves encrypted vault/ + gate only)
app_unlocked.html
vault_gate.html
.env.vault
js/
latest.json
latest.md
history/
snapshots/
docs/
README.md
"""


def cmd_activate(args):
    """Prepare public surface: gate as index.html, ignore plaintext, build vault. No git push."""
    pw = load_password()
    if not pw:
        # Optionally persist from env if provided this invocation
        print("ERROR: set HORSEY_VAULT_PASSWORD (or .env.vault) before activate", file=sys.stderr)
        return 2
    # Write .env.vault 0600 if activating from env
    if not ENV_VAULT.exists() or args.write_env:
        ENV_VAULT.write_text(f"HORSEY_VAULT_PASSWORD={pw}\n")
        os.chmod(ENV_VAULT, 0o600)
        print("wrote .env.vault (0600)")
    if not GATE_SRC.exists():
        print("ERROR: vault_gate.html missing", file=sys.stderr)
        return 2
    if not UNLOCKED.exists():
        # ensure master copy exists
        shutil.copy2(ROOT / "index.html", UNLOCKED)
        print("copied index.html → app_unlocked.html")
    # Gate becomes public index
    shutil.copy2(GATE_SRC, ROOT / "index.html")
    print("copied vault_gate.html → index.html")

    # Extend gitignore (idempotent)
    gi = ROOT / ".gitignore"
    cur = gi.read_text() if gi.exists() else ""
    for line in GITIGNORE_EXTRA.strip().splitlines():
        if line and line not in cur:
            cur += ("\n" if cur and not cur.endswith("\n") else "") + line + "\n"
    # Always keep vault/ tracked (do not ignore vault/)
    if "vault/" in cur.splitlines():
        # remove accidental vault/ ignore
        cur = "\n".join(ln for ln in cur.splitlines() if ln.strip() != "vault/") + "\n"
    gi.write_text(cur)
    print("updated .gitignore")

    # git rm --cached plaintext (keep working tree!)
    import subprocess
    patterns = [
        "app_unlocked.html", "vault_gate.html", "js", "latest.json", "latest.md",
        "history", "snapshots", "docs", "README.md",
    ]
    for pat in patterns:
        subprocess.run(
            ["git", "rm", "-r", "--cached", "--ignore-unmatch", "--", pat],
            cwd=str(ROOT), check=False, capture_output=True, text=True,
        )
    # Ensure vault + gate index are addable
    subprocess.run(["git", "add", "-f", "--", "index.html", "vault/app.enc.json", "vault/state.enc.json", "vault/vault_meta.json", "vault/manifest.json", ".gitignore", ".nojekyll"], cwd=str(ROOT), check=False)

    meta = ensure_meta()
    app = build_app(pw, meta)
    st = build_state(pw, meta)
    man = write_manifest()
    # Verify working tree intact
    intact = all((ROOT / x).exists() for x in ["app_unlocked.html", "js/feed-adapter.js", "latest.json", "pod/memos_index.json"])
    print(json.dumps({
        "ok": True,
        "working_tree_intact": intact,
        "app": app,
        "state": st,
        "manifest": man,
        "note": "Ready to commit+push when you choose. No push performed by activate.",
    }, indent=2))
    return 0 if intact else 1


def cmd_verify(args):
    """Decrypt round-trip check (local only)."""
    pw = load_password()
    if not pw:
        print("Vault password not configured yet; skipping")
        return 0
    if not APP_ENC.exists() or not STATE_ENC.exists():
        print("ERROR: encrypted vault missing — run build first", file=sys.stderr)
        return 2
    app = json.loads(decrypt_blob(pw, json.loads(APP_ENC.read_text())))
    st = json.loads(decrypt_blob(pw, json.loads(STATE_ENC.read_text())))
    print(json.dumps({
        "ok": True,
        "html_len": len(app.get("html") or ""),
        "app_files": list((app.get("files") or {}).keys()),
        "state_files": len(st.get("files") or {}),
        "state_aliases": len(st.get("aliases") or {}),
        "has_latest": "latest.json" in (st.get("files") or {}),
        "has_memos": "pod/memos_index.json" in (st.get("files") or {}),
        "has_blotter": "pod/ledger/paper_blotter.jsonl" in (st.get("files") or {}),
    }, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("build")
    sp.add_parser("build-state")
    sp.add_parser("verify")
    act = sp.add_parser("activate")
    act.add_argument("--write-env", action="store_true", help="Force rewrite .env.vault from env password")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        return cmd_build(a)
    if a.cmd == "build-state":
        return cmd_build_state(a)
    if a.cmd == "activate":
        return cmd_activate(a)
    if a.cmd == "verify":
        return cmd_verify(a)
    return 2


if __name__ == "__main__":
    sys.exit(main())

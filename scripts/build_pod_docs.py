#!/usr/bin/env python3
"""Build the encrypted dashboard's "Pod Docs" library (docs/pod_docs/, gitignored, vault-only).

Generated from sources each run:  the joint level-selection framework (display copy) and the CEO
pre-market template (rendered as markdown). Authored in place (platform-maintained): architecture,
roster, runbook and changelog. Every doc gets a plain-English display normalization (no file paths,
snake_case identifiers or UUIDs) and must pass a hygiene scan (no PII, account numbers, balances,
positions, Income Sleeve or family data, secrets). Any finding => exit 2 and the index is NOT
rewritten, so a failing doc never reaches the vault. Plaintext never goes to git (docs/ is ignored);
only vault/** carries it, encrypted.
Run: /workspace/spy_gamma_venv/bin/python scripts/build_pod_docs.py [--check]
"""
import json, os, re, sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("HORSEY_ROOT", Path(__file__).resolve().parents[1]))
OUT = ROOT / "docs" / "pod_docs"
CT = ZoneInfo("America/Chicago")
FRAMEWORK_SRC = ROOT / "pod" / "intraday_tape_gamma" / "2026-11-12_joint_level_selection_framework.md"
TEMPLATE_SRC = ROOT / "pod" / "playbooks" / "ceo_legacy_premarket_template.txt"
# (output file, title, kind) -- order is the reader's order
DOCS = [
    ("framework.md", "Joint Level-Selection Framework (v1.3)", "generated"),
    ("premarket_template.md", "CEO Pre-Market Template", "generated"),
    ("architecture.md", "Pod Architecture", "authored"),
    ("roster.md", "Pod Roster", "authored"),
    ("runbook.md", "Daily Runbook", "authored"),
    ("changelog.md", "Changelog", "authored"),
]
SPECIFIC = [  # exact display replacements (applied before the generic rules)
    (r"`latest\.json → intraday_30m`", "the snapshot's 30-minute intraday table"),
    (r"`latest\.json`", "the latest snapshot"), (r"`rvol_est`", "the watcher's old RVOL estimate"),
    (r"`rvol_20d`", "the old 20-day RVOL"), (r"`tod_rvol`", "time-of-day RVOL"), (r"`next_session`", "today's-expiry scope"),
    (r"`gex_scopes`", "GEX scopes"), (r"`blocked_move`", "blocked move"), (r"`history/timeseries\.csv`", "the snapshot history"),
    (r"`evidence/`", "the evidence folder"), (r"`interval=5m&range=60d`", "5-minute bars over 60 days"),
    (r"`pod/playbooks/ceo_legacy_premarket_template\.txt`", "the CEO pre-market template"),
    (r"`2026-11-13_scenario_card_SPY_options\.md`", "Friday's SPY options scenario card"),
    (r"`2026-11-12_key_level_mechanics_proposal\.md`(/`\.json`)?", "the key-level mechanics proposal"),
]
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
SNAKE_RE = re.compile(r"\b[a-z]+_[a-z0-9_]+\b")
PATH_RE = re.compile(r"(/workspace|/home/|/tmp/|/cursor/|\b(pod|docs|history|snapshots|scripts|vault)/[\w./-]*|\b[\w-]+\.(md|json|jsonl|py|csv|txt|html|lock)\b)")
SCAN = {
    "masked or full account number": re.compile(r"(•{2,}|\*{3,})\s?\d{2,}|\baccount\s*(number|no\.?|#)\s*[:#]?\s*\w*\d{3,}", re.I),
    "balance or account value": re.compile(r"\b(balance|portfolio value|account value|buying power|cash balance|net worth)\b[^\n]{0,30}\$\s?\d", re.I),
    "positions or holdings": re.compile(r"\b(holdings snapshot|cost basis|unrealized p&?l|\d+(\.\d+)? shares of [A-Z]{1,5})\b", re.I),
    "income sleeve / brokerage": re.compile(r"\b(income sleeve|robinhood|drip|dividend reinvest\w*|ex-dividend|margin apr)\b", re.I),
    "family / personal": re.compile(r"\b(family|spouse|wife|husband|children|kids|student[- ]debt|student loan|mortgage|ssn|social security|date of birth|home address)\b", re.I),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}"),
    "phone": re.compile(r"\(?\b\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b"),
    "secret": re.compile(r"(ghp_\w{10,}|github_pat_\w{10,}|\bPK[A-Z0-9]{16,}\b|\bAK[A-Z0-9]{16,}\b|sk-[A-Za-z0-9]{16,}|APCA_\w+|HORSEY_VAULT_PASSWORD|PRIVATE KEY|password\s*[:=])", re.I),
    "unfinished placeholder": re.compile(r"PLACEHOLDER"),
    "uuid": UUID_RE, "snake_case label": SNAKE_RE, "file path or file name": PATH_RE,
}


def humanize_token(tok):
    t = re.sub(r"^.*/", "", tok.rstrip("/"))
    t = re.sub(r"\.(md|json|jsonl|py|csv|txt|html|lock)$", "", t)
    t = re.sub(r"^\d{4}-\d{2}-\d{2}_?(\d{4}-ct-)?", "", t)
    return t.replace("_", " ").replace("-", " ").strip() or "file"


def normalize(md):
    s = md
    for a, b in SPECIFIC:
        s = re.sub(a, b, s)
    s = UUID_RE.sub("", s)
    s = re.sub(r"`([^`\n]+)`", lambda m: humanize_token(m.group(1)) if (PATH_RE.search(m.group(1)) or SNAKE_RE.search(m.group(1))) else m.group(0), s)
    s = re.sub(r"(?<![\w/])(?:/workspace|/home|/tmp|/cursor)[\w./-]*", "", s)
    s = re.sub(r"\b(?:pod|docs|history|snapshots|scripts|vault)/[\w./-]+", lambda m: humanize_token(m.group(0)), s)
    s = re.sub(r"\b[\w-]+\.(md|json|jsonl|py|csv|txt|html|lock)\b", lambda m: humanize_token(m.group(0)), s)
    s = SNAKE_RE.sub(lambda m: m.group(0).replace("_", " "), s)
    return s


def scan(text):
    out = {}
    for name, rx in SCAN.items():
        n = len(rx.findall(text))
        if n:
            out[name] = n
    return out


def template_to_md(txt):
    lines = ["# CEO Pre-Market Template", "", "*The CEO's legacy pre-market checklist, kept as the reference for the morning flow. Trade scenarios and opening-bias translation are the CIO's; the snapshot supplies data only.*", ""]
    for raw in txt.splitlines():
        line = raw.rstrip()
        if not line.strip():
            lines.append(""); continue
        depth = len(line) - len(line.lstrip("\t ")) if line[:1] in "\t " else 0
        body = line.strip()
        if body.startswith("•"):
            body = body.lstrip("•").strip()
            lines.append(("  " if depth else "") + f"- {body}")
        elif body.startswith("-"):
            lines.append("  " * max(1, depth // 2) + body)
        elif re.match(r"^\d+\.", body):
            lines.append("   " + body)
        else:
            lines.append(f"## {body}" if not depth else f"  {body}")
    return "\n".join(lines) + "\n"


def build(check_only=False):
    OUT.mkdir(parents=True, exist_ok=True)
    gen = {}
    if FRAMEWORK_SRC.exists():
        head = "*Display copy of the desk's joint framework (PTJ Macro and Intraday Tape). File names and code identifiers are shown in plain English.*\n\n"
        gen["framework.md"] = head + normalize(FRAMEWORK_SRC.read_text(encoding="utf-8"))
    if TEMPLATE_SRC.exists():
        gen["premarket_template.md"] = normalize(template_to_md(TEMPLATE_SRC.read_text(encoding="utf-8")))
    texts, report, missing = {}, {}, []
    for fn, title, kind in DOCS:
        if kind == "generated":
            t = gen.get(fn)
        else:
            p = OUT / fn
            t = normalize(p.read_text(encoding="utf-8")) if p.exists() else None
        if t is None:
            missing.append(fn); continue
        texts[fn] = t
        report[fn] = scan(t)
    bad = {k: v for k, v in report.items() if v}
    summary = {"docs": len(texts), "missing": missing, "findings": bad}
    if bad or check_only:
        print(json.dumps(summary, indent=1))
        return 2 if bad else 0
    for fn, t in texts.items():
        tmp = OUT / f".{fn}.tmp"; tmp.write_text(t, encoding="utf-8"); os.replace(tmp, OUT / fn)
    idx = {"title": "Pod Docs", "updated_ct": datetime.now(timezone.utc).astimezone(CT).strftime("%b %-d, %Y %-I:%M %p CT"),
           "docs": [{"id": fn[:-3].replace("_", "-"), "file": fn, "title": title} for fn, title, _ in DOCS if fn in texts],
           "hygiene": "passed"}
    tmp = OUT / ".index.json.tmp"; tmp.write_text(json.dumps(idx, indent=1), encoding="utf-8"); os.replace(tmp, OUT / "index.json")
    print(json.dumps(dict(summary, written=list(texts)), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(build("--check" in sys.argv))

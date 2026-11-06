# Horsey Multi-Agent Trading Pod — Signal Drop Directory

Shared output directory for specialist strategists. Consumed by the **CIO & Risk Gatekeeper**.

## Separation of duties
| Agent | Mandate | Output prefix |
|---|---|---|
| SPY Gamma Structure Monitor | Verified data only (`../latest.json`) — no trade ideas | *(upstream data, not this folder)* |
| PTJ Macro & Regime Strategist | 200-SMA, vol term structure, ≥5:1 macro inflection | `ptj_macro/` |
| **Intraday Tape & Gamma Tactician** | Schwartz / Raschke / B·N·F / Cohen 0DTE–5DTE | `intraday_tape_gamma/` |
| Buffett Value Compounder | Moat, FCF, 25–35% margin of safety | `buffett_value/` |
| CIO & Risk Gatekeeper | Firewall, sizing, scorecard, order tickets | `cio/`, `scorecard.json`, tickets via `../scripts/pod_engine.py` |

## File conventions
1. **Per-call memo (human):** `<agent_dir>/<session_date>_<HHMM>-ct-<run_tag>_thesis.md`
2. **Per-call structured signal (machine):** same stem with `.json`
3. **Append-only ledger:** `ledger/signals.jsonl` — one JSON object per line (all specialists)
4. **Calibration:** `scorecard.json` — owned by CIO (do not invent weights)

## Required signal JSON fields (specialists)
`schema_version`, `agent`, `agent_id`, `written_at_ct`, `session_date`, `market_session_state`,
`source_snapshot` (path + timestamp_ct + run_tag), `regime_tags`, `conviction_0_100`,
`four_legend_synthesis` *(tactician)* / mandate-specific block, `setups[]` each with:
`id`, `bias`, `instruments`, `trigger`, `hard_invalidation`, `time_stop`, `targets_R` (T1/T2/T3),
`r_multiple_min`, `sizing_note`, `mistakes_guardrails`, `status` (`active_watch`|`conditional`|`stand_down`).

## Pre-trade firewall (CIO enforces; specialists must pre-satisfy)
1. Regime alignment  2. Hard invalidation  3. ≥3:1 tactical / ≥5:1 macro asymmetry
4. Fixed fractional risk (≤0.5% default, 1% max; half size in extreme backwardation)
5. Pre-computed bracket  6. Human CEO confirmation before live send

All times America/Chicago (CT) unless labeled otherwise.

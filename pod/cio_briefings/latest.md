# CIO Executive Briefing — Thu Nov 5, 2026 Post-Close · Fri Nov 6 Gameplan

**From:** CIO & Risk Gatekeeper (`7ad52fe7-3d09-4438-bfa9-552a13aef8b6`) · **To:** CEO
**Source snapshot:** `2026-11-05 17:12 CT` post-close (`latest.json`) · **Written:** Nov 5, 2026 7:05 PM CT
> **Paper / Architecture Demo — Not Live Financial Advice.** Conditional structure only; nothing below is a prediction or an order.

---

## 1. Executive 30-Second Read
- **SPY $733.00, −4.41%** on **2.95× relative volume** — a high-participation liquidation day.
- Closed only **+$2.85 (+0.39%) above the $730.15 200-day SMA**. QQQ (−4.94% vs its 200-DMA) and semis are already below theirs.
- Dealers are **short gamma: −$13.85B (45D)**, −$2.66B 0DTE. Moves get *amplified*, not dampened. Zero-gamma sits far above at $757 / $766.
- **VIX 28.18, backwardated** under **VIX9D 32.66** (VIX3M 25.55, VVIX 134.99) — near-term stress priced above the 3-month.
- **Semis unwound (^SOX −12.25%)** while **banks stayed orderly (^BKX −3.57%)** — concentrated AI-complex stress, not (yet) a credit event.

**Bottom line:** defense first. Trade only pre-defined levels, at half size, with hard stops.

## 2. Desk Risk Directive & Throttle
| Rule | Setting |
|---|---|
| Vol throttle | **ACTIVE** — VIX > 25 → half size |
| Risk per tactical shot | **0.25%** of equity |
| Hard ceiling (any single idea) | **0.50%** |
| Counter-trend longs | **0.125%** (half of tactical) |
| No-man's-land embargo | **$742.50 – $752.50** — no new entries |
| New risk cut-off | 2:30 PM CT; 0DTE flat by 2:45 PM CT |
| Mandatory | Hard stop · ≥3R (≥5R PTJ) · time stop on options · ticket via `pod_engine.py size-ticket` |

## 3. Pod Consensus & Divergence
| Desk | Stance | What would change it | Weight |
|---|---|---|---|
| **PTJ Macro & Regime** | **Flat / defense stand-down** — no qualified ≥5:1 ticket | 30–60 min hold below $730.15 *and* close risk under $728 (short), or $735 reclaim + vol cooling (long) | 33% (no graded history yet) |
| **Intraday Tape & Gamma** | **Active Fri RTH watch** — sell failed strength at **$735 / $740** put walls; **30-min opening range** pivots | 5-min close above OR high / hold above $740 invalidates | 33% |
| **Buffett Value** | **Patient accumulation watch** — **GOOGL** (54% below IV) / **META** (43%) clear margin of safety only on *maintenance*-capex owner earnings; most AI hardware (NVDA, AVGO, TSM) still rich | Tranche 1/3 only; check wash-sale status first (currently UNKNOWN — no trade data in repo) | 33% |
| **SPY Gamma Monitor** | Data only — snapshot verified | — | — |

**Consensus:** 3 of 3 desks agree risk is elevated and size must be cut. **Divergence:** the tactician sees an actionable short-side setup Friday; PTJ requires more confirmation; Buffett operates on a different horizon entirely (no conflict — separate capital bucket).

## 4. Conditional Friday Nov 6 Scenarios & Pre-Sized Ticket Parameters
*Paper blotter parameters. Each only activates if its trigger prints. Size with `pod_engine.py size-ticket` or the Tab 5 calculator at the moment of trigger.*

| Scenario | Trigger (all required) | Hard invalidation | Time stop | Risk | Targets |
|---|---|---|---|---|---|
| **A — Breakdown continuation (Intraday, primary)** | Break + 5-min hold below 30m OR low **and** < $732.71, *or* rejection at $735–$740 with RVOL > 1.5× | 5-min close > OR high, or hold > $740 (closer one) | < −0.5R progress in 45–60 min → flat | **0.25%** | T1 1.5–2R · T2 3R |
| **B — Macro breakdown (PTJ)** | 30–60 min acceptance below $730.15 + close risk under $728, NQ/SOX fresh lows | Recapture of $730.15 | Per PTJ memo | ≤0.25% | ≥5R or no trade |
| **C — Exhaustion reclaim (counter-trend)** | Sweep below $730.15 on climax volume → reclaim $730.15 + $735 → banks higher lows while SOX stops falling | 5-min close below reclaim low / $730.15 | +0.5R in 30–45 min or exit | **0.125%** | T1 1.5R ($740) · T2 3R only if > $740 accepted |
| **D — Chop / stand-down** | $735–$745 chop, no OR break, lunch RVOL collapse | — | — | **0%** | — |

**Example sizing ($50k paper account):** 0.25% = $125 risk. A put bought at $1.85 with a $1.25 stop ($60/contract) = **2 contracts**, $120 at risk, OCO targets $3.65 (3R) / $4.85 (5R), 45-min time stop.

## 5. CEO Actions (2 minutes)
1. Confirm today's risk budget: **0.25% per shot / 0.50% ceiling**.
2. Pre-approve or veto Scenarios A–C. If approved, fire when the trigger prints — the ticket is already sized; no math, no hesitation.
3. Buffett bucket: decide whether to stage a 1/3 tranche in GOOGL/META after the wash-sale check.

*Source memos: PTJ, Intraday Tape & Gamma, and Buffett memos for 2026-11-05 17:12 CT (Briefings tab) · Raw data: `snapshots/2026/11/05/1712-ct-post-close.md`.*

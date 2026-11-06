# Intraday Tape & Gamma Tactician — Thesis Memo

| Field | Value |
|---|---|
| Agent | Intraday Tape & Gamma Tactician (`ee33b632-6c42-4d43-bf17-665bc939db17`) |
| Lens | Marty Schwartz · Linda Raschke · Takashi Kotegawa (B·N·F) · Steven A. Cohen |
| Written (CT) | **2026-11-05 18:40 CT** (America/Chicago) |
| Market state | **CLOSED / AFTER-HOURS** — RTH ended; Globex open |
| Source snapshot | `/workspace/horsey/latest.json` · **2026-11-05T17:12:12-0600** · `run_tag=post-close` |
| Next RTH | **Friday 2026-11-06** (Nov 6 SPY expiry = **0DTE** at open) |
| Companion JSON | `2026-11-05_1712-ct-post-close_thesis.json` |
| Ledger | `/workspace/horsey/pod/ledger/signals.jsonl` |

> For CIO & Risk Gatekeeper only. Not investment advice. Numbers below are verified from `latest.json` / `latest.md` — none fabricated. GEX is an OI-side dealer proxy, not observed inventory.

---

## 0. Session-state framing (critical)

Equities are **closed**. This memo is a **pre-Friday RTH playbook**. Triggers reference Friday Nov 6 opening-range pivots and overnight Globex context from the snapshot (ES/NQ quotes as of **17:02 CT**, early Globex — **not** live fills at memo time). Do not stage overnight entries from stale delayed prints.

---

## 1. Verified metrics (exact from snapshot)

### SPY / trend
| Metric | Value |
|---|---:|
| Regular close / prior close | **733.00** / 766.81 |
| Day change | **−33.81 (−4.4092%)** |
| Open / High / Low | 765.47 / 765.97 / **732.71** |
| AH indication (`current_price`) | **731.15** |
| Volume / RVOL(20) | 120,886,017 / **2.9539×** |
| SMA20 / SMA50 / SMA200 | 765.5275 / 766.396 / **730.151** |
| Dist. to SMA200 | **+$2.849 (+0.3902%)** |
| Approx 10d EMA (from 16 available daily closes) | **~752.63** → price **below** → Schwartz **RED LIGHT** |

### Net GEX (all scopes **NEGATIVE / short-gamma**)
| Scope | Net GEX ($bn / 1%) | Call wall | Put wall | Model zero-γ |
|---|---:|---:|---:|---:|
| Nov 6 (→ Friday 0DTE) | **−2.660** | **760** | **735** | **757.19** |
| Nov 20 OPEX | **−6.383** | 800 | 740 | 782.27 |
| ≤45 DTE | **−13.851** | **770** | **740** | **765.98** |
| All upcoming | **−16.106** | 800 | 740 | 765.97 |

- **No cumulative-strike gamma flip** in any scope (literal definition). Model zero-γ is a **zone**, not a pin.
- **$735 node (Nov 6):** put OI **23,652** vs call OI **387**; net GEX **−$367.1m / 1%** → **primary put-gamma wall** for Friday.

### Volatility term structure
| Index | Close | 1D % |
|---|---:|---:|
| VIX9D | **32.66** | +115.01% |
| VIX | **28.18** | +69.66% |
| VIX3M | **25.55** | +35.90% |
| VVIX | **134.99** | +49.31% |

**Ordering:** VIX9D > VIX > VIX3M → **`backwardated_implied_volatility_tenors`**. Stress / expansion regime (spot IV horizons, not a futures curve).

### AI/Semis vs Banks (Cohen sector board)
| Symbol | Close | 1D % | vs SPY (pp) |
|---|---:|---:|---:|
| SPY | 733.00 | −4.41 | 0 |
| ^SOX | 9011.14 | **−12.25** | −7.84 |
| SMH | 447.73 | −11.21 | −6.80 |
| NVDA | 181.60 | **−9.26** | −4.85 |
| ^BKX | 171.21 | −3.57 | **+0.84** |
| KBWB | 88.03 | −3.54 | +0.87 |
| MSFT | 535.93 | −3.58 | +0.83 |

### Futures (snapshot early Globex only)
| | Quote @ 17:02 CT | RTH last | Overnight range (partial) |
|---|---:|---:|---|
| ES | 7364.75 | 7353.00 | 7351.50–7373.00 |
| NQ | 26667.25 | 26604.00 | 26584–26717 |

### Verified catalysts
1. CNBC — Democrats call to rein in AI buildout (proposals/pledges, **not** enacted moratorium).
2. Investing.com/Reuters — AI/data-center permitting/grid risk after midterms.

---

## 2. Regime classification

**Tag:** `short_gamma_high_rvol_trend_expansion` + `schwartz_red_light` + `vix_backwardation` + `knife_edge_200sma`

| Lens | Read |
|---|---|
| **GEX** | Negative all scopes → hedging **amplifies** moves. Playbook = trend / failure-at-walls, **not** fade-the-open MR. |
| **RVOL** | 2.95× → respect trend (mistake #14). |
| **Schwartz** | RED LIGHT below 10d EMA / 20 / 50. Barely above 200 SMA. **No bottom fishing.** |
| **Raschke** | Expansion day confirmed. Prior bar 80-20-like (open top, close bottom). Friday = **OR pivots + time stops**. |
| **B·N·F** | Semis extremely extended vs MAs — snapback *risk* exists but is **conditional** only after climax + reclaim. |
| **Cohen 40/30/30** | Market β bearish; sector = AI crash / banks relative hold; stock = MSFT > NVDA. |

**Primary Friday bias:** respect downside / sell failed strength. **Do not** fade high-RVOL short-gamma without Setup B exhaustion checklist.

**Conviction:** regime **92**/100 · Setup A **68** · Setup B **28** · actionable overall **70**.

---

## 3. Setups (exact trigger / invalidation / time stop / R)

### Setup A — `ITT-2026-11-06-A` · SHORT · `active_watch` · PRIMARY
- **Instruments:** ES/MES; SPY Nov 6 0DTE or Nov 7 1DTE puts (liquid ATM/near); QQQ only if NQ leads (same beta bucket).
- **Trigger (Fri RTH):** Wait for **first 30-min Opening Range**. Enter short on (1) break + 5-min hold **below OR_low AND below 732.71**, **or** (2) rally into **735–740** / OR_high that **rejects** (close back below mid-OR) with early RVOL still >1.5×. Prefer NQ/SOX/NVDA confirmation.
- **Hard invalidation:** 5-min close back above Friday **OR_high**, **or** sustained hold above **740** — use the closer uncle-point. **No averaging down.**
- **Time stop:** Flatten if < **−0.5R** progress in **45–60 min**. No new risk after **14:30 CT**. 0DTE flat by **14:45 CT** unless T1 runner locked.
- **Targets:** T1 **1.5–2.0R** · T2 **3.0R** · runner only if 730 fails with accelerating RVOL (trail; never widen).
- **Min R:** **3.0** · **Size:** **0.25%** equity risk (half of 0.5% — extreme backwardation/VVIX).

### Setup B — `ITT-2026-11-06-B` · LONG · `conditional` ONLY
- **Trigger:** (1) sweep below **730.15** and/or **732.71** on climax volume, **then** (2) reclaim/hold **730.15 + 735**, **and** (3) banks higher-lows while SOX/NVDA stop cutting lows. **All three required.**
- **Hard invalidation:** 5-min close back below reclaim-candle low **or** below **730.15**.
- **Time stop:** **+0.5R within 30–45 min** or exit. No lunch hope holds. 0DTE longs flat **14:30 CT**.
- **Targets:** T1 **1.5R** (mid-OR / 740) · T2 **3.0R** only if accept >740 · do **not** base-case chase 760.
- **Size:** **0.125%** equity (half of A). Counter-regime.

### Setup C — `ITT-2026-11-06-C` · FLAT · `stand_down`
Chop **735–745** with no OR break/rejection, or lunch RVOL collapse → **zero risk** (mistakes #25/#27/#32).

---

## 4. Key rules applied (from methodologies + 50 mistakes)

Avoided / enforced: **#1** no average-down · **#3** uncle-point pre-defined · **#6** one beta book · **#10** 0DTE premium sizing + time stop · **#11/#14** never fade high-RVOL short-gamma as MR · **#12** 200 SMA marked · **#13** VIX backwardation / VVIX · **#16** zero-γ as zone · **#18** expansion day classified · **#23** wait for OR · **#24** overnight + prior RTH marked · **#25** no mid-range · **#29** no bottom fishing · **#30** mechanical time stops · **#43** verified headlines only.

Schwartz: RED LIGHT + scale down after violent day. Raschke: OR pivots + time stops. B·N·F: extension noted, snapback gated. Cohen: 40/30/30 decomposition + take-in-half.

---

## 5. CIO handoff

1. Weight this lens for **Friday 0DTE–5DTE / ES tactical**; defer multi-day SMA200 inflection sizing to **PTJ Macro (≥5:1)**.
2. Enforce **half-size** under VIX9D>VIX>VIX3M and VVIX 135.
3. **Reject** any ticket that fades the breakdown without Setup B’s three-part checklist.
4. `scorecard.json` / `pod_engine.py` **not present yet** — exact contract counts deferred to CIO + CEO equity; this memo supplies structure for brackets.
5. **Human CEO confirmation required** before any live send.


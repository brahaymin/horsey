# PTJ Macro & Regime Thesis — 2026-11-05 post-close

**Agent:** PTJ Macro & Regime Strategist (`05895e3e-1c82-4cec-9ff3-07aa73b5c0db`)  
**To:** CIO & Risk Gatekeeper (`7ad52fe7-3d09-4438-bfa9-552a13aef8b6`)  
**Written (America/Chicago):** `2026-11-05T18:44:37-0600`  
**Source snapshot CT / UTC:** `2026-11-05T17:12:12-0600` / `2026-11-05T23:12:12.899191+00:00`  
**Primary bias:** **FLAT — DEFENSE / STAND-DOWN**  
**Conviction:** regime defense **88** / actionable now **15** / stand-down **85**

---

## One-line verdict
SPY is **knife-edge above the 200-DMA** while QQQ/SOX are already below theirs; VIX tenors are **backwardated** and dealer-gamma proxy is **deeply short**. **No qualified >=5:1 macro ticket with tight invalidation right now.** Stay flat; cut size.

## Verified levels & stress (from `latest.json` / `timeseries.jsonl`)
- **SPY** RTH close **733.00**, AH indication **731.15** (-4.4092% d1), RVOL **2.9539x**
- **SMA20 / SMA50 / SMA200:** **765.5275 / 766.396 / 730.151** -> vs 200-DMA **+$2.849 (+0.3902%)**
- **QQQ** 647.05 (-6.4889%), vs 200-DMA **-4.9447%** | **IWM** not in snapshot (RTY=F **2728.8**)
- **GEX** 0DTE / 45D / All: **-$2.66B / -$13.85B / -$16.11B** | walls 0DTE **760/735**, 45D **770/740** | zero-g **757.19 / 765.98**
- **Vol:** VIX9D **32.66** > VIX **28.18** > VIX3M **25.55**; VVIX **134.99**
- **^SOX** -12.2461% (vs 200-DMA -16.75%) | **^BKX** 171.21 (-3.5673%, vs 200-DMA -1.11%)
- **ES/NQ:** 7364.75 / 26667.25 | credit/rates/dollar: **not in snapshot** | pod PnL: **not tracked**
- **Catalysts:** CNBC/Reuters midterms AI & data-center restriction/pledge coverage (proposals, not enacted national moratorium)

## Five PTJ rules
1. **200-DMA:** DEFENSE ALERT — SPY barely above; leadership already below.
2. **>=5:1 asymmetry:** FAIL for immediate ticket — Setups A/B are conditional watches only.
3. **Vol & cross-asset stress:** EXTREME (backwardation + VVIX spike + semis collapse + policy shock).
4. **MTM honesty:** No horsey positions found; cut any CEO discretionary losers — never average down.
5. **Exposure scaling:** Flat / minimal; **0.25%** max risk if a later ticket clears geometry.

## Setups logged
| ID | Status | Bias |
|---|---|---|
| PTJ-2026-11-05-STANDDOWN | stand_down | flat |
| PTJ-2026-11-06-A | conditional | short after confirmed hold <730.151/728 |
| PTJ-2026-11-06-B | conditional | long only after 735 reclaim + vol cooling |

## CIO handoff
File-bus delivery only (`pod/ptj_macro/...` + `pod/ledger/signals.jsonl`). **Do not require a direct chat ping** for architecture compliance; CIO ingests the bus. No `pod_engine.py size-ticket` until a trigger prints with recomputed >=5:1.

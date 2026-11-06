# CIO Execution & Risk Directive — CEO Decisions Applied · Thu Nov 5, 2026 7:19 PM CT

**From:** CIO & Risk Gatekeeper (`7ad52fe7-3d09-4438-bfa9-552a13aef8b6`) · **To:** CEO
**Source decisions:** `CEO-Q-2026-11-05-POSTCLOSE` · submitted `2026-11-05T19:18:12-0600` CT · session `2026-11-06`
**Prior briefing:** `pod/cio_briefings/2026-11-05_1712-ct-post-close_cio_briefing.md`
> **Paper / Architecture Demo — Not Live Financial Advice.** Conditional structure only; nothing below is a live order.

---

## 1. CEO Decisions — Recorded & Binding

| # | Question | CEO Decision | Desk Action |
|---|---|---|---|
| 1 | Friday Tactical Risk Budget | **Approve 0.25% / shot (0.50% ceiling)** | Risk sizer cap = 0.25% effective (request 0.50% → VIX>25 half-size) |
| 2 | Pre-Approve Conditional Friday Scenarios | **Scenario A only** — Intraday 30m OR Breakdown / $735–$740 Rejection Short (0.25%) | Stage `ITT-2026-11-06-A` / `TKT-ITT-2026-11-06-A`. Scenarios B & C = **stand_down** |
| 3 | Buffett Value Bucket (GOOGL / META) | **Wait Patiently** — Hold cash for full-FCF 25–35% Margin of Safety | **hold_cash** — no GOOGL/META tranches staged |

Artifacts: `pod/ceo_decisions/latest_decisions.json` · archive `pod/ceo_decisions/2026-11-05_1918_ceo_decision.json` · `pod/ceo_questions.json` → `answered`.

---

## 2. Friday Scenario A — Pre-Sized Conditional Ticket

**Ticket ID:** `TKT-ITT-2026-11-06-A` · **Setup:** `ITT-2026-11-06-A` · **Status:** `conditional_preapproved`  
**Equity reference:** $50,000 paper · **Risk:** 0.25% effective = **$125** budget (actual **$120**)  
**Instrument:** SPY `option_put` × **2** contracts (long premium / short-underlying bias)  
**Agent:** Intraday Tape & Gamma (`ee33b632-6c42-4d43-bf17-665bc939db17`)

| Parameter | Value |
|---|---|
| Limit entry (premium) | **$1.70 – $2.00** (ref fill **$1.85**) |
| Hard OCO stop | **$1.25** premium (−$0.60 / contract) |
| T1 / T2 | **$3.65** (3R) / **$4.85** (5R) · scale 50/50 |
| Best R:R | **5.0R** (min 3.0R) — Firewall ✓ |
| Underlying trigger | Break + 5-min hold below Fri 30m OR_low **AND** < **$732.71**, *or* rejection at **$735–$740** put-wall zone (RVOL > 1.5×) |
| Hard invalidation (underlying) | 5-min close > OR_high, or hold > **$740** (closer one) |
| Time stop | < −0.5R in **45–60 min** → flat; no new risk after **14:30 CT**; 0DTE flat by **14:45 CT** |
| Firewall | **6/6 PASS** → `APPROVED_FOR_STAGING` |

**Do not fire** until the Friday RTH trigger prints. Re-run `pod_engine.py size-ticket` if live premium differs materially from the $1.85 reference.

---

## 3. Explicit Stand-Downs (CEO Directive)

- **Scenario B** (PTJ Macro acceptance < $730.15) — **not pre-approved** → `stand_down`
- **Scenario C** (climax sweep reclaim long) — **not pre-approved** → `stand_down`
- **ITT-2026-11-06-B** exhaustion long — **stand_down**
- **Buffett GOOGL / META** — **hold_cash** for full-FCF **25–35%** MoS; wash-sale check remains deferred; **zero tranches staged**

---

## 4. Desk Risk Directive (Post-Decision)

| Rule | Setting |
|---|---|
| Per-shot tactical risk | **0.25%** (CEO) |
| Hard ceiling | **0.50%** (CEO) |
| Vol throttle | ACTIVE (VIX 28.18 > 25) |
| Staged conditional risk | Scenario A only (`TKT-ITT-2026-11-06-A`) |
| Buffett bucket | **hold_cash** |
| Human confirmation | Still required before any live capital send |

---

## 5. Next Actions

1. Friday RTH: mark 30-minute Opening Range; arm Scenario A triggers only.
2. If trigger prints → confirm fill premium, OCO bracket, time stop; paper-stage fill then human gate.
3. If OR chops $735–$745 with no break/rejection → Scenario D observe (0% risk).
4. Buffett desk: continue MoS watchlist only; no staging until full-FCF 25–35% MoS prints.

*Ledger: `pod/ledger/paper_blotter.jsonl` · State: `pod/signals/latest_pod_state.json` · Decisions: `pod/ceo_decisions/latest_decisions.json`.*

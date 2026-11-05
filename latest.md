# SPY Gamma Structure & Regime Snapshot — November 5, 2026

**Run:** 2026-11-05T23:12:12.899191+00:00 UTC. Regular session: Nov 5. All prices USD; public feeds are delayed. No trade ideas.

## Executive regime description
SPY closed **$733.00**, **-4.4092%**, on **120,886,017 shares / 2.9539×** its prior-20-session mean. It finished just above its **$730.151000 200-day SMA**, but below its 20/50-day SMAs. The assumed dealer-OI gamma proxy is **negative**, both for the next session and aggregate upcoming expirations; AI/semiconductors materially underperformed, while banks declined less than SPY. Short-dated implied volatility is above longer-dated implied volatility.

**Context check:** SPY remains above $700, and negative gamma plus elevated volume is consistent with a short-gamma environment. Today was an AI-led *decline*, not an extension of the advance. A single current OI snapshot cannot establish historical dealer inventory or verify the earlier “light GEX” characterization. Actual signed flows and dealer positions are unavailable.

## 1. SPY price, volume and trend

| Metric | Verified value |
|---|---:|
| Regular close / prior close | $733.00 / $766.81 |
| Change | $-33.81 / -4.409176% |
| Open / high / low | $765.47 / $765.97 / $732.71 |
| Session share volume | 120,886,017 |
| Prior-20 mean volume, excluding today | 40,924,225.00 |
| Relative volume | 2.953899× |
| 20-session average including today (alternative convention) | 44,946,970.85 |
| SMA 20; close distance | $765.527500; $-32.527500 / -4.249031% |
| SMA 50; close distance | $766.396000; $-33.396000 / -4.357538% |
| SMA 200; close distance | $730.151000; $2.849000 / 0.390193% |

**Post-close indications:** Cboe quote endpoint `current_price` **$731.15** (cache `2026-11-05 23:10:17`); chain-underlying indication **$731.19** (cache `2026-11-05 23:11:31`). These are asynchronous, not simultaneous quotes. The endpoint still labels `last_trade_time` 16:00 local, so the exact after-hours tick time is **not independently verified**. Relative to the regular close, the chain indication is -0.246930% and lies $1.039000 above the 200 SMA.

**Liquidity-level checks:** prior-day high/low **775.64 / 762.64**; prior-5-session high/low **775.64 / 750.36**; prior-20-session high/low **779.42 / 750.36**. Today's low **732.71** breached all those prior lows, and the close did **not** reclaim them. No upside sweep of those highs occurred. This is a downside break on daily OHLC, not sufficient evidence to label a stop-hunt or intraday liquidity grab.

## 2. Options and GEX

Cboe chain cache: **2026-11-05 23:11:31** (top-level timezone assumed UTC). **13,010** raw records; **120 `B1SPY` records excluded** as nonstandard roots; **402 expired Nov 5 SPY contracts excluded** after 16:15 ET. Aggregate unexpired standard-root coverage: **12,488 contracts**, through **2029-01-19**. Near-term coverage: **5,130 contracts / 16 expirations**, Nov 6–Dec 18 (all available expiries within 45 calendar DTE).

**Nov 6 is 1DTE at this Nov 5 snapshot and becomes 0DTE next session. Expired Nov 5 0DTE must not be counted as future exposure.**

### Convention
`Call GEX = +gamma × OI × 100 × S² × 0.01`; `Put GEX = -gamma × OI × 100 × S² × 0.01`, with **S = 731.19**. Units: dollars of delta-notional change for a 1% move. This imposes the requested **dealers-long-calls / dealers-short-puts** assumption. It does not reveal actual ownership or dealer inventory.

### Net exposure, ratios and repriced zero-gamma estimates

| Scope | Call GEX $bn/1% | Put GEX $bn/1% | Net GEX $bn/1% | Put/call OI | Put/call volume | Model zero-gamma |
|---|---:|---:|---:|---:|---:|---:|
| Nov 6 only | 0.282440 | -2.942013 | **-2.659573** | 1.903024 | 1.131773 | 757.1885 |
| Nov 20 OPEX | 0.972410 | -7.355729 | **-6.383318** | 4.458343 | 3.737935 | 782.2699 |
| Upcoming ≤45 DTE | 4.178739 | -18.030069 | **-13.851330** | 2.608585 | 1.594140 | 765.9808 |
| All upcoming expiries | 7.379889 | -23.485701 | **-16.105812** | 2.051339 | 1.539841 | 765.9742 |

**All four scopes are negative/short-gamma proxies.**

**Gamma-flip distinction:** Cumulatively adding fixed-spot net GEX in ascending strike order produces **no negative-to-positive cumulative crossing** in any of these scopes. Therefore there is **no flip under the requested literal cumulative-strike definition**. The table’s separate model levels are conventional *spot-repriced* zero-gamma roots, not cumulative-strike levels and not independently published/observed walls. Frozen Cboe IV, European Black–Scholes approximation, ACT/365, expiry 16:15 ET, rate **4.00%** and dividend yield **1.20%** are explicit model assumptions—not measured rates.

| Model scope | Valid-IV contracts / total | OI represented | Repriced-model net GEX at spot, $bn/1% |
|---|---:|---:|---:|
| Nov 6 | 253 / 342 | 85.3581% | -2.482856 |
| Nov 20 | 385 / 474 | 99.6801% | -6.331174 |
| ≤45 DTE | 3,504 / 5,130 | 98.2300% | -13.553407 |
| All | 9,174 / 12,488 | 98.8586% | -15.577860 |

The model and reported-gamma totals differ because source gammas are rounded and can be closing marks, and the model revalues time/IV-valid contracts. Do not imply an exact observable dealer flip. The search range is ±30% of spot; all roots found are recorded in JSON.

### Walls: gamma-weighted, not interchangeable with OI-only maxima

| Scope | Primary/secondary call-gamma strikes (GEX $m) | Primary/secondary put-gamma strikes (GEX $m) |
|---|---|---|
| Nov 6 | 760 (20.079); 755 (17.008); 745 (12.484) | 735 (-373.036); 740 (-322.586); 750 (-267.934) |
| Nov 20 | 800 (86.665); 750 (75.711); 770 (67.442) | 740 (-817.971); 725 (-617.187); 750 (-479.073) |
| ≤45 DTE | 770 (270.787); 750 (249.134); 780 (225.063) | 740 (-1,587.147); 750 (-1,183.181); 730 (-1,088.449) |
| All | 800 (412.426); 770 (375.051); 780 (343.633) | 740 (-1,740.485); 750 (-1,454.655); 730 (-1,303.680) |

| Scope | Call OI / put OI | Call volume / put volume | Largest call-OI strike | Largest put-OI strike |
|---|---:|---:|---|---|
| Nov 6 | 260,425 / 495,595 | 1,318,956 / 1,492,759 | 780: 17,125 | 750: 38,550 |
| Nov 20 | 691,979 / 3,085,080 | 334,883 / 1,251,771 | 800: 162,101 | 500: 321,244 |
| ≤45 DTE | 2,694,635 / 7,029,185 | 2,863,236 / 4,564,398 | 800: 251,773 | 500: 346,225 |
| All | 5,582,366 / 11,451,325 | 3,159,516 / 4,865,151 | 800: 363,091 | 500: 529,850 |

Far-out-of-money put OI at 500/480 is not the same as an actionable near-spot put-gamma wall; spot gamma concentrates much closer to 740/750/730. No trading implication is asserted.

### Explicit $735 strike node

| Scope | Call OI | Put OI | Call volume | Put volume | Σ(call gamma×OI) | Σ(put gamma×OI) | Net GEX $m/1% |
|---|---:|---:|---:|---:|---:|---:|---:|
| Nov 6 | 387 | 23,652 | 28,788 | 97,658 | 11.1843 | 697.7340 | -367.056119 |
| Nov 20 | 2,510 | 16,676 | 2,919 | 23,313 | 27.8610 | 186.7712 | -84.959561 |
| ≤45 DTE | 9,091 | 137,031 | 50,382 | 174,004 | 90.1116 | 1,756.0162 | -890.657263 |
| All | 26,124 | 195,524 | 55,333 | 189,409 | 169.8176 | 2,093.8280 | -1,028.650642 |

| Nov 6 $735 contract | Gamma per share/$ | IV | Delta | Call/put GEX $m/1% |
|---|---:|---:|---:|---:|
| SPY261106C00735000 | 0.0289 | 35.2200% | 0.4397 | 5.979561 |
| SPY261106P00735000 | 0.0295 | 35.3200% | -0.5626 | -373.035680 |

OI-weighted gamma at 735: ≤45D **calls 0.00991218 / puts 0.01281474**; all-expiry **calls 0.00650044 / puts 0.01070880**. Cross-expiry gamma cannot be described as one contract’s gamma; the table reports summed gamma×OI. Nov 6’s **735 is the primary put-gamma wall**, whereas the aggregate put-gamma maximum is **740**.

**Independent chain check:** Yahoo matched **309 Nov 6 contracts**, with **309/309 exact OI matches**. At 735, calls match on OI **387** and volume **28,788**; puts match on OI **23,652**, but Yahoo volume **97,679** versus Cboe **97,658**—a **21-contract snapshot difference**, explicitly retained. Yahoo supplies no gamma, so source Greeks have no independent second-feed validation.

**Close-spot sensitivity, keeping source gamma fixed:** at S=733.00, all-upcoming net GEX is **$-16,185,647,963.18 / 1%**; ≤45D is **$-13,919,990,461.00 / 1%**. This rescales S² only; it is not a full close-time gamma revaluation.

## 3. Volatility regime

| Index | Close | Point change | Day change % |
|---|---:|---:|---:|
| ^VIX | 28.18 | 11.57 | 69.656833 |
| ^VVIX | 134.99 | 44.58 | 49.308705 |
| ^VIX9D | 32.66 | 17.47 | 115.009875 |
| ^VIX3M | 25.55 | 6.75 | 35.904255 |

**VIX9D 32.66 > VIX 28.18 > VIX3M 25.55:** backwardated implied-volatility tenors. VIX/VIX3M **1.102935**; VIX−VIX3M **2.63** points; VIX9D−VIX **4.48** points. This is **not a measured VIX-futures term structure**. Cboe index last times are 16:15:01 ET. The correct VIX close-to-close change is **+11.57**, not the +11.75 intraday-high rise cited in some reporting.

## 4. AI / data-center relative strength

Returns use 1/5/20 trading-session close comparisons; volume is shares, not signed investment flow.

| Symbol | Close | 1D % | 5D % | 20D % | 1D vs SPY, pp | Volume | Vol/prior20 |
|---|---:|---:|---:|---:|---:|---:|---:|
| SPY | 733.0000 | -4.4092 | -4.7260 | -5.2886 | 0.0000 | 120,886,017 | 2.9539 |
| ^SOX | 9,011.1377 | -12.2461 | -21.7661 | -28.6174 | -7.8370 | 0 | unavailable |
| SMH | 447.7300 | -11.2122 | -20.2859 | -26.2717 | -6.8031 | 28,709,942 | 2.8374 |
| SOXX | 131.9000 | -12.1467 | -22.9946 | -29.7507 | -7.7376 | 65,770,030 | 2.0094 |
| QQQ | 647.0500 | -6.4889 | -9.5465 | -13.4474 | -2.0797 | 125,582,100 | 2.9153 |
| XLK | 162.3800 | -8.0000 | -13.9663 | -17.8987 | -3.5908 | 33,229,448 | 3.2635 |
| NVDA | 181.6000 | -9.2590 | -15.0051 | -21.2079 | -4.8498 | 301,617,834 | 2.9128 |
| AVGO | 291.5300 | -10.4307 | -15.4986 | -19.0509 | -6.0216 | 53,372,113 | 2.4506 |
| TSM | 353.1000 | -10.7770 | -18.2279 | -22.9022 | -6.3678 | 42,645,926 | 3.4085 |
| MSFT | 535.9300 | -3.5750 | 1.0922 | 2.5487 | 0.8341 | 41,558,807 | 1.7267 |
| VRT | 176.7300 | -14.0209 | -28.1352 | -27.4894 | -9.6117 | 19,232,168 | 2.5098 |
| CEG | 239.6500 | -2.4703 | -9.4225 | -15.9329 | 1.9389 | 6,960,402 | 1.9404 |
| ANET | 159.1600 | -10.8247 | -13.6033 | -24.5580 | -6.4156 | 14,811,494 | 2.0633 |

**Reading:** NVDA and SOX significantly underperformed SPY over all three horizons. Semiconductors, networking and VRT were materially weaker on Nov 5; MSFT and CEG declined less than SPY. MSFT was also positive over 5D and 20D. High volume confirms elevated participation, not net buying/selling or ETF flows.

**SOXX quality correction:** Yahoo contained unadjusted pre-split Nov 2–4 bars alongside otherwise split-adjusted history and a reported 3-for-1 split on Nov 5. Each affected day was independently checked against Cboe historical data; only those three bars were divided by 3, with prior volumes multiplied by 3. This avoids the erroneous −70.7% daily return. The report uses the corrected **−12.1467%** return. Cboe’s SOXX quote also contained an inconsistent unadjusted previous close; it was not used for returns. Cboe `_SOX` was stale at Nov 4, so **current SOX is from Yahoo**, not the stale Cboe quote.

## 5. KBW banks

| Symbol | Close | 1D % | 5D % | 20D % | 200 SMA | Distance from 200 SMA | 1D / 5D / 20D vs SPY, pp |
|---|---:|---:|---:|---:|---:|---|---|
| ^BKX | 171.2067 | -3.5673 | -2.4408 | 0.7098 | 173.1259835 | $-1.9192835 / -1.108605% | 0.8419 / 2.2852 / 5.9984 |
| KBWB | 88.0300 | -3.5393 | -2.2974 | 0.9055 | 88.9745000 | $-0.9445000 / -1.061540% | 0.8698 / 2.4286 / 6.1941 |

Both bank measures are **below** their 200 SMAs, but outperformed SPY across 1D/5D/20D. Index volume is not a meaningful tradable-flow measure; KBWB share volume is available in JSON.

## 6. New Globex session: quote bias and levels

**New-session start: Nov 5, 18:00 EST / 23:00 UTC.** Public quotes are roughly 10 minutes delayed at collection, and only the opening minutes through 18:02 EST were available. Do not present these as live quotes at report generation.

| Future | Quote / quote time EST | Yahoo displayed change / % | Inferred Yahoo reference | Reopened range available | Minute-bar last / vs prior 16:59 last |
|---|---|---|---:|---|---|
| ES=F | 7,364.75 / 2026-11-05 18:02:11-05:00 | -18.25 / -0.247% | 7,383.00 | 7,351.50–7,373.00 | 7,364.75 / 11.7500 / 0.159799% |
| NQ=F | 26,667.25 / 2026-11-05 18:02:11-05:00 | -84.75 / -0.317% | 26,752.00 | 26,584.00–26,717.00 | 26,671.00 / 67.0000 / 0.251842% |
| RTY=F | 2,728.80 / 2026-11-05 18:02:10-05:00 | -6.60 / -0.241% | 2,735.40 | 2,722.90–2,731.20 | 2,728.80 / 5.4001 / 0.198287% |

**Bias description:** the available reopened quotes are below Yahoo’s displayed reference, but above the preceding 16:59 traded closes. These are different benchmarks, so “down versus reference” and “rebounding versus late-session last” can both be true. The reference is inferred as quote minus displayed change; **official CME settlements were not independently verified**. Available opening-minute volume totals were **ES 998 / NQ 514 / RTY 75 contracts**, potentially partial because bars are delayed. Quote and minute-bar last prices can differ, as explicitly shown. Full overnight ranges so far are unavailable beyond the delayed opening-minutes data.

| Future | Prior 16:59 last | Prior completed session reconstructed low–high | Nov 4 daily close |
|---|---:|---|---:|
| ES=F | 7,353.00 | 7,346.00–7,738.75 | 7,720.50 |
| NQ=F | 26,604.00 | 26,570.00–28,620.75 | 28,566.75 |
| RTY=F | 2,723.40 | 2,718.50–2,888.30 | 2,865.90 |

Minute-derived levels are distinct from Yahoo’s daily full-session OHLC, which differed slightly. They are explicitly labelled reconstructed, not official session extrema or settlements.

## 7. Verified current AI/data-center regulation reporting

- **[Top Democrats call to rein in AI buildout after midterms sweep](https://www.cnbc.com/2026/11/05/democrats-data-center-restrictions-midterms.html)** — 2026-11-05 15:16 EST. CNBC reports calls for a national data-center moratorium and Texas governor-elect Hinojosa's campaign pledge to pause approvals. These are political proposals/pledges, not proof of an enacted national moratorium.

- **[Blue tsunami wipes out $2.5 trillion from Nasdaq, as investors fret about AI trade](https://www.investing.com/news/stock-market-news/ai-datacenter-stocks-slip-as-democratic-sweep-clouds-expansion-plans-5003196)** — 2026-11-05; updated 17:18 (publisher timezone unspecified). Reuters attributes AI-sector weakness to permitting, grid and water constraints and potential construction pauses after the midterms. It also reports Microsoft tightening oversight of some partner data-center projects, citing Bloomberg; underlying Bloomberg report not independently opened.

These are verified published reports about policy risk, proposals and campaign pledges. They do **not** establish that a national moratorium has been enacted. No unverified dollar-flow claim or uncited X commentary is incorporated.

## 8. Source audit and limitations

- Primary option-chain source: https://cdn.cboe.com/api/global/delayed_quotes/options/SPY.json — HTTP 200, exact source gamma/OI/volume/IV/delta preserved. `_SPY.json` was also tested and returned HTTP 403; no attempt to bypass access restrictions.

- Prices/history/independent options: Yahoo Finance through `yfinance`; Yahoo regular close and Cboe close agree at **733.00**. Direct requests to query1/query2 Yahoo chart/options endpoints returned HTTP 429, but the normal `yfinance` client succeeded. No private credentials were used.

- Daily Cboe historical cross-check: https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/SPY.json — latest completed row Nov 4; appending independently verified Nov 5 close reproduces **20 SMA 765.5275; 50 SMA 766.396; 200 SMA 730.151** exactly. No dividend adjustment.

- Cboe quote sources: `https://cdn.cboe.com/api/global/delayed_quotes/quotes/{SPY,_VIX,_VVIX,_VIX9D,_VIX3M,_BKX}.json`. Successful index quotes cross-check the daily index levels. `_SOX` is explicitly stale and excluded from current SOX.

- Published Quantisnow commentary reported SPY 733.43 and QQQ 648.05, conflicting with both primary quote/history feeds. Those conflicting prices and its flow ratios were **not used**: https://www.quantisnow.com/articles/tech-falls-7-94-nasdaq-100-drops-6-31-equal-weight-beats-s-p-500-by-1-44-points

- Volume discrepancy: Yahoo regular SPY volume **120,886,017** versus Cboe feed **122,331,534**. Scope/consolidation timing was not independently resolved. The primary relative-volume calculation consistently uses Yahoo regular daily volume and Yahoo historical daily volume, not a mixture of providers.

- OI is generally the previous clearing-day balance; no intraday OI update is asserted. Session option volume cannot show opening vs closing trades, side, dealer sign, net flow, or live changes in inventory. Source gamma precision is limited; monetary output decimals are computational precision, not market certainty.

- SMA values are exact arithmetic means of the specified feed closes after removing float32 serialization noise by snapping to $0.0001. SPY SMAs are independently reproduced from Cboe’s cent-precision closes. Figures in tables are rounded; machine JSON retains full computed precision.

## 9. Reusable implementation and outputs

- Script: `/workspace/spy_gamma_monitor.py`

- Machine-readable full snapshot, including every strike profile and every 735 contract: `/workspace/spy_gamma_snapshot.json`

- Compact generated tables: `/workspace/spy_gamma_summary.md`

- This comprehensive report: `/workspace/spy_gamma_report.md`

- Auditable source cache: `/workspace/spy_gamma_data/`

- Dated verified article metadata: `/workspace/spy_gamma_headlines.json`

- Isolated Python runtime: `/workspace/spy_gamma_venv/bin/python`; dependency lock: `/workspace/spy_gamma_requirements.lock.txt`

```bash
/workspace/spy_gamma_venv/bin/python /workspace/spy_gamma_monitor.py
# JSON stdout; Markdown table stderr; both also saved by default
/workspace/spy_gamma_venv/bin/python /workspace/spy_gamma_monitor.py --offline \
  --output /workspace/replay.json --summary /workspace/replay.md
```

The script automatically collects numerical data and cross-checks/split-normalizes SOXX, computes separate next-session/OPEX/45D/all-expiry profiles, isolates reopened futures bars, and records source timestamps/errors. It returns a nonzero status if primary SPY or options data is missing/stale. The current live-run expected `_SPY` 403 is recorded as a source error without invalidating the working `SPY` endpoint. Historical `--asof` never relabels a current chain as historical. Headlines are dated, manually verified input metadata; future runs do **not** falsely auto-verify new news and omit stale dated entries.

No recurring job was scheduled; this task requested a reusable script, not permission to create a schedule.

# Buffett Value Compounder — Capital-Allocation & Panic Shopping List Memo
Date: 2026-11-05 post-close (CT). Prices: yfinance Nov 5 2026 closes. Fundamentals: yfinance TTM (last 4 quarters reported; latest quarter Sep-30-2026 for most names, Jul-31 for NVDA/AVGO, Jun-30 for V/BRK).
Lens: Fundamental Value (Buffett/Munger). No personal or account data. Not an order; CIO & CEO gate applies.

## Regime (from horsey latest.md)
SPY 733.00 (-4.41%, RVOL 2.95x), +0.39% above 200-SMA 730.15. Net GEX is negative in all scopes (45D -13.85B per 1%). VIX 28.18; VIX9D 32.66 > VIX > VIX3M 25.55 (backwardated). SOX -12.2%. Read: short-gamma panic, so the shopping list is active and entries are tranched.

## Method
Owner earnings (OE) = TTM OCF - capex. For MSFT/GOOGL/META, OE = OCF - maintenance capex (proxy: D&A), because most of their AI capex is growth spend; full-capex FCF is shown alongside. DCF: growth g for years 1-5, g/2 for years 6-10, 3% terminal, 9% discount, plus net cash, divided by shares. BRK-B is valued at 1.5x book. JPM is valued at 12x TTM net income. MoS buy zones are 75% and 65% of intrinsic value (IV). Discount = 1 - price/IV.

| Ticker | Close | OE $B | Net cash $B | IV/sh | 25% MoS buy | 35% MoS buy | Discount to IV | Basis |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| MSFT | 535.93 | 153.7 | -69.4 | 502.63 | 376.97 | 326.71 | -6.6% | OCF 195.9B - maint capex(=D&A) 42.1B; full FCF 60.0B |
| GOOGL | 282.57 | 151.6 | 114.8 | 619.68 | 464.76 | 402.79 | 54.4% | OCF 179.6B - maint capex(=D&A) 28.0B; full FCF 19.7B |
| META | 605.08 | 108.2 | -32.4 | 1059.18 | 794.39 | 688.47 | 42.9% | OCF 133.1B - maint capex(=D&A) 24.9B; full FCF 28.6B |
| AAPL | 344.17 | 136.1 | -37.8 | 169.75 | 127.31 | 110.34 | -102.8% | OCF 145.1B - capex 9.0B |
| NVDA | 181.6 | 127.0 | 23.6 | 131.05 | 98.28 | 85.18 | -38.6% | OCF 134.4B - capex 7.4B |
| AVGO | 291.53 | 39.4 | -35.4 | 196.7 | 147.52 | 127.85 | -48.2% | OCF 40.7B - capex 1.2B |
| TSM | 353.1 | 44.2 | 96.7 | 229.51 | 172.13 | 149.18 | -53.9% | OCF 3127.6B - capex 1664.8B (TWD, /33.08) |
| V | 422.59 | 21.0 | -8.0 | 278.93 | 209.2 | 181.3 | -51.5% | OCF 22.6B - capex 1.6B |
| BRK-B | 572.33 | - | - | 522.23 | 391.67 | 339.45 | -9.6% | 1.5x book/B-sh 348.15 |
| JPM | 337.34 | - | - | 291.58 | 218.69 | 189.53 | -15.7% | 12x TTM NI/sh 24.30 |

Growth assumptions: MSFT 10%, GOOGL 9%, META 8%, AAPL 5%, NVDA 10%, AVGO 10%, TSM 10% (TWD converted at 33.08), V 9%.

## Actionable now (>=25% discount)
- GOOGL (54% below IV) and META (43% below IV) clear the bar ONLY under maintenance-capex owner earnings. On full TTM FCF (GOOGL 19.7B, META 28.6B) they do not. Treat this as a conditional, tranched (1/3) allocation that depends on AI capex earning at or above its cost of capital. Invalidation: capex/revenue keeps rising for 2 or more years without operating-income growth, search share erodes (GOOGL), or ad-revenue growth falls below 5% (META).
- Nothing else qualifies.

## Panic Shopping List (wait for these levels)
MSFT 377/327; NVDA 98/85; AVGO 148/128; TSM 172/149; V 209/181; AAPL 127/110; JPM 219/190; BRK-B 392/339.
Invalidation is about the business thesis, not price stops: lost moat (switching costs or network effects), ROIC below 15%, net debt above 2x EBITDA, or a value-destroying acquisition.

## Wash-sale
No trade data in repo. wash_sale_tracker_v2.py needs data/robinhood_options.csv (absent). workflows/wash_sale_tracker.yml calls wash_sale_tracker.py, which does not match the file name, so the job will fail. Restriction status is UNKNOWN; check before any buy.

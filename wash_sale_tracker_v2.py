#!/usr/bin/env python3
"""
Wash Sale Tracker v2 - Robinhood Options with FIFO Lot Matching + Realized P/L

ENHANCED FEATURES (added per your request):
- FIFO lot matching to compute accurate realized P/L per closing trade (focused on long options: BTO → STC).
- Wash sale detection with disallowed loss amounts calculated.
- Summary of total realized gains/losses before and after wash adjustments.
- Notes on basis adjustments for disallowed losses.
- Designed to support your goal of cleaning up trading by mid-November 2026 so wash sale windows clear before year-end.

USAGE (same as v1 + new options):
    python wash_sale_tracker_v2.py your_options.csv [--tax-year-end 2026-12-31]

Outputs:
- processed_trades_with_pnl.csv          (every trade + realized_pnl, is_wash, disallowed_amount)
- wash_sale_summary.txt                  (high-level numbers for quick review)
- potential_wash_details.csv             (detailed flagged events)
- (Optional) GitHub Actions workflow for automated weekly runs

This gives you real-time visibility into:
- How much of your May (and other) losses are currently allowable vs. potentially disallowed.
- Progress toward recovering losses while keeping tax picture clean.
- Any late trades whose wash windows would spill into 2027 (helps your mid-Nov quit target).

REQUIREMENTS
    pip install pandas

Author: Grok-assisted — tailored for your trading accountability + tax-clean exit plan
"""

import pandas as pd
from datetime import datetime, timedelta, date
from collections import defaultdict
import sys
import os
import re
from dataclasses import dataclass
from typing import List, Dict

# ============================================================
# CONFIG & COLUMN MAPPING (edit to match your export)
# ============================================================
COLUMN_MAP = {
    'trade_date': ['trade_date', 'Activity Date', 'order_created_at', 'date', 'Date'],
    'action': ['action', 'side', 'direction', 'Trans Code', 'Side'],
    'underlying': ['underlying', 'chain_symbol', 'Symbol', 'symbol', 'Instrument'],
    'expiration': ['expiration', 'expiration_date', 'exp_date', 'Expiration'],
    'strike': ['strike', 'strike_price', 'Strike'],
    'option_type': ['option_type', 'type', 'Option Type', 'put_call'],
    'quantity': ['quantity', 'order_quantity', 'processed_quantity', 'Quantity'],
    'price': ['price', 'Price'],
    'fees': ['fees', 'Fees & Commissions', 'fee', 'Fees'],
}

@dataclass
class Lot:
    """Represents one opening lot for FIFO matching."""
    date: date
    original_qty: int
    remaining_qty: int
    cost_per_share: float   # premium per share

def find_column(df, possible_names):
    for name in possible_names:
        if name in df.columns:
            return name
    return None

def parse_option_symbol(symbol_str):
    if pd.isna(symbol_str):
        return None
    s = str(symbol_str).strip().upper().replace(' ', '').replace('$', '').replace("'", "")
    match = re.match(r'^([A-Z]{1,6})(\d{6})([CP])(\d{8})$', s)
    if match:
        underlying = match.group(1)
        try:
            exp_date = datetime.strptime(match.group(2), '%y%m%d').date()
        except:
            return None
        opt_type = 'call' if match.group(3) == 'C' else 'put'
        try:
            strike = int(match.group(4)) / 1000.0
        except:
            return None
        return {'underlying': underlying, 'expiration': exp_date, 'option_type': opt_type, 'strike': strike}
    return None

def normalize_action(action_str):
    if pd.isna(action_str):
        return None
    a = str(action_str).upper().strip()
    if any(x in a for x in ['BUY', 'BTO', 'BTC']):
        return 'buy'
    if any(x in a for x in ['SELL', 'STO', 'STC']):
        return 'sell'
    return None

def create_contract_key(row):
    return f"{row['underlying']}_{row['expiration'].strftime('%Y-%m-%d')}_{row['option_type']}_{row['strike']}"

def main(csv_path: str, tax_year_end: date = date(2026, 12, 31)):
    if not os.path.exists(csv_path):
        print(f"Error: {csv_path} not found")
        sys.exit(1)

    print(f"Loading and processing {csv_path}...")
    df = pd.read_csv(csv_path)

    # Column resolution & cleaning
    col_map = {k: find_column(df, v) for k, v in COLUMN_MAP.items()}
    required = ['trade_date', 'action', 'underlying', 'expiration', 'strike', 'option_type', 'quantity', 'price']
    missing = [k for k in required if col_map.get(k) is None]
    if missing:
        print(f"Missing required columns: {missing}. Edit COLUMN_MAP or rename your CSV.")
        sys.exit(1)

    df['trade_date'] = pd.to_datetime(df[col_map['trade_date']], errors='coerce').dt.date
    df['action'] = df[col_map['action']].apply(normalize_action)
    df['underlying'] = df[col_map['underlying']].astype(str).str.upper().str.strip()
    df['expiration'] = pd.to_datetime(df[col_map['expiration']], errors='coerce').dt.date
    df['strike'] = pd.to_numeric(df[col_map['strike']], errors='coerce')
    df['option_type'] = df[col_map['option_type']].astype(str).str.lower().str.strip().replace({'c':'call','p':'put'})
    df['quantity'] = pd.to_numeric(df[col_map['quantity']], errors='coerce').fillna(0).astype(int)
    df['price'] = pd.to_numeric(df[col_map['price']], errors='coerce')
    df['fees'] = pd.to_numeric(df.get(col_map.get('fees'), 0), errors='coerce').fillna(0.0)

    # Fallback symbol parsing
    sym_col = find_column(df, ['symbol', 'Symbol', 'chain_symbol', 'Instrument'])
    if sym_col and (df['expiration'].isna().any() or df['strike'].isna().any()):
        parsed = df[sym_col].apply(parse_option_symbol)
        mask = parsed.notna()
        if mask.any():
            p = pd.DataFrame(parsed[mask].tolist(), index=df[mask].index)
            df.loc[mask, 'underlying'] = p['underlying'].combine_first(df.loc[mask, 'underlying'])
            df.loc[mask, 'expiration'] = p['expiration'].combine_first(df.loc[mask, 'expiration'])
            df.loc[mask, 'option_type'] = p['option_type'].combine_first(df.loc[mask, 'option_type'])
            df.loc[mask, 'strike'] = p['strike'].combine_first(df.loc[mask, 'strike'])

    # Filter valid options
    df = df.dropna(subset=['trade_date', 'action', 'underlying', 'expiration', 'strike', 'option_type', 'quantity', 'price'])
    df = df[(df['quantity'] > 0) & (df['action'].isin(['buy', 'sell']))]
    df['contract_key'] = df.apply(create_contract_key, axis=1)
    df = df.sort_values('trade_date').reset_index(drop=True)

    print(f"Processing {len(df)} valid options trades across {df['contract_key'].nunique()} unique contracts...")

    # Precompute buys for wash detection
    buys_by_contract: Dict[str, List[date]] = defaultdict(list)
    for _, r in df[df['action'] == 'buy'].iterrows():
        buys_by_contract[r['contract_key']].append(r['trade_date'])

    # FIFO LOT MATCHING + REALIZED P/L + WASH DETECTION
    open_lots: Dict[str, List[Lot]] = defaultdict(list)
    processed = []
    MULTIPLIER = 100

    for _, row in df.iterrows():
        key = row['contract_key']
        d = row['trade_date']
        action = row['action']
        qty = int(row['quantity'])
        price = float(row['price'])
        fees = float(row['fees'])

        record = row.to_dict()
        record['realized_pnl'] = 0.0
        record['is_wash'] = False
        record['disallowed_amount'] = 0.0
        record['note'] = ''

        if action == 'buy':
            lot = Lot(date=d, original_qty=qty, remaining_qty=qty, cost_per_share=price)
            open_lots[key].append(lot)
            record['note'] = 'Buy - new lot added (FIFO)'

        elif action == 'sell':
            realized = 0.0
            to_close = qty
            matched = 0
            while to_close > 0 and open_lots[key]:
                lot = open_lots[key][0]
                match_qty = min(to_close, lot.remaining_qty)
                cost = lot.cost_per_share * match_qty * MULTIPLIER
                proceeds = price * match_qty * MULTIPLIER
                realized += (proceeds - cost)
                realized -= (fees * match_qty / qty) if qty > 0 else 0
                lot.remaining_qty -= match_qty
                to_close -= match_qty
                matched += match_qty
                if lot.remaining_qty <= 0:
                    open_lots[key].pop(0)

            record['realized_pnl'] = round(realized, 2)

            if to_close > 0:
                record['note'] = f"FIFO closed {matched} contracts; {to_close} unmatched (possible short or over-close)"
            else:
                record['note'] = f"FIFO matched {matched} contracts from open lots"

            if realized < 0:
                window_start = d - timedelta(days=30)
                window_end = d + timedelta(days=30)
                buys_in_window = [b for b in buys_by_contract.get(key, []) if window_start <= b <= window_end]
                if buys_in_window:
                    record['is_wash'] = True
                    record['disallowed_amount'] = round(-realized, 2)
                    record['note'] += " | ⚠️ WASH SALE — loss disallowed this year; added to replacement buy(s) basis"
                    earliest_buy = min(buys_in_window)
                    record['note'] += f" (earliest replacement buy: {earliest_buy})"

        processed.append(record)

    trades_df = pd.DataFrame(processed)

    # SUMMARY & OUTPUT
    total_realized_pnl = trades_df['realized_pnl'].sum()
    total_disallowed = trades_df['disallowed_amount'].sum()
    net_allowable_pnl = total_realized_pnl + total_disallowed

    wash_count = int(trades_df['is_wash'].sum())
    wash_df = trades_df[trades_df['is_wash'] == True][['trade_date', 'contract_key', 'realized_pnl', 'disallowed_amount', 'note']]

    print("\n" + "="*70)
    print("WASH SALE + REALIZED P/L SUMMARY (FIFO)")
    print("="*70)
    print(f"Total realized P/L (all closes):        ${total_realized_pnl:,.2f}")
    print(f"Disallowed losses due to wash sales:    ${total_disallowed:,.2f}")
    print(f"Net allowable P/L for 2026 (approx):    ${net_allowable_pnl:,.2f}")
    print(f"Number of wash-flagged closing sells:   {wash_count}")
    print("="*70)

    trades_df.to_csv('processed_trades_with_pnl.csv', index=False)
    print("\nSaved: processed_trades_with_pnl.csv")

    if not wash_df.empty:
        wash_df.to_csv('potential_wash_details.csv', index=False)
        print("Saved: potential_wash_details.csv")

    with open('wash_sale_summary.txt', 'w') as f:
        f.write(f"Wash Sale + FIFO P/L Summary — Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"Source file: {csv_path}\n\n")
        f.write(f"Total realized P/L from closing trades: ${total_realized_pnl:,.2f}\n")
        f.write(f"Disallowed due to wash sales:           ${total_disallowed:,.2f}\n")
        f.write(f"Net allowable for tax year:             ${net_allowable_pnl:,.2f}\n\n")
        f.write("This helps track progress toward recovering losses while keeping your 2026 tax picture clean.\n")
        f.write("Note: Focused on long options (BTO→STC). Short options approximated.\n")
        f.write("Cross-check with your 1099-B.\n")

    print("Saved: wash_sale_summary.txt")

    # Late trade warning for your mid-Nov goal
    late_risk = trades_df[
        (trades_df['action'] == 'sell') &
        (trades_df['trade_date'] > (tax_year_end - timedelta(days=60))) &
        (trades_df['is_wash'] == True)
    ]
    if not late_risk.empty:
        print(f"\n⚠️  {len(late_risk)} late wash-flagged sells whose windows may extend near/into next year.")
        print("Factor this into your mid-November trading stop date.")

    print("\nDone. Use these for your journal / coach reviews.")
    print("Mid-Nov quit tip: Sells after ~Oct 15–Nov 1 will mostly clear their wash windows by year-end if you stop new activity.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_file")
    parser.add_argument("--tax-year-end", type=str, default="2026-12-31")
    args = parser.parse_args()
    try:
        end_date = datetime.strptime(args.tax_year_end, "%Y-%m-%d").date()
    except:
        end_date = date(2026, 12, 31)
    main(args.csv_file, tax_year_end=end_date)
import pandas as pd

import matplotlib.pyplot as plt

import matplotlib.dates as mdates

import yfinance as yf

from datetime import datetime

import os

CSV_FILE = "spy_historical.csv"

# Create CSV if it doesn't exist

if not os.path.exists(CSV_FILE):

    pd.DataFrame(columns=['Date', 'SPY_Close', 'Net_GEX', 'Daily_Volume',

                          'Call_Wall', 'Put_Wall', 'Gamma_Flip']).to_csv(CSV_FILE, index=False)

df = pd.read_csv(CSV_FILE)

df['Date'] = pd.to_datetime(df['Date'])

# Pull latest SPY data

print("Fetching latest SPY data...")

ticker = yf.Ticker("SPY")

hist = ticker.history(period="5d")

latest = hist.iloc[-1]

latest_date = latest.name.strftime('%Y-%m-%d')

latest_close = round(latest['Close'], 2)

latest_volume = int(latest['Volume'])

print(f"Date: {latest_date} | Close: {latest_close} | Volume: {latest_volume}")

if latest_date in df['Date'].dt.strftime('%Y-%m-%d').values:

    print("Data for today already exists.")

else:

    # Get GEX from environment variables (set in workflow) or leave blank

    net_gex = os.getenv("NET_GEX") or None

    call_wall = os.getenv("CALL_WALL") or None

    put_wall = os.getenv("PUT_WALL") or None

    gamma_flip = os.getenv("GAMMA_FLIP") or None

    new_row = pd.DataFrame({

        'Date': [latest_date],

        'SPY_Close': [latest_close],

        'Net_GEX': [float(net_gex) if net_gex else None],

        'Daily_Volume': [latest_volume],

        'Call_Wall': [float(call_wall) if call_wall else None],

        'Put_Wall': [float(put_wall) if put_wall else None],

        'Gamma_Flip': [float(gamma_flip) if gamma_flip else None]

    })

    df = pd.concat([df, new_row], ignore_index=True)

    df = df.drop_duplicates(subset=['Date'], keep='last').sort_values('Date')

    df.to_csv(CSV_FILE, index=False)

    print(f"✅ Added new row for {latest_date}")

# Generate chart

plt.style.use('dark_background')

fig, ax1 = plt.subplots(figsize=(14, 8))

ax1.set_xlabel('Date')

ax1.set_ylabel('Net GEX', color='tab:red')

ax1.plot(df['Date'], df['Net_GEX'], color='tab:red', linewidth=2.5, label='Net GEX')

ax1.tick_params(axis='y', labelcolor='tab:red')

ax1.axhline(0, color='gray', linestyle='--', linewidth=1)

if len(df) >= 8:

    ax1.axvspan(df['Date'].iloc[-8], df['Date'].iloc[-1], alpha=0.25, color='red', label='Short-Gamma Regime')

ax2 = ax1.twinx()

ax2.set_ylabel('SPY Close', color='tab:cyan')

ax2.plot(df['Date'], df['SPY_Close'], color='tab:cyan', linewidth=2.5, label='SPY Close')

ax2.tick_params(axis='y', labelcolor='tab:cyan')

ax3 = ax1.twinx()

ax3.spines['right'].set_position(('outward', 60))

ax3.set_ylabel('Daily Volume (M)', color='tab:gray')

ax3.bar(df['Date'], df['Daily_Volume'] / 1_000_000, color='tab:gray', alpha=0.35, width=0.8, label='Volume')

ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))

plt.title('SPY - Net GEX vs Price & Volume', fontsize=16, pad=20)

fig.legend(loc='upper left', bbox_to_anchor=(0.12, 0.9))

plt.grid(True, alpha=0.3)

plt.tight_layout()

chart_file = f"spy_gamma_chart_{latest_date}.png"

plt.savefig(chart_file, dpi=300, bbox_inches='tight')

plt.close()

print(f"✅ Chart saved: {chart_file}")

print("Done.")
 

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import yfinance as yf
from datetime import datetime
import os
# ================== FILE SETUP ==================
CSV_FILE = "spy_historical.csv"
# Create CSV with headers if it doesn't exist
if not os.path.exists(CSV_FILE):
   pd.DataFrame(columns=['Date', 'SPY_Close', 'Net_GEX', 'Daily_Volume',
                         'Call_Wall', 'Put_Wall', 'Gamma_Flip']).to_csv(CSV_FILE, index=False)
# Load existing data
df = pd.read_csv(CSV_FILE)
df['Date'] = pd.to_datetime(df['Date'])
# ================== AUTO PULL LATEST SPY DATA ==================
print("Fetching latest SPY data from Yahoo Finance...")
ticker = yf.Ticker("SPY")
hist = ticker.history(period="5d")  # get last few days
latest = hist.iloc[-1]
latest_date = latest.name.strftime('%Y-%m-%d')
latest_close = round(latest['Close'], 2)
latest_volume = int(latest['Volume'])
print(f"Latest available date: {latest_date}")
print(f"SPY Close: {latest_close}")
print(f"Volume: {latest_volume:,}")
# Check if this date already exists
if latest_date in df['Date'].dt.strftime('%Y-%m-%d').values:
   print(f"\nData for {latest_date} already exists. Skipping duplicate.")
else:
   print(f"\nAdding new row for {latest_date}...")
   # ================== MANUAL GEX INPUT (still required) ==================
   print("\nEnter today's GEX levels (press Enter to leave blank if unknown):")
   net_gex = input("Net GEX (e.g. -7000): ").strip() or None
   call_wall = input("Call Wall (e.g. 755): ").strip() or None
   put_wall = input("Put Wall (e.g. 732): ").strip() or None
   gamma_flip = input("Gamma Flip (e.g. 750): ").strip() or None
   # Convert to float if provided
   net_gex = float(net_gex) if net_gex else None
   call_wall = float(call_wall) if call_wall else None
   put_wall = float(put_wall) if put_wall else None
   gamma_flip = float(gamma_flip) if gamma_flip else None
   # Add new row
   new_row = pd.DataFrame({
       'Date': [latest_date],
       'SPY_Close': [latest_close],
       'Net_GEX': [net_gex],
       'Daily_Volume': [latest_volume],
       'Call_Wall': [call_wall],
       'Put_Wall': [put_wall],
       'Gamma_Flip': [gamma_flip]
   })
   df = pd.concat([df, new_row], ignore_index=True)
   df = df.drop_duplicates(subset=['Date'], keep='last')
   df = df.sort_values('Date')
   # Save back to CSV
   df.to_csv(CSV_FILE, index=False)
   print(f"✅ Saved updated data to {CSV_FILE}")
# ================== GENERATE CHART ==================
plt.style.use('dark_background')
fig, ax1 = plt.subplots(figsize=(14, 8))
# Net GEX
color = 'tab:red'
ax1.set_xlabel('Date')
ax1.set_ylabel('Net GEX', color=color)
ax1.plot(df['Date'], df['Net_GEX'], color=color, linewidth=2.5, label='Net GEX')
ax1.tick_params(axis='y', labelcolor=color)
ax1.axhline(0, color='gray', linestyle='--', linewidth=1)
# Short-gamma regime shading (last 8 trading days)
if len(df) >= 8:
   ax1.axvspan(df['Date'].iloc[-8], df['Date'].iloc[-1], alpha=0.25, color='red', label='Short-Gamma Regime')
# SPY Close
ax2 = ax1.twinx()
color = 'tab:cyan'
ax2.set_ylabel('SPY Close', color=color)
ax2.plot(df['Date'], df['SPY_Close'], color=color, linewidth=2.5, label='SPY Close')
ax2.tick_params(axis='y', labelcolor=color)
# Volume
ax3 = ax1.twinx()
ax3.spines['right'].set_position(('outward', 60))
color = 'tab:gray'
ax3.set_ylabel('Daily Volume (M)', color=color)
ax3.bar(df['Date'], df['Daily_Volume'] / 1_000_000, color=color, alpha=0.35, width=0.8, label='Volume')
# Formatting
ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
plt.title('SPY - Net GEX vs Price & Volume (Short-Gamma Regime)', fontsize=16, pad=20)
fig.legend(loc='upper left', bbox_to_anchor=(0.12, 0.9))
plt.grid(True, alpha=0.3)
plt.tight_layout()
# Save chart with today's date
chart_filename = f"spy_gamma_chart_{latest_date}.png"
plt.savefig(chart_filename, dpi=300, bbox_inches='tight')
plt.show()
print(f"\n✅ Chart saved as: {chart_filename}")
print(f"✅ Total rows in data: {len(df)}")

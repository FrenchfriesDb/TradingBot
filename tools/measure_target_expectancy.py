"""Is ANY target multiple positive in this regime? One pass, whole curve.

    python3 tools/measure_target_expectancy.py

Records, for every demand-zone tap, the best R reached BEFORE the stop was touched. From
that one number the whole win-rate/expectancy curve falls out for any target, without
re-running anything.

WHAT IT IS NOT. This is the RAW setup: fill at the zone top, stop at the zone low, 20-bar
horizon, no displacement gate, no candle-3 rule, no retest requirement, no AI. The live
bot applies all of those and should therefore select a BETTER subset than this — the
crypto c3 measurement suggested it does (89% vs 54% on n=19, small). Read this as the
floor the filters have to beat, not as the bot's expectancy.

Costs are modelled as a flat 0.1R, which is generous for commission-free stocks and far
too kind for crypto, where 0.25%/side on a 1.5% stop is nearer 0.33R.
"""
import sys; sys.path.insert(0, "/Users/usahealthlife/Desktop/TradingBot")
import datetime as dt, pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from config import API_KEY, API_SECRET
from bot.indicators import find_demand_zone, find_supply_zone

SYMS = ["AAPL","QQQ","SPY","NVDA","TSLA","GOOGL","META","MSFT"]
c = StockHistoricalDataClient(API_KEY, API_SECRET)
bars = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, feed=DataFeed.IEX,
        timeframe=TimeFrame(4, TimeFrameUnit.Hour),
        start=dt.datetime(2026,4,1), end=dt.datetime(2026,9,29))).df

def max_r_before_stop(fut, entry, stop, is_long, horizon):
    """Best R reached before the stop was touched. None if never tapped."""
    R = abs(entry - stop)
    if R <= 0: return None
    best = 0.0
    for _, b in fut.head(horizon).iterrows():
        h, l = float(b["high"]), float(b["low"])
        adverse = l if is_long else h
        fav     = h if is_long else l
        hit_stop = (adverse <= stop) if is_long else (adverse >= stop)
        r_now = ((fav - entry) if is_long else (entry - fav)) / R
        best = max(best, r_now)
        if hit_stop:
            return best if best > 0 else 0.0
    return best

rows = []
for sym in SYMS:
    try: d = bars.loc[sym].reset_index()
    except KeyError: continue
    for i in range(60, len(d)-25):
        w = d.iloc[:i+1]; px = float(w["close"].iloc[-1])
        found, lo, hi, kind = find_demand_zone(w, px, max_distance_pct=0.08)
        if not found: continue
        R = hi - lo
        if R <= 0: continue
        entry, stop = hi, lo - R * 0.0   # fill at the zone top, stop at its low
        fut = d.iloc[i+1:]
        # only count it if price actually reached the zone
        tap = fut.head(20)[fut.head(20)["low"] <= hi]
        if tap.empty: continue
        best = max_r_before_stop(fut.loc[tap.index[0]:], entry, lo, True, 20)
        if best is not None: rows.append(best)

print(f"zone taps measured: {len(rows)}\n")
print(f"{'target':>7} {'win rate':>9} {'gross exp':>10} {'net of 0.1R costs':>18}")
for T in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
    wins = sum(1 for b in rows if b >= T)
    wr = wins/len(rows) if rows else 0
    gross = wr*T - (1-wr)*1
    print(f"{T:6.1f}R {wr*100:8.0f}% {gross:+9.2f}R {gross-0.1:+17.2f}R")
import statistics as st
print(f"\n  median best-R reached: {st.median(rows):.2f}R   "
      f"75th pct: {sorted(rows)[int(len(rows)*.75)]:.2f}R   max: {max(rows):.2f}R")

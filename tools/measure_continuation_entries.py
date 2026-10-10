"""Continuation entries vs the pullback baseline, scored identically.

    python3 tools/measure_continuation_entries.py

After an impulse that clears the displacement gates and agrees with the daily trend,
score four entry styles by the best R reached before the stop: buy the impulse close, or
wait for a 25% / 50% / 62% retrace of it. Stop sits at the impulse low for all four.

CAVEATS, because the headline gradient is partly explained by them:
  * The shared stop means a DEEPER entry has a TIGHTER R — at 62% retrace R is only 38%
    of the impulse range, so "1R" is a smaller absolute move than for a shallow entry.
    Fair in R terms, and the tighter stop's extra stop-outs are captured in the win rate,
    but the effect is partly better risk PLACEMENT rather than better timing.
  * n is ~45 per style. Directional, not settled.
  * 4H bars hide intrabar sequencing: a bar that both fills and stops is counted as
    filled-then-stopped, which flatters all four styles equally.
  * Deeper entries miss more often (nofill rises 1 -> 6), so the edge costs frequency.
"""
import sys; sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import os
import datetime as dt, pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from config import API_KEY, API_SECRET
from bot.indicators import displacement_gates, range_atr, get_daily_trend

SYMS = ["AAPL","QQQ","SPY","NVDA","TSLA","GOOGL","META","MSFT"]
c = StockHistoricalDataClient(API_KEY, API_SECRET)
bars = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, feed=DataFeed.IEX,
        timeframe=TimeFrame(4, TimeFrameUnit.Hour),
        start=dt.datetime(2026,4,1), end=dt.datetime(2026,9,29))).df

HORIZON = 20

def best_r(fut, entry, stop, is_long):
    R = abs(entry - stop)
    if R <= 0: return None
    best, filled = 0.0, False
    for _, b in fut.head(HORIZON).iterrows():
        h, l = float(b["high"]), float(b["low"])
        if not filled:
            filled = (l <= entry) if is_long else (h >= entry)
            if not filled: continue
        adverse = l if is_long else h
        fav     = h if is_long else l
        best = max(best, ((fav-entry) if is_long else (entry-fav))/R)
        if (adverse <= stop) if is_long else (adverse >= stop):
            return best
    return best if filled else None

styles = {"momentum (close of impulse)": 0.0, "shallow pullback 25%": 0.25,
          "OTE-ish pullback 50%": 0.50, "deep pullback 62%": 0.62}
res = {k: [] for k in styles}
nofill = {k: 0 for k in styles}

for sym in SYMS:
    try: d = bars.loc[sym].reset_index()
    except KeyError: continue
    for i in range(60, len(d)-HORIZON-1):
        w = d.iloc[:i+1]
        bar = d.iloc[i]
        body = abs(float(bar.close)-float(bar.open))
        rng  = float(bar.high)-float(bar.low)
        if rng <= 0: continue
        g = displacement_gates(w, 1.8, 0.0015, 0.0015)
        if body < g.get("min_body_abs", 0) or body/rng < 0.4: continue   # a real impulse
        is_long = float(bar.close) > float(bar.open)
        trend = get_daily_trend(w)                      # trade WITH the daily trend only
        if (trend == "bullish") != is_long: continue
        hi, lo = float(bar.high), float(bar.low)
        fut = d.iloc[i+1:]
        for name, frac in styles.items():
            entry = (hi - (hi-lo)*frac) if is_long else (lo + (hi-lo)*frac)
            # STOP_MODE=atr gives every style the SAME R, which is the control that
            # showed the depth gradient was mostly stop placement, not timing.
            if os.getenv("STOP_MODE") == "atr":
                _a = range_atr(w)
                if _a <= 0: continue
                stop = (entry - 1.5*_a) if is_long else (entry + 1.5*_a)
            else:
                stop = lo if is_long else hi             # impulse invalidation
            b = best_r(fut, entry, stop, is_long)
            if b is None: nofill[name] += 1
            else: res[name].append(b)

print(f"{'entry style':28} {'n':>5} {'nofill':>7} {'1R win':>7} {'exp@1R':>8} {'exp@1.5R':>9} {'median':>7}")
for name in styles:
    v = res[name]
    if not v: print(f"{name:28} {0:5d}"); continue
    import statistics as st
    w1  = sum(1 for b in v if b >= 1.0)/len(v)
    w15 = sum(1 for b in v if b >= 1.5)/len(v)
    print(f"{name:28} {len(v):5d} {nofill[name]:7d} {w1*100:6.0f}% "
          f"{w1*1-(1-w1):+7.2f}R {w15*1.5-(1-w15):+8.2f}R {st.median(v):6.2f}R")
print("\n  baseline (demand-zone pullback, same scoring): 55% at 1R, +0.09R, median 1.14R")

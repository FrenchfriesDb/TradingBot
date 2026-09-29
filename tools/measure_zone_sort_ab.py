"""nearest vs conviction, scored at a real 1:2 — frequency AND win rate.

    python3 tools/measure_zone_sort_ab.py

Exists because backtest_stocks.py returns NO TRADES in every configuration tried on
2026-09-29 (60d, 12 symbols, IEX, warm-up 90), so it cannot answer a comparison. This
walks real 15m bars instead: enter at the zone, stop at the zone low, target exactly 2R,
cut at the session boundary the way the EOD flatten does.

Set ZONE_SORT to pick the key; this runs both and prints them side by side.
"""
import sys, os, importlib; sys.path.insert(0,"/Users/usahealthlife/Desktop/TradingBot")
import datetime as dt, pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from config import API_KEY, API_SECRET

SYMS = ["AAPL","QQQ","SPY","NVDA","TSLA","GOOGL","META","MSFT",
        "AMZN","AMD","PLTR","NFLX","BE","AMG"]
c = StockHistoricalDataClient(API_KEY, API_SECRET)
bars = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, feed=DataFeed.IEX,
        timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        start=dt.datetime(2026,7,1), end=dt.datetime(2026,9,29))).df
MAXB = 26            # one session — the EOD flatten

def run(mode):
    os.environ["ZONE_SORT"] = mode
    import bot.indicators as ind
    importlib.reload(ind)
    armed = win = loss = unres = 0
    for s in SYMS:
        try: d = bars.loc[s].reset_index()
        except KeyError: continue
        d["day"] = pd.to_datetime(d["timestamp"]).dt.date
        for i in range(60, len(d)-MAXB-1):
            w = d.iloc[:i+1]; px = float(w["close"].iloc[-1])
            f, lo, hi, k = ind.find_demand_zone(w, px, max_distance_pct=0.08)
            if not f or hi <= lo: continue
            armed += 1
            R = hi-lo; entry, stop, tgt = hi, lo, hi + 2*R
            day = d.iloc[i]["day"]; filled=False; done=False
            for _, b in d.iloc[i+1:i+1+MAXB].iterrows():
                if b["day"] != day: break
                h, l = float(b["high"]), float(b["low"])
                if not filled:
                    filled = l <= entry
                    if not filled: continue
                if l <= stop: loss += 1; done=True; break
                if h >= tgt:  win  += 1; done=True; break
            if filled and not done: unres += 1
    return armed, win, loss, unres

print(f"{'sort':12} {'zones armed':>12} {'filled':>7} {'WIN 2R':>7} {'loss':>6} "
      f"{'unresolved':>11} {'win rate':>9} {'exp':>8}")
for mode in ("nearest", "conviction"):
    a, w, l, u = run(mode)
    filled = w+l+u
    wr = w/(w+l) if (w+l) else 0
    exp = (w*2 - l*1)/filled if filled else 0
    print(f"{mode:12} {a:12d} {filled:7d} {w:7d} {l:6d} {u:11d} {wr*100:8.0f}% {exp:+7.2f}R")

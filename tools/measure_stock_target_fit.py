"""Stocks: the hold is capped by the session, so which target fits inside it?

    python3 tools/measure_stock_target_fit.py

The crypto fix — extend STALE_TRADE_HOURS 6 -> 18 — CANNOT be ported here. A US session
is 6.5h, entries stop 30m before the close and the EOD flatten fires 15m before it, so the
maximum possible hold is 6.25h and only for an open-bell entry. Holding longer means
holding OVERNIGHT, which is what produced the 7/23 gap loss through META's stop.

So this asks the stock version of the question: inside a fixed session, which target
actually fits? Trades are cut at the session boundary, exactly as the EOD flatten does.

RAW SETUP: no displacement gate, no candle-3 rule, no retest requirement, no AI, no trend
filter. The live bot applies all of those and should select a better subset — read this as
the floor they have to beat.
"""
import sys; sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import datetime as dt, pandas as pd, statistics as st
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from config import API_KEY, API_SECRET
from bot.indicators import find_demand_zone

SYMS = ["AAPL","QQQ","SPY","NVDA","TSLA","GOOGL","META","MSFT"]
c = StockHistoricalDataClient(API_KEY, API_SECRET)
bars = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, feed=DataFeed.IEX,
        timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        start=dt.datetime(2026,7,1), end=dt.datetime(2026,9,29))).df
BAR_H = 0.25
MAXB  = 26                      # 6.5h — the whole session

paths=[]
for s in SYMS:
    try: d = bars.loc[s].reset_index()
    except KeyError: continue
    d["day"] = pd.to_datetime(d["timestamp"]).dt.date
    for i in range(60, len(d)-MAXB-1):
        w = d.iloc[:i+1]; px=float(w["close"].iloc[-1])
        f, lo, hi, k = find_demand_zone(w, px, max_distance_pct=0.08)
        if not f or hi<=lo: continue
        R=hi-lo; entry, stop = hi, lo
        day = d.iloc[i]["day"]
        filled=False; t_stop=None; tgt={}
        for n,(_,b) in enumerate(d.iloc[i+1:i+1+MAXB].iterrows(),1):
            if b["day"] != day: break         # session boundary = EOD flatten
            h,l = float(b["high"]), float(b["low"])
            if not filled:
                filled = l<=entry
                if not filled: continue
            if t_stop is None and l<=stop: t_stop=n
            for m in (1.0,1.5,2.0,2.5,3.0):
                if m not in tgt and h >= entry+m*R: tgt[m]=n
            if t_stop: break
        if filled: paths.append((t_stop, tgt, n))

print(f"stock zone taps (15m, intraday only): {len(paths)}\n")
print(f"{'target':>7} {'resolved in session':>20} {'stopped':>8} {'unresolved':>11} {'expectancy':>11}")
for m in (1.0,1.5,2.0,2.5,3.0):
    win=loss=un=0
    for t_stop, tgt, last in paths:
        t = tgt.get(m)
        if t is not None and (t_stop is None or t < t_stop): win+=1
        elif t_stop is not None: loss+=1
        else: un+=1
    n=len(paths); e=(win*m - loss*1)/n
    print(f"{m:6.1f}R {win/n*100:19.0f}% {loss/n*100:7.0f}% {un/n*100:10.0f}% {e:+10.2f}R")
hits=[t for _,tg,_ in paths for mm,t in tg.items() if mm==2.0]
if hits: print(f"\n  when 2R DOES resolve it takes {st.median(hits)*BAR_H:.1f}h (median), "
               f"{max(hits)*BAR_H:.1f}h worst — against a {6.25:.2f}h ceiling")

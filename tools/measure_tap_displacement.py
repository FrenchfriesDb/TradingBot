"""What tap-time displacement threshold actually maximises expectancy?

    python3 tools/measure_tap_displacement.py

The stock bot uses DISPLACEMENT_ATR_MULT=1.8 at the TAP — the same threshold as zone
FORMATION — and it refuses 99.2%% of taps. This asks whether that number is right.

It is not wrong in direction: the gate turns a LOSING raw setup (-0.12R at 2R with no
gate) into a winning one (+0.17R at 1.8x). That is the first direct measurement of a
filter beating the raw floor, and it vindicates the gate itself.

But 1.8x is past the peak on TOTAL return. 1.0x keeps 3.3x the trades at almost the same
per-trade edge, earning 2.4x the total R over the same window.

CAVEATS: no trend filter, no AI, no news, no retest requirement in this measurement
(zone_left passes 99%% live, so it barely binds). Session-capped like the EOD flatten.
"""
import sys; sys.path.insert(0,"/Users/usahealthlife/Desktop/TradingBot")
import datetime as dt, pandas as pd, statistics as st
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from config import API_KEY, API_SECRET
from bot.indicators import find_demand_zone, range_atr

SYMS = ["AAPL","QQQ","SPY","NVDA","TSLA","GOOGL","META","MSFT","AMZN","AMD","PLTR","NFLX"]
c = StockHistoricalDataClient(API_KEY, API_SECRET)
bars = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, feed=DataFeed.IEX,
        timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        start=dt.datetime(2026,7,1), end=dt.datetime(2026,9,29))).df
MAXB=26

taps=[]
for s in SYMS:
    try: d=bars.loc[s].reset_index()
    except KeyError: continue
    d["day"]=pd.to_datetime(d["timestamp"]).dt.date
    for i in range(60,len(d)-MAXB-1):
        w=d.iloc[:i+1]; px=float(w["close"].iloc[-1])
        f,lo,hi,k=find_demand_zone(w,px,max_distance_pct=0.08)
        if not f or hi<=lo: continue
        atr=range_atr(w)
        if atr<=0: continue
        R=hi-lo; entry,stop,tgt1,tgt2 = hi, lo, hi+R, hi+2*R
        day=d.iloc[i]["day"]
        # find the tap bar, then measure displacement over the 3 bars ending there
        fut=d.iloc[i+1:i+1+MAXB]
        tap_n=None
        for n,(_,b) in enumerate(fut.iterrows()):
            if b["day"]!=day: break
            if float(b["low"])<=entry: tap_n=i+1+n; break
        if tap_n is None: continue
        win3 = d.iloc[max(0,tap_n-2):tap_n+1]
        best=0.0
        for _,b in win3.iterrows():
            o,h,l,cl = float(b["open"]),float(b["high"]),float(b["low"]),float(b["close"])
            rng=h-l
            if rng<=0 or cl<=o: continue          # must close UP for a long
            body=abs(cl-o)
            if body/rng < 0.5: continue           # DISPLACEMENT_BODY_FRAC
            best=max(best, body/atr)
        # outcome from the tap
        out1=out2=None
        for _,b in d.iloc[tap_n:tap_n+MAXB].iterrows():
            if b["day"]!=day: break
            h,l=float(b["high"]),float(b["low"])
            if l<=stop: out1=out1 or "LOSS"; out2=out2 or "LOSS"; break
            if out1 is None and h>=tgt1: out1="WIN"
            if h>=tgt2: out2="WIN"; break
        taps.append((best,out1,out2))

print(f"taps: {len(taps)}   (displacement measured over the 3 bars ending at the tap)\n")
print(f"{'threshold':>10} {'passes':>8} {'% kept':>7} {'win@1R':>8} {'exp@1R':>8} "
      f"{'win@2R':>8} {'exp@2R':>8}")
for thr in (0.0, 0.3, 0.5, 0.8, 1.0, 1.4, 1.8, 2.5):
    sel=[t for t in taps if t[0] >= thr]
    if len(sel)<20: print(f"{thr:9.1f}x {len(sel):8d}  (too few)"); continue
    d1=[t[1] for t in sel if t[1]]; d2=[t[2] for t in sel if t[2]]
    w1=sum(1 for x in d1 if x=="WIN")/len(d1) if d1 else 0
    w2=sum(1 for x in d2 if x=="WIN")/len(d2) if d2 else 0
    print(f"{thr:9.1f}x {len(sel):8d} {len(sel)/len(taps)*100:6.0f}% "
          f"{w1*100:7.0f}% {w1*1-(1-w1):+7.2f}R {w2*100:7.0f}% {w2*2-(1-w2):+7.2f}R")

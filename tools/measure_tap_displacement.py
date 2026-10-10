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

RE-RUN 2026-10-01 WITH THE LIVE STOP — THE NUMBERS ABOVE WERE WRONG.
This tool used R = hi-lo (the raw zone height). The live bot floors its stop at
MIN_STOP_ATR_MULT x the 1H ATR anchored to the zone edge (bot/strategy.py:2136). A stop
that tight puts the 2R target far closer than it really is and flatters every win rate —
the same defect found in the crypto twin the same day. Fixed; median stop is now 1.25%% of
price (p10 0.58%%, p90 2.15%%). Corrected table, same 8,289 taps:

 threshold  passes  %kept   win@1R   exp@1R   total@1R   win@2R   exp@2R  total@2R
      0.0x    8289   100%%     49%%   -0.03R      -249R      17%%   -0.49R    -4062R
      0.8x    1174    14%%     54%%   +0.08R       +94R      22%%   -0.35R     -411R
      1.0x     820    10%%     57%%   +0.14R      +115R      24%%   -0.28R     -230R
      1.4x     464     6%%     57%%   +0.14R       +65R      28%%   -0.17R      -79R
      1.8x     282     3%%     57%%   +0.14R       +39R      31%%   -0.06R      -17R
      2.5x     157     2%%     56%%   +0.12R       +19R      35%%   +0.06R       +9R

TWO THINGS CHANGE AND ONE DOES NOT.

1. THE GATE STILL WORKS, and 1.0x is still the right value. At a 1R target it lifts the
   win rate 49%% -> 57%% and expectancy -0.03R -> +0.14R, and 1.0x is the TOTAL-R peak
   (+115R) because stricter settings keep the same per-trade edge on a third of the taps.
   TAP_DISPLACEMENT_ATR_MULT = 1.0 stands.

2. THE JUSTIFICATION I SHIPPED IT ON WAS WRONG. That was "+0.14R at 2R vs -0.12R ungated".
   With a real stop those 2R figures are -0.28R and -0.49R. The +0.14R is real but it
   belongs to the 1R column.

3. THE 2R TARGET IS THE ACTUAL PROBLEM. At a realistic stop width it resolves 17-24%% of
   the time inside a session, so every threshold below 2.5x loses at 2R. This is the same
   wall tools/measure_hold_time.py hit (2R resolves ~18%% in a session) and it is why the
   operator's 1:2 goal keeps failing to appear in the results: the target is unreachable
   in the time the bot allows, not badly selected.

FEE SENSITIVITY (FEE_RATES env, 0%% = Alpaca reality, rest hypothetical): at 2R only 2.5x
clears zero at 0%%, and ANY fee sinks it. The stock edge could not survive crypto-style
taker costs — useful as the direct comparison against the crypto curve, which needs
<=0.10%%/side before anything clears zero.
"""
import sys; sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import datetime as dt, os, pandas as pd, statistics as st
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from config import API_KEY, API_SECRET
from bot.indicators import find_demand_zone, range_atr, structural_stop_price
from bot.strategy import MIN_STOP_ATR_MULT

SYMS = ["AAPL","QQQ","SPY","NVDA","TSLA","GOOGL","META","MSFT","AMZN","AMD","PLTR","NFLX"]
c = StockHistoricalDataClient(API_KEY, API_SECRET)
START, END = dt.datetime(2026,7,1), dt.datetime(2026,9,29)
bars = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, feed=DataFeed.IEX,
        timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        start=START, end=END)).df
# 1H bars, because the LIVE stop is floored at MIN_STOP_ATR_MULT x the 1H ATR
# (bot/strategy.py:2136). Using the raw zone height as the stop — what this tool did until
# 2026-10-01 — models a stop far tighter than the bot places, which puts the 2R target
# far closer than it really is and flatters every win rate. Same defect found and fixed in
# the crypto twin the same day; it is why that table went from +0.39R gross to +0.07R.
h1 = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, feed=DataFeed.IEX,
        timeframe=TimeFrame(1, TimeFrameUnit.Hour),
        start=START, end=END)).df
MAXB=26
FEES = [float(x)/100 for x in (os.getenv("FEE_RATES") or "0,0.05,0.10,0.15,0.25").split(",")]

taps=[]; stop_pcts=[]
for s in SYMS:
    try: d=bars.loc[s].reset_index()
    except KeyError: continue
    try:
        hd=h1.loc[s].reset_index()
        hd["atr"]=(hd["high"]-hd["low"]).rolling(14).mean()
        h_ts=pd.to_datetime(hd["timestamp"]).to_numpy()
        h_atr=hd["atr"].to_numpy()
    except KeyError:
        continue
    d["day"]=pd.to_datetime(d["timestamp"]).dt.date
    for i in range(60,len(d)-MAXB-1):
        w=d.iloc[:i+1]; px=float(w["close"].iloc[-1])
        f,lo,hi,k=find_demand_zone(w,px,max_distance_pct=0.08)
        if not f or hi<=lo: continue
        atr=range_atr(w)
        if atr<=0: continue
        # The bot's ACTUAL stop: anchored to the zone edge, floored at 1.5x the 1H ATR.
        j=int(h_ts.searchsorted(pd.to_datetime(w["timestamp"].iloc[-1]),side="right"))-1
        atr1h=float(h_atr[j]) if 0<=j<len(h_atr) and h_atr[j]==h_atr[j] else 0.0
        if atr1h<=0: continue
        entry=hi
        stop=structural_stop_price(entry, lo, atr1h, True, MIN_STOP_ATR_MULT, None)
        R=entry-stop
        if R<=0: continue
        tgt1,tgt2 = entry+R, entry+2*R
        stop_pcts.append(R/entry*100)
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
        taps.append((best,out1,out2,entry,R))

sp=pd.Series(stop_pcts)
print(f"taps: {len(taps)}   (displacement measured over the 3 bars ending at the tap)")
print(f"stop distance: median {sp.median():.2f}% of price  "
      f"(p10 {sp.quantile(.1):.2f}%  p90 {sp.quantile(.9):.2f}%)  "
      f"— SANITY CHECK: this is the LIVE 1.5x-1H-ATR stop, not the zone height\n")
THRS=(0.0, 0.3, 0.5, 0.8, 1.0, 1.4, 1.8, 2.5)
print(f"{'threshold':>10} {'passes':>8} {'% kept':>7} {'win@1R':>8} {'exp@1R':>8} "
      f"{'win@2R':>8} {'exp@2R':>8}")
rows=[]
for thr in THRS:
    sel=[t for t in taps if t[0] >= thr]
    if len(sel)<20: print(f"{thr:9.1f}x {len(sel):8d}  (too few)"); continue
    d1=[t[1] for t in sel if t[1]]; d2=[t[2] for t in sel if t[2]]
    w1=sum(1 for x in d1 if x=="WIN")/len(d1) if d1 else 0
    w2=sum(1 for x in d2 if x=="WIN")/len(d2) if d2 else 0
    print(f"{thr:9.1f}x {len(sel):8d} {len(sel)/len(taps)*100:6.0f}% "
          f"{w1*100:7.0f}% {w1*1-(1-w1):+7.2f}R {w2*100:7.0f}% {w2*2-(1-w2):+7.2f}R")
    rows.append((thr, sel, w2*2-(1-w2)))

# ── fee sensitivity ───────────────────────────────────────────────────────────────────
# Alpaca equities are COMMISSION-FREE, so 0% is this bot's real cost and every other
# column is a counterfactual — "what would this edge be worth if it paid crypto-style
# taker fees". It exists to put the two bots on one footing, not because the stock bot
# pays anything. fee_R = 2 * rate * entry / R, same formula as the crypto tool.
print(f"\n  NET EXPECTANCY @2R BY TAKER FEE (0% = Alpaca reality; rest are hypothetical)")
print(f"  {'gate':>10} {'passes':>8} " + " ".join(f"{r*100:>8.3f}%" for r in FEES))
print("  " + "-"*(20+10*len(FEES)))
for thr, sel, gross in rows:
    base=sum(2*t[3]/t[4] for t in sel)/len(sel)      # entry/R per tap, x2 sides
    cells=[]
    for r in FEES:
        net=gross-base*r
        cells.append(f"{net:>+8.2f}"+("*" if net>0 else " "))
    print(f"  {thr:>9.1f}x {len(sel):>8} " + " ".join(cells))
print("  " + "-"*(20+10*len(FEES)))
print("  * = net positive. Slippage and spread are NOT modelled.")

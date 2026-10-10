"""Wider stops: does a bigger R outrun the fee drag, or just move the target out of reach?

    python3 tools/measure_wider_stops.py

Both. Fee drag collapses from 0.42R to 0.07R exactly as predicted, and the gross edge
decays just as fast (+0.12R at 1.5x to -0.04R at 6x). Net improves -0.32R -> ~-0.11R and
never turns positive. A wider stop buys a cheaper trade and a worse one at the same rate.

Raw setup: no displacement gate, no c3 rule, no retest requirement, no AI. The live bot
applies all of those and should select a better subset — this is the floor they must beat.
"""
import sys; sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import ccxt, pandas as pd, statistics as st
from bot.indicators import find_demand_zone

SYMS = ["BTC/USD","ETH/USD","SOL/USD","DOGE/USD","XRP/USD","ADA/USD",
        "AVAX/USD","POL/USD","HYPE/USD","INJ/USD","SEI/USD","DRIFT/USD"]
ex = ccxt.coinbase({"enableRateLimit": True, "timeout": 20000})
HOLD = 18
FEE_SIDE = 0.0025          # the configured TAKER_FEE_RATE

data={}
for s in SYMS:
    try: data[s]=pd.DataFrame(ex.fetch_ohlcv(s,"1h",limit=300),
                              columns=["ts","open","high","low","close","volume"])
    except Exception: pass

setups=[]
for s,d in data.items():
    for i in range(60,len(d)-HOLD-1):
        w=d.iloc[:i+1]; px=float(w["close"].iloc[-1])
        f,lo,hi,k=find_demand_zone(w,px,max_distance_pct=0.08)
        if not f or hi<=lo: continue
        setups.append((hi, hi-lo, px, d.iloc[i+1:i+1+HOLD][["high","low"]].values))
print(f"setups: {len(setups)}   hold {HOLD}h   fee {FEE_SIDE*100:.2f}%/side\n")
print(f"{'stop = k x zone':>16} {'R as % px':>10} {'filled':>7} {'win@2R':>7} "
      f"{'gross':>8} {'fee in R':>9} {'NET':>8}")
for k in (1.0, 1.5, 2.0, 3.0, 4.0, 6.0):
    win=loss=unres=0; Rp=[]
    for entry, zh, px, bars in setups:
        R = zh*k
        stop, tgt = entry - R, entry + 2*R
        Rp.append(R/px)
        filled=False; done=False
        for h,l in bars:
            if not filled:
                filled = l <= entry
                if not filled: continue
            if l <= stop: loss+=1; done=True; break
            if h >= tgt:  win +=1; done=True; break
        if filled and not done: unres+=1
    n = win+loss+unres
    if not n: continue
    gross = (win*2 - loss)/n
    medR = st.median(Rp)
    fee_r = (FEE_SIDE*2)/medR
    wr = win/(win+loss) if (win+loss) else 0
    print(f"{k:14.1f}x {medR*100:9.2f}% {n:7d} {wr*100:6.0f}% "
          f"{gross:+7.2f}R {fee_r:8.2f}R {gross-fee_r:+7.2f}R")

"""Does a resting limit at the zone edge fill often enough to be worth the cheaper fee?

    python3 tools/measure_maker_fills.py

FILL: yes. 81%%, and IDENTICAL for touch and trade-through — when price reaches a zone it
goes through it, so a resting order is consumed rather than merely tagged at the queue
head. Maker entry is mechanically viable.

ECONOMICS: no. The gross edge is +0.11R and the stop-loss is inherently a TAKER order.
Even at zero maker fees on entry and target, the unavoidable stop fee alone costs 0.13R
and the net is -0.02R. Fee optimisation takes this from clearly losing to break-even and
no further.

The 0.25%%/side rows assume the configured TAKER_FEE_RATE. Coinbase Advanced Trade entry
tier is nearer 0.60%% taker / 0.40%% maker, so the realistic case is WORSE than the best
row shown.
"""
import sys; sys.path.insert(0,"/Users/usahealthlife/Desktop/TradingBot")
import ccxt, pandas as pd, statistics as st
from bot.indicators import find_demand_zone

SYMS = ["BTC/USD","ETH/USD","SOL/USD","DOGE/USD","XRP/USD","ADA/USD",
        "AVAX/USD","POL/USD","HYPE/USD","INJ/USD","SEI/USD","DRIFT/USD"]
ex = ccxt.coinbase({"enableRateLimit": True, "timeout": 20000})
HOLD = 18

rows=[]
for s in SYMS:
    try: d=pd.DataFrame(ex.fetch_ohlcv(s,"1h",limit=300),
                        columns=["ts","open","high","low","close","volume"])
    except Exception: continue
    for i in range(60,len(d)-HOLD-1):
        w=d.iloc[:i+1]; px=float(w["close"].iloc[-1])
        f,lo,hi,k=find_demand_zone(w,px,max_distance_pct=0.08)
        if not f or hi<=lo: continue
        R=hi-lo; entry, stop, tgt = hi, lo, hi+2*R
        fut=d.iloc[i+1:i+1+HOLD]
        # TOUCH fill: low <= entry.  THROUGH fill: low strictly below entry, so a
        # resting order is actually consumed rather than merely tagged at the queue head.
        touch=through=False; out=None; exit_is_limit=None
        for _,b in fut.iterrows():
            l,h=float(b["low"]),float(b["high"])
            if not touch and l<=entry: touch=True
            if not through and l<entry: through=True
            if not touch: continue
            if l<=stop: out="LOSS"; exit_is_limit=False; break      # stop = market exit
            if h>=tgt:  out="WIN";  exit_is_limit=True;  break      # target = resting limit
        rows.append(dict(touch=touch, through=through, out=out, R_pct=R/px))

r=pd.DataFrame(rows)
print(f"\nzone setups: {len(r)}")
print(f"  filled on TOUCH   (optimistic): {r.touch.mean()*100:.0f}%")
print(f"  filled on THROUGH (conservative): {r.through.mean()*100:.0f}%")
med_R = st.median(r.R_pct)*100
print(f"  median R = {med_R:.2f}% of price\n")

dec = r[r.out.notna()]
w = (dec.out=="WIN").sum(); l = (dec.out=="LOSS").sum()
gross = (w*2 - l)/len(dec)
print(f"  decided {len(dec)}   win {w}  loss {l}   gross {gross:+.2f}R\n")
print(f"{'entry/exit fees':34} {'cost in R':>10} {'net':>8}")
for lbl, f_in, f_out_win, f_out_loss in (
    ("all taker 0.25% (today)",            0.25, 0.25, 0.25),
    ("maker entry 0.25 / taker exits",     0.25, 0.25, 0.25),
    ("maker 0.15 in, 0.15 TP, 0.25 SL",    0.15, 0.15, 0.25),
    ("maker 0.00 in, 0.00 TP, 0.25 SL",    0.00, 0.00, 0.25),
    ("maker 0.00 everywhere (rebate-tier)",0.00, 0.00, 0.00)):
    pw = (f_in + f_out_win)/med_R
    pl = (f_in + f_out_loss)/med_R
    cost = ((w*pw) + (l*pl))/len(dec)
    print(f"  {lbl:32} {cost:9.2f}R {gross-cost:+7.2f}R")

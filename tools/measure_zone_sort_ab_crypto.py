"""Same sort A/B on crypto — where the hold is 18h, not a session cap.

    python3 tools/measure_zone_sort_ab_crypto.py

The sort key turned out to make NO difference here (identical 37%% win rate and +0.10R
gross either way; conviction just fills 9%% less). The useful output is the fee line.

Median zone width — which IS R — is 1.12%% of price on crypto. At the 0.25%%/side taker
default that is a 0.50%% round trip, or 0.45R, against a gross edge of +0.10R. The crypto
strategy is GROSS-POSITIVE and FEE-NEGATIVE, which is a different disease from the stock
bot (gross-negative, costs near zero) and needs a different fix: maker fills, wider stops,
or a cheaper venue — not entry tuning.
"""
import sys, os, importlib; sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import ccxt, pandas as pd

SYMS = ["BTC/USD","ETH/USD","SOL/USD","DOGE/USD","XRP/USD","ADA/USD",
        "AVAX/USD","POL/USD","HYPE/USD","INJ/USD","SEI/USD","DRIFT/USD"]
ex = ccxt.coinbase({"enableRateLimit": True, "timeout": 20000})
HOLD = 18                      # bars on 1h = STALE_TRADE_HOURS
FEE_R = 0.33                   # ~0.25%/side on a ~1.5% stop

data = {}
for s in SYMS:
    try:
        data[s] = pd.DataFrame(ex.fetch_ohlcv(s, "1h", limit=300),
                               columns=["ts","open","high","low","close","volume"])
    except Exception:
        pass
print(f"symbols with data: {len(data)}   hold = {HOLD}h (STALE_TRADE_HOURS)\n")

def run(mode):
    os.environ["ZONE_SORT"] = mode
    import bot.indicators as ind
    importlib.reload(ind)
    armed=win=loss=unres=0
    for s, d in data.items():
        for i in range(60, len(d)-HOLD-1):
            w = d.iloc[:i+1]; px=float(w["close"].iloc[-1])
            f, lo, hi, k = ind.find_demand_zone(w, px, max_distance_pct=0.08)
            if not f or hi <= lo: continue
            armed += 1
            R=hi-lo; entry, stop, tgt = hi, lo, hi+2*R
            filled=False; done=False
            for _, b in d.iloc[i+1:i+1+HOLD].iterrows():
                h,l = float(b["high"]), float(b["low"])
                if not filled:
                    filled = l <= entry
                    if not filled: continue
                if l <= stop: loss+=1; done=True; break
                if h >= tgt:  win +=1; done=True; break
            if filled and not done: unres += 1
    return armed, win, loss, unres

print(f"{'sort':12} {'armed':>7} {'filled':>7} {'WIN 2R':>7} {'loss':>6} {'unres':>6} "
      f"{'win rate':>9} {'gross':>8} {'net of fees':>12}")
res={}
for mode in ("nearest","conviction"):
    a,w,l,u = run(mode); res[mode]=(a,w,l,u)
    filled=w+l+u
    wr = w/(w+l) if (w+l) else 0
    gross=(w*2-l*1)/filled if filled else 0
    print(f"{mode:12} {a:7d} {filled:7d} {w:7d} {l:6d} {u:6d} {wr*100:8.0f}% "
          f"{gross:+7.2f}R {gross-FEE_R:+11.2f}R")

"""Does the pullback setup work in some volatility regimes and not others?

    python3 tools/measure_regime_split.py

Every earlier measurement pooled all market conditions together. This splits the same
demand-zone taps by the state of the tape AT THE TAP — volatility level, whether
volatility is expanding, and whether price had actually declined into the zone.

It ends with an OUT-OF-SAMPLE check, which matters here more than usual: the volatility
threshold is something I chose after looking at the data, and that is how a finding gets
manufactured. The threshold is refitted on the first half of the period and applied blind
to the second.
"""
import sys; sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import datetime as dt, pandas as pd, statistics as st
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from config import API_KEY, API_SECRET
from bot.indicators import find_demand_zone, range_atr

SYMS = ["AAPL","QQQ","SPY","NVDA","TSLA","GOOGL","META","MSFT"]
c = StockHistoricalDataClient(API_KEY, API_SECRET)
bars = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, feed=DataFeed.IEX,
        timeframe=TimeFrame(4, TimeFrameUnit.Hour),
        start=dt.datetime(2026,4,1), end=dt.datetime(2026,9,29))).df
H = 20

def best_r(fut, entry, stop, is_long):
    R = abs(entry-stop)
    if R <= 0: return None
    best, filled = 0.0, False
    for _, b in fut.head(H).iterrows():
        h, l = float(b["high"]), float(b["low"])
        if not filled:
            filled = (l <= entry) if is_long else (h >= entry)
            if not filled: continue
        best = max(best, ((h-entry) if is_long else (entry-l))/R)
        if ((l <= stop) if is_long else (h >= stop)): return best
    return best if filled else None

rows = []
for sym in SYMS:
    try: d = bars.loc[sym].reset_index()
    except KeyError: continue
    for i in range(80, len(d)-H-1):
        w = d.iloc[:i+1]; px = float(w["close"].iloc[-1])
        found, lo, hi, kind = find_demand_zone(w, px, max_distance_pct=0.08)
        if not found or hi <= lo: continue
        atr_now  = range_atr(w)
        atr_prev = range_atr(w.iloc[:-20])
        if atr_now <= 0 or atr_prev <= 0: continue
        b = best_r(d.iloc[i+1:], hi, lo, True)
        if b is None: continue
        # regime features, measured BEFORE the outcome
        rows.append(dict(idx=i, sym=sym, best=b, expand=atr_now/atr_prev,
                         atr_pct=atr_now/px,
                         trend_up=float(w["close"].iloc[-1]) > float(w["close"].iloc[-21])))

r = pd.DataFrame(rows)
print(f"n = {len(r)}\n")

def report(label, sub):
    if len(sub) < 30: print(f"  {label:26} n={len(sub):4d}  (too few)"); return
    w1 = (sub.best >= 1.0).mean(); w15 = (sub.best >= 1.5).mean()
    print(f"  {label:26} n={len(sub):4d}  1R {w1*100:3.0f}% ({w1*1-(1-w1):+.2f}R)   "
          f"1.5R {w15*100:3.0f}% ({w15*1.5-(1-w15):+.2f}R)   med {sub.best.median():.2f}R")

print("ATR EXPANDING vs CONTRACTING (ATR now / ATR 20 bars ago)")
report("contracting  <0.9", r[r.expand < 0.9])
report("flat      0.9-1.1", r[(r.expand>=0.9)&(r.expand<=1.1)])
report("expanding    >1.1", r[r.expand > 1.1])
print("\nABSOLUTE VOLATILITY (ATR as % of price)")
q = r.atr_pct.quantile([.33,.66]).values
report(f"low   <{q[0]*100:.2f}%", r[r.atr_pct < q[0]])
report(f"mid", r[(r.atr_pct>=q[0])&(r.atr_pct<=q[1])])
report(f"high  >{q[1]*100:.2f}%", r[r.atr_pct > q[1]])
print("\n20-BAR TREND AT THE TAP")
report("price above 20 bars ago", r[r.trend_up])
report("price below 20 bars ago", r[~r.trend_up])

print("\nCOMBINED: high volatility AND a real recent decline into the zone")
q66 = r.atr_pct.quantile(.66)
hi_vol = r.atr_pct > q66
dip    = ~r.trend_up
report("high vol + dip", r[hi_vol & dip])
report("high vol, no dip", r[hi_vol & ~dip])
report("low/mid vol + dip", r[~hi_vol & dip])
report("low/mid vol, no dip", r[~hi_vol & ~dip])
print("\n  (the bot today trades ALL of these — it has no volatility or dip condition)")


print("\n" + "="*64)
print("OUT-OF-SAMPLE: thresholds fixed on the FIRST half, applied to the SECOND")
half = {}
for sym, sub in r.groupby("sym"):
    half[sym] = sub.idx.median()
r["late"] = r.apply(lambda x: x.idx > half[x.sym], axis=1)
early, late = r[~r.late], r[r.late]
q_in = early.atr_pct.quantile(.66)          # threshold chosen on EARLY data only
print(f"  threshold from first half: ATR > {q_in*100:.2f}% of price")
for label, sub in (("FIRST half (in-sample)", early), ("SECOND half (out-of-sample)", late)):
    sel = sub[(sub.atr_pct > q_in) & (~sub.trend_up)]
    rest = sub[~((sub.atr_pct > q_in) & (~sub.trend_up))]
    if len(sel) < 25: print(f"  {label:30} n={len(sel)} too few"); continue
    w = (sel.best >= 1.5).mean(); wr = (rest.best >= 1.5).mean()
    print(f"  {label:30} selected n={len(sel):3d}  1.5R {w*100:3.0f}% ({w*1.5-(1-w):+.2f}R)"
          f"   |  everything else n={len(rest):4d} ({wr*1.5-(1-wr):+.2f}R)")

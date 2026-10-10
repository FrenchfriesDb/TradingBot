"""What hold time would let a 2R target actually resolve?

    python3 tools/measure_hold_time.py

Walks each demand-zone tap forward up to 72 bars, recording when 2R was reached and when
the stop was hit, then replays the same paths under different hold windows.

VALIDATION: at the bot's current 6h it reports 58%% of trades timing out, against 51%%
STALE exits in the live log. The model tracks reality closely enough to act on.

COSTS ARE NOT MODELLED. Crypto runs ~0.25%%/side, which on a ~1.5%% stop is ~0.33R round
trip — subtract that from every expectancy below before drawing conclusions.
"""
import sys; sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import ccxt, pandas as pd, statistics as st
from bot.indicators import find_demand_zone

ex = ccxt.coinbase({"enableRateLimit": True, "timeout": 20000})
SYMS = ["BTC/USD","ETH/USD","SOL/USD","DOGE/USD","XRP/USD","ADA/USD","AVAX/USD","POL/USD"]
HOLDS = [6, 12, 18, 24, 36, 48, 72]        # bars on 1h = hours
MAXH  = max(HOLDS)

paths = []
for s in SYMS:
    try: o = ex.fetch_ohlcv(s, "1h", limit=300)
    except Exception: continue
    d = pd.DataFrame(o, columns=["ts","open","high","low","close","volume"])
    for i in range(80, len(d)-MAXH-1):
        w = d.iloc[:i+1]; px = float(w["close"].iloc[-1])
        f, lo, hi, k = find_demand_zone(w, px, max_distance_pct=0.08)
        if not f or hi <= lo: continue
        R = hi - lo
        entry, stop, tgt = hi, lo, hi + 2*R
        filled = False
        t_stop = t_tgt = None
        r_at = {}
        for n, (_, b) in enumerate(d.iloc[i+1:i+1+MAXH].iterrows(), 1):
            h, l = float(b["high"]), float(b["low"])
            if not filled:
                filled = l <= entry
                if not filled: continue
            if t_stop is None and l <= stop: t_stop = n
            if t_tgt  is None and h >= tgt:  t_tgt  = n
            r_at[n] = (float(b["close"]) - entry) / R
            if t_stop and t_tgt: break
        if filled: paths.append((t_stop, t_tgt, r_at))

print(f"zone taps: {len(paths)}   (crypto 1h; the bot's STALE_TRADE_HOURS = 6)\n")
print(f"{'hold':>6} {'hit 2R':>7} {'stopped':>8} {'timed out':>10} {'expectancy':>11} {'med exit R':>11}")
for H in HOLDS:
    win = loss = to = 0; rs = []
    for t_stop, t_tgt, r_at in paths:
        hit_t = t_tgt is not None and t_tgt <= H
        hit_s = t_stop is not None and t_stop <= H
        if hit_t and (not hit_s or t_tgt < t_stop): win += 1; rs.append(2.0)
        elif hit_s: loss += 1; rs.append(-1.0)
        else:
            to += 1
            last = max([n for n in r_at if n <= H], default=None)
            rs.append(r_at.get(last, 0.0) if last else 0.0)
    n = len(paths)
    print(f"{H:5d}h {win/n*100:6.0f}% {loss/n*100:7.0f}% {to/n*100:9.0f}% "
          f"{sum(rs)/n:+10.2f}R {st.median(rs):+10.2f}R")

"""Does expectancy actually rise with volatility — or was August just unlucky?

WHY (2026-10-03). Stock expectancy went +0.76R (Jun) -> +0.38R (Jul) -> -0.02R (Aug) ->
+0.02R (Sep) while the tape compressed: 4H ATR 1.93% -> 1.50%, daily range -33%, and the
1R hit rate 13% -> 8%. The bot HAS a volatility floor (MIN_ATR_PCT = 0.40% of 4H ATR) but
the 10th percentile of that value is 1.09% — it has never fired and cannot at that setting.

The tempting move is to raise the floor to ~1.7%, which would have skipped Aug-Sep and kept
Jun-Jul. That is fitting a threshold to FOUR monthly data points, which is how you build a
filter that works perfectly on the past and not at all going forward.

So instead: bucket every TAP by the 4H ATR at entry and measure expectancy per bucket. With
thousands of taps the data names the threshold, or shows there isn't one.

    python3 tools/measure_vol_buckets.py

Same zone + 1H-ATR-floored stop as live, scored to the live 1R target, session-capped.
A MONOTONIC rise across buckets = a real volatility dependence worth gating on.
A FLAT profile = August was something else and a volatility filter would be superstition.
"""
import datetime as dt
import io
import re
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/usahealthlife/Desktop/TradingBot")
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

from config import API_KEY, API_SECRET
from bot.indicators import find_demand_zone, range_atr, structural_stop_price
from bot.strategy import MIN_STOP_ATR_MULT

# MIN_ATR_PCT is a LOCAL inside on_trading_iteration, not a module constant — so it cannot
# be imported, and it cannot be swept via env either. Read it out of the source so this
# tool always reflects the live value instead of a copy that can drift.
_m = re.search(r"MIN_ATR_PCT\s*=\s*([0-9.]+)",
               io.open("bot/strategy.py", encoding="utf-8").read())
MIN_ATR_PCT = float(_m.group(1)) if _m else 0.004

SYMS = ["AAPL", "QQQ", "SPY", "NVDA", "TSLA", "GOOGL", "META", "MSFT",
        "AMZN", "AMD", "PLTR", "NFLX"]
START, END = dt.datetime(2026, 6, 1), dt.datetime(2026, 10, 4)
MAXB = 26
EDGES = [0.0, 1.00, 1.30, 1.60, 1.90, 2.30, 99.0]      # 4H ATR as % of price


def main():
    c = StockHistoricalDataClient(API_KEY, API_SECRET)

    def bars(n, unit):
        return c.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, feed=DataFeed.IEX,
                timeframe=TimeFrame(n, unit), start=START, end=END)).df

    d15, h1, h4 = bars(15, TimeFrameUnit.Minute), bars(1, TimeFrameUnit.Hour), bars(4, TimeFrameUnit.Hour)

    buckets = defaultdict(list)
    for s in SYMS:
        try:
            d, hd, fd = d15.loc[s].reset_index(), h1.loc[s].reset_index(), h4.loc[s].reset_index()
        except KeyError:
            continue
        d["t"] = pd.to_datetime(d["timestamp"], utc=True).dt.tz_localize(None)
        d["day"] = d["t"].dt.date
        hd["atr"] = (hd["high"] - hd["low"]).rolling(14).mean()
        h_ts = pd.to_datetime(hd["timestamp"], utc=True).dt.tz_localize(None).to_numpy()
        h_atr = hd["atr"].to_numpy()
        fd["atrp"] = (fd["high"] - fd["low"]).rolling(14).mean() / fd["close"] * 100
        f_ts = pd.to_datetime(fd["timestamp"], utc=True).dt.tz_localize(None).to_numpy()
        f_ap = fd["atrp"].to_numpy()

        for i in range(60, len(d) - MAXB - 1):
            w = d.iloc[:i + 1]
            px = float(w["close"].iloc[-1])
            f, lo, hi, _k = find_demand_zone(w, px, max_distance_pct=0.08)
            if not f or hi <= lo:
                continue
            if range_atr(w) <= 0:
                continue
            now = w["t"].iloc[-1].to_datetime64()
            k = int(h_ts.searchsorted(now, side="right")) - 1
            atr1h = float(h_atr[k]) if 0 <= k < len(h_atr) and h_atr[k] == h_atr[k] else 0.0
            j4 = int(f_ts.searchsorted(now, side="right")) - 1
            atrp = float(f_ap[j4]) if 0 <= j4 < len(f_ap) and f_ap[j4] == f_ap[j4] else None
            if atr1h <= 0 or atrp is None:
                continue
            tap = None
            for j in range(i + 1, min(i + 1 + MAXB, len(d))):
                if d["day"].iloc[j] != d["day"].iloc[i]:
                    break
                if float(d["low"].iloc[j]) <= hi:
                    tap = j
                    break
            if tap is None:
                continue
            entry = hi
            stop = structural_stop_price(entry, lo, atr1h, True, MIN_STOP_ATR_MULT, None)
            R = entry - stop
            if R <= 0:
                continue
            tgt = entry + R                       # the live 1R target
            out = None
            last = entry
            for j in range(tap, min(tap + MAXB, len(d))):
                if d["day"].iloc[j] != d["day"].iloc[tap]:
                    break
                lo_j, hi_j, last = float(d["low"].iloc[j]), float(d["high"].iloc[j]), float(d["close"].iloc[j])
                if lo_j <= stop:
                    out = -1.0
                    break
                if hi_j >= tgt:
                    out = 1.0
                    break
            if out is None:
                out = (last - entry) / R          # EOD flatten at its realized R
            b = next(x for x in range(len(EDGES) - 1) if EDGES[x] <= atrp < EDGES[x + 1])
            buckets[b].append(out)
        print(f"  {s} done", flush=True)

    tot = sum(len(v) for v in buckets.values())
    print(f"\n  EXPECTANCY BY 4H-ATR BUCKET — {tot:,} taps, {len(SYMS)} symbols, "
          f"{START:%Y-%m-%d}..{END:%Y-%m-%d}")
    print(f"  live floor MIN_ATR_PCT = {MIN_ATR_PCT*100:.2f}% (never binds; p10 is ~1.09%)\n")
    print(f"  {'4H ATR %':<14} {'n':>7} {'share':>7} {'win%':>7} {'exp R':>8} {'tot R':>9}")
    print("  " + "-" * 56)
    for b in sorted(buckets):
        v = buckets[b]
        lo_e, hi_e = EDGES[b], EDGES[b + 1]
        label = f"{lo_e:.2f}-{hi_e:.2f}" if hi_e < 90 else f"{lo_e:.2f}+"
        w = sum(1 for x in v if x > 0) / len(v) * 100
        e = sum(v) / len(v)
        print(f"  {label:<14} {len(v):>7} {len(v)/tot*100:>6.0f}% {w:>6.0f}% "
              f"{e:>+8.3f} {e*len(v):>+9.0f}")
    print("  " + "-" * 56)
    print("  MONOTONIC rise = a real volatility dependence, and the data names the floor.")
    print("  FLAT = August was something else; a volatility filter would be superstition.")


if __name__ == "__main__":
    main()

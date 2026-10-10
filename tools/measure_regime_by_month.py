"""What changed in August? The backtest runs TODAY's code over every month, so a drop in
expectancy from July (+0.38R) to August (-0.02R) cannot be a code change — it is the tape.

Four things that would each explain it, measured per month on the same symbols the stock
bot trades:

  TRAVEL      how far price actually goes after touching a demand zone, in R. If the 2R
              resolution rate collapsed, the market stopped paying for the same setups.
  VOLATILITY  ATR as % of price. A quieter tape makes every ATR-scaled stop smaller and
              every target nearer in absolute terms, but travel shrinks with it.
  FOLLOW      of the bars that close up, how many are followed by another up close —
              trend persistence. Chop looks identical to trend in a single bar.
  RANGE       daily high-low as % of open. Separates "quiet" from "wide but directionless".

    python3 tools/measure_regime_by_month.py

Uses the SAME find_demand_zone + 1H-ATR-floored stop as the live bot and the tap studies,
so the travel number is comparable to the +0.24R / -0.02R expectancies it is explaining.
"""
import datetime as dt
import sys
from collections import defaultdict

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

from config import API_KEY, API_SECRET
from bot.indicators import find_demand_zone, range_atr, structural_stop_price
from bot.strategy import MIN_STOP_ATR_MULT

SYMS = ["AAPL", "NVDA", "GOOGL", "AMZN", "AMD", "PLTR", "NFLX"]
START, END = dt.datetime(2026, 6, 1), dt.datetime(2026, 10, 4)
MAXB = 26


def main():
    c = StockHistoricalDataClient(API_KEY, API_SECRET)
    d15 = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, feed=DataFeed.IEX,
            timeframe=TimeFrame(15, TimeFrameUnit.Minute), start=START, end=END)).df
    h1 = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, feed=DataFeed.IEX,
            timeframe=TimeFrame(1, TimeFrameUnit.Hour), start=START, end=END)).df
    dd = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, feed=DataFeed.IEX,
            timeframe=TimeFrame(1, TimeFrameUnit.Day), start=START, end=END)).df

    travel = defaultdict(list)      # month -> [max favourable excursion in R]
    hit1, hit2 = defaultdict(int), defaultdict(int)
    taps = defaultdict(int)
    vol, rng, follow = defaultdict(list), defaultdict(list), defaultdict(list)

    for s in SYMS:
        try:
            d, hd, dayd = d15.loc[s].reset_index(), h1.loc[s].reset_index(), dd.loc[s].reset_index()
        except KeyError:
            continue
        d["t"] = pd.to_datetime(d["timestamp"], utc=True).dt.tz_localize(None)
        d["day"] = d["t"].dt.date
        hd["atr"] = (hd["high"] - hd["low"]).rolling(14).mean()
        h_ts = pd.to_datetime(hd["timestamp"], utc=True).dt.tz_localize(None).to_numpy()
        h_atr = hd["atr"].to_numpy()

        # daily range + volatility
        for _, b in dayd.iterrows():
            m = pd.Timestamp(b["timestamp"]).strftime("%Y-%m")
            o = float(b["open"])
            if o > 0:
                rng[m].append((float(b["high"]) - float(b["low"])) / o * 100)

        # trend persistence on 15m closes
        up = (d["close"] > d["open"]).to_numpy()
        for i in range(len(up) - 1):
            if up[i]:
                follow[d["t"].iloc[i].strftime("%Y-%m")].append(1.0 if up[i + 1] else 0.0)

        for i in range(60, len(d) - MAXB - 1):
            w = d.iloc[:i + 1]
            px = float(w["close"].iloc[-1])
            f, lo, hi, _k = find_demand_zone(w, px, max_distance_pct=0.08)
            if not f or hi <= lo:
                continue
            atr15 = range_atr(w)
            if atr15 <= 0:
                continue
            m = w["t"].iloc[-1].strftime("%Y-%m")
            vol[m].append(atr15 / px * 100)
            k = int(h_ts.searchsorted(w["t"].iloc[-1].to_datetime64(), side="right")) - 1
            atr1h = float(h_atr[k]) if 0 <= k < len(h_atr) and h_atr[k] == h_atr[k] else 0.0
            if atr1h <= 0:
                continue
            # tap, then MAX FAVOURABLE EXCURSION in R before the stop or the session end
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
            taps[m] += 1
            best = 0.0
            for j in range(tap, min(tap + MAXB, len(d))):
                if d["day"].iloc[j] != d["day"].iloc[tap]:
                    break
                if float(d["low"].iloc[j]) <= stop:
                    break
                best = max(best, (float(d["high"].iloc[j]) - entry) / R)
            travel[m].append(best)
            if best >= 1.0:
                hit1[m] += 1
            if best >= 2.0:
                hit2[m] += 1
        print(f"  {s} done", flush=True)

    months = sorted(travel)
    print(f"\n  WHAT CHANGED BY MONTH — {len(SYMS)} symbols, same zone/stop model as live\n")
    print(f"  {'month':<9} {'taps':>6} {'med travel':>11} {'hit 1R':>8} {'hit 2R':>8} "
          f"{'ATR%':>7} {'day rng%':>9} {'follow%':>8}")
    print("  " + "-" * 74)
    for m in months:
        tv = pd.Series(travel[m])
        print(f"  {m:<9} {taps[m]:>6} {tv.median():>10.2f}R "
              f"{hit1[m]/max(1,taps[m])*100:>7.0f}% {hit2[m]/max(1,taps[m])*100:>7.0f}% "
              f"{pd.Series(vol[m]).median():>6.2f}% {pd.Series(rng[m]).median():>8.2f}% "
              f"{pd.Series(follow[m]).mean()*100:>7.0f}%")
    print("  " + "-" * 74)
    print("  med travel = median MAX FAVOURABLE EXCURSION after a tap, in R.")
    print("  hit 1R / 2R = share of taps that reached that target before the stop.")
    print("  follow% = of 15m bars closing UP, how many are followed by another UP close.")


if __name__ == "__main__":
    main()

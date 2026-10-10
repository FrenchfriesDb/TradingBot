"""The 2D vertex: tap gate x target R:R, on one tap population.

WHY (2026-10-03). The 1D sweeps each found a vertex — tap gate peaks at 1.0x on TOTAL R
(+115R), and 1R beats 2R as a target (64% x 1R beats ~20% x 2R). But they were found
INDEPENDENTLY, and they interact: a looser gate lets through weaker setups that may still
reach a NEARER target, so the joint optimum need not be the pair of separate optima.

Both dials are measurable on the same taps, so a grid is honest here in a way that sweeping
five parameters against 53 trades would not be. Each cell carries real n.

    python3 tools/measure_gate_x_target.py

Live stop model (1.5x 1H ATR floor), session-capped, unresolved trades scored at the EOD
flatten for their realized R — the same scoring as backtest_stocks, so the numbers are
comparable to the live config's +0.24R.
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
from bot.indicators import (find_demand_zone, range_atr, structural_stop_price,
                            has_displacement, displacement_min_body)
from bot.strategy import (MIN_STOP_ATR_MULT, DISPLACEMENT_BODY_FRAC, DISPLACEMENT_MIN_PCT)

SYMS = ["AAPL", "QQQ", "SPY", "NVDA", "TSLA", "GOOGL", "META", "MSFT",
        "AMZN", "AMD", "PLTR", "NFLX"]
START, END = dt.datetime(2026, 7, 1), dt.datetime(2026, 10, 3)
MAXB = 26
GATES = [0.0, 0.5, 0.8, 1.0, 1.4, 1.8]
TARGETS = [0.8, 1.0, 1.2, 1.5, 2.0]


def main():
    c = StockHistoricalDataClient(API_KEY, API_SECRET)
    d15 = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, feed=DataFeed.IEX,
            timeframe=TimeFrame(15, TimeFrameUnit.Minute), start=START, end=END)).df
    h1 = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, feed=DataFeed.IEX,
            timeframe=TimeFrame(1, TimeFrameUnit.Hour), start=START, end=END)).df

    taps = []          # (gate_value, {target: realized_R})
    for s in SYMS:
        try:
            d, hd = d15.loc[s].reset_index(), h1.loc[s].reset_index()
        except KeyError:
            continue
        d["t"] = pd.to_datetime(d["timestamp"], utc=True).dt.tz_localize(None)
        d["day"] = d["t"].dt.date
        hd["atr"] = (hd["high"] - hd["low"]).rolling(14).mean()
        h_ts = pd.to_datetime(hd["timestamp"], utc=True).dt.tz_localize(None).to_numpy()
        h_atr = hd["atr"].to_numpy()

        for i in range(60, len(d) - MAXB - 1):
            w = d.iloc[:i + 1]
            px = float(w["close"].iloc[-1])
            f, lo, hi, _k = find_demand_zone(w, px, max_distance_pct=0.08)
            if not f or hi <= lo:
                continue
            atr15 = range_atr(w)
            if atr15 <= 0:
                continue
            k = int(h_ts.searchsorted(w["t"].iloc[-1].to_datetime64(), side="right")) - 1
            atr1h = float(h_atr[k]) if 0 <= k < len(h_atr) and h_atr[k] == h_atr[k] else 0.0
            if atr1h <= 0:
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
            # displacement over the 3 bars ending at the tap, in ATRs
            best = 0.0
            for j in range(max(0, tap - 2), tap + 1):
                b = d.iloc[j]
                o, h_, l_, cl = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
                rng, body = h_ - l_, abs(cl - o)
                if rng <= 0 or cl <= o or body / rng < DISPLACEMENT_BODY_FRAC:
                    continue
                best = max(best, body / atr15)
            # walk ONCE, record which targets were reached before the stop
            outs, hit = {}, {t: None for t in TARGETS}
            last = entry
            stopped = False
            for j in range(tap, min(tap + MAXB, len(d))):
                if d["day"].iloc[j] != d["day"].iloc[tap]:
                    break
                lo_j, hi_j, last = float(d["low"].iloc[j]), float(d["high"].iloc[j]), float(d["close"].iloc[j])
                if lo_j <= stop:
                    stopped = True
                    break
                for t in TARGETS:
                    if hit[t] is None and hi_j >= entry + t * R:
                        hit[t] = t
            flat = (last - entry) / R
            for t in TARGETS:
                outs[t] = hit[t] if hit[t] is not None else (-1.0 if stopped else flat)
            taps.append((best, outs))
        print(f"  {s} done", flush=True)

    print(f"\n  GATE x TARGET — {len(taps):,} taps, {len(SYMS)} symbols, "
          f"{START:%Y-%m-%d}..{END:%Y-%m-%d}")
    print("  each cell: TOTAL R  (n)\n")
    print(f"  {'gate':<8}" + "".join(f"{str(t)+'R':>16}" for t in TARGETS))
    print("  " + "-" * (8 + 16 * len(TARGETS)))
    best_cell = None
    for g in GATES:
        sel = [o for b, o in taps if b >= g]
        row = f"  {str(g)+'x':<8}"
        for t in TARGETS:
            if len(sel) < 20:
                row += f"{'thin':>16}"
                continue
            tot = sum(o[t] for o in sel)
            row += f"{tot:>+11.0f}R ({len(sel):>3})"[:16].rjust(16)
            if best_cell is None or tot > best_cell[0]:
                best_cell = (tot, g, t, len(sel), tot / len(sel))
        print(row)
    print("  " + "-" * (8 + 16 * len(TARGETS)))
    if best_cell:
        tot, g, t, n, exp = best_cell
        print(f"  VERTEX: gate {g}x, target {t}R  ->  {tot:+.0f}R total "
              f"over {n} taps ({exp:+.3f}R each)")
        print(f"  live config is gate 1.0x / target 1.0R.")


if __name__ == "__main__":
    main()

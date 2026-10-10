"""Can a BREAKOUT entry catch the moves the retest model structurally misses?

WHY (2026-10-02). TSLA opened +5% on Q3 deliveries and the bot sat in SWEEP_HUNT all
morning: its model is BOS -> sweep -> arm zone -> price TAPS BACK -> enter, and a
gap-and-go never comes back. The zone-contact study said the same in aggregate — 120 real
zones in 60 days, only 16 ever touched. The 104 untouched ARE these moves.

The repo already has detect_bull_flag + flag_breakout_retest behind ENABLE_FLAG_CONTINUATION
(default OFF, never backtested, wired to nothing for months). But read it: it returns "the
retest of the broken edge", so turning it on would NOT have caught TSLA either. It is the
same retest bet on a different pattern.

So this measures TWO entries off the identical flag detections:

    BREAK    enter at the close of the bar that closes through flag_high. No pullback.
             This is the one that could catch a gap-and-go.
    RETEST   enter when price comes back to the broken edge (what the live flag path does).

Same stop as the live bot (structural_stop_price, floored at MIN_STOP_ATR_MULT x 1H ATR),
same 2R-before-1R scoring, same symbols/window/session cap as tools/measure_tap_displacement
so the tables sit side by side.

    python3 tools/measure_breakout_entries.py

NOTHING HERE CHANGES LIVE BEHAVIOUR. ENABLE_FLAG_CONTINUATION stays off.
"""
import datetime as dt
import os
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

from config import API_KEY, API_SECRET
from bot.indicators import detect_bull_flag, structural_stop_price
from bot.strategy import MIN_STOP_ATR_MULT

SYMS = ["AAPL","QQQ","SPY","NVDA","TSLA","GOOGL","META","MSFT","AMZN","AMD","PLTR","NFLX"]
START, END = dt.datetime(2026, 7, 1), dt.datetime(2026, 9, 29)
MAXB = 26                      # session cap, same as the tap measurement
RETEST_WINDOW = 6              # flag_breakout_retest's max_shift
FEES = [float(x)/100 for x in (os.getenv("FEE_RATES") or "0,0.05,0.10").split(",")]

c = StockHistoricalDataClient(API_KEY, API_SECRET)
bars = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, feed=DataFeed.IEX,
        timeframe=TimeFrame(15, TimeFrameUnit.Minute), start=START, end=END)).df
h1 = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, feed=DataFeed.IEX,
        timeframe=TimeFrame(1, TimeFrameUnit.Hour), start=START, end=END)).df


def score(d, entry_i, entry, stop, day):
    """Realized R by the bot's OWN exit rules: target, stop, or the EOD flatten.

    The first version returned None when a trade neither hit target nor stop before the
    close, and the caller dropped it — which threw away 30 of 31 breakouts, because a 2R
    target resolves intraday only ~20% of the time (measure_hold_time, and the tap table).
    Dropping the unresolved ones does not make them disappear; live they are FLATTENED at
    the close for whatever R they happen to be sitting at. Scoring that exit is both more
    honest and what the bot actually does.

    Returns (r1, r2, entry, R) where r1/r2 are realized R at a 1R and a 2R target.
    """
    R = entry - stop
    if R <= 0:
        return None
    t1, t2 = entry + R, entry + 2 * R
    r1 = r2 = None
    last = entry
    for j in range(entry_i + 1, min(entry_i + 1 + MAXB, len(d))):
        b = d.iloc[j]
        if b["day"] != day:
            break
        lo, hi, last = float(b["low"]), float(b["high"]), float(b["close"])
        if lo <= stop:                      # pessimistic on a same-bar tie
            return (r1 if r1 is not None else -1.0,
                    r2 if r2 is not None else -1.0, entry, R)
        if r1 is None and hi >= t1:
            r1 = 1.0
        if hi >= t2:
            return (r1 if r1 is not None else 1.0, 2.0, entry, R)
    flat = (last - entry) / R               # EOD flatten at whatever it is worth
    return (r1 if r1 is not None else flat,
            r2 if r2 is not None else flat, entry, R)


brk, rtst, flags = [], [], 0
for s in SYMS:
    try:
        d = bars.loc[s].reset_index()
        hd = h1.loc[s].reset_index()
    except KeyError:
        continue
    d["day"] = pd.to_datetime(d["timestamp"]).dt.date
    hd["atr"] = (hd["high"] - hd["low"]).rolling(14).mean()
    h_ts, h_atr = pd.to_datetime(hd["timestamp"]).to_numpy(), hd["atr"].to_numpy()
    last_fh = None
    for i in range(30, len(d) - MAXB - 1):
        w = d.iloc[:i + 1]
        found, pole_lo, pole_hi, flag_lo, flag_hi, _tgt = detect_bull_flag(w)
        if not found:
            continue
        # One entry per distinct flag: the detector keeps firing while the flag is intact.
        if last_fh is not None and abs(flag_hi - last_fh) / flag_hi < 0.002:
            continue
        # find the bar that CLOSES through the flag high
        bk = None
        for j in range(i + 1, min(i + 1 + RETEST_WINDOW * 2, len(d))):
            if d.iloc[j]["day"] != d.iloc[i]["day"]:
                break
            if float(d.iloc[j]["close"]) > flag_hi:
                bk = j
                break
        if bk is None:
            continue
        flags += 1
        last_fh = flag_hi
        k = int(h_ts.searchsorted(pd.to_datetime(d.iloc[bk]["timestamp"]), side="right")) - 1
        atr1h = float(h_atr[k]) if 0 <= k < len(h_atr) and h_atr[k] == h_atr[k] else 0.0
        if atr1h <= 0:
            continue
        day = d.iloc[bk]["day"]

        # (a) BREAK — enter at the close of the breaking bar, no pullback
        e = float(d.iloc[bk]["close"])
        r = score(d, bk, e, structural_stop_price(e, flag_lo, atr1h, True,
                                                  MIN_STOP_ATR_MULT, None), day)
        if r:
            brk.append(r)

        # (b) RETEST — enter if price comes back to the broken edge within the window
        for j in range(bk + 1, min(bk + 1 + RETEST_WINDOW, len(d))):
            if d.iloc[j]["day"] != day:
                break
            if float(d.iloc[j]["low"]) <= flag_hi:
                e2 = flag_hi
                r2 = score(d, j, e2, structural_stop_price(e2, flag_lo, atr1h, True,
                                                           MIN_STOP_ATR_MULT, None), day)
                if r2:
                    rtst.append(r2)
                break


def report(name, rows):
    if len(rows) < 10:
        print(f"  {name:<8} {len(rows):>6}   (too few to read)")
        return
    w1 = sum(1 for r in rows if r[0] > 0) / len(rows)
    w2 = sum(1 for r in rows if r[1] > 0) / len(rows)
    e1 = sum(r[0] for r in rows) / len(rows)
    e2 = sum(r[1] for r in rows) / len(rows)
    sp = pd.Series([r[3] / r[2] * 100 for r in rows])
    cells = []
    for f in FEES:
        fee_r = sum(2 * f * r[2] / r[3] for r in rows) / len(rows)
        cells.append(f"{e2 - fee_r:>+8.2f}" + ("*" if e2 - fee_r > 0 else " "))
    print(f"  {name:<8} {len(rows):>6} {w1*100:>6.0f}% {e1:>+8.2f}R {e1*len(rows):>+8.0f}R "
          f"{w2*100:>6.0f}% {e2:>+8.2f}R {e2*len(rows):>+8.0f}R  stop {sp.median():>5.2f}%  "
          + " ".join(cells))


print(f"\n  BREAKOUT vs RETEST — {flags} flag breakouts, {len(SYMS)} symbols, "
      f"{START:%Y-%m-%d}..{END:%Y-%m-%d}, 15m, session-capped")
print(f"  live stop model (floored at {MIN_STOP_ATR_MULT}x 1H ATR); "
      f"net@2R at fees " + ", ".join(f"{f*100:.2f}%" for f in FEES))
# NOT the tap table's "win@2R". Unresolved trades are flattened at the close for their
# realized R, so "pos%" means FINISHED POSITIVE, not "hit the target before the stop" —
# which is why the 1R and 2R columns agree for every flattened trade. The two tables are
# therefore NOT comparable on win rate. Expectancy still is.
print(f"  {'entry':<8} {'n':>6} {'pos%1R':>7} {'exp@1R':>9} {'tot@1R':>9} "
      f"{'pos%2R':>7} {'exp@2R':>9} {'tot@2R':>9}  {'stop':>10}  net@2R by fee")
print("  " + "-" * 118)
report("BREAK", brk)
report("RETEST", rtst)
print("  " + "-" * 118)
print("  BREAK = enter at the close of the bar that closes through flag_high (no pullback)")
print("  RETEST = enter when price returns to the broken edge — what ENABLE_FLAG_CONTINUATION does")
print("  No AI gate, no news, no trend filter, no slippage. ENABLE_FLAG_CONTINUATION unchanged (off).")

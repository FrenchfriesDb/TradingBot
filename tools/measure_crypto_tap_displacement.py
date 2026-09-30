"""Does the crypto DIRECT-TAP entry need a fresh-momentum requirement?

WHY (2026-09-30). binance_bot's `choch_fvg + is_fresh` branch enters on a bare tap: the
only thing that can stop it is tap_candle_opposes_bias, an explicit VETO whose own
docstring says "a `normal` or `doji` tap still passes". On 2026-09-30 ETH entered LONG at
$2,703.10 on a 9%-body doji, four 28-33% green bars into a bounce that followed an
89%-body RED bar, and stopped 13 minutes later for -1.52R.

The stock bot answered the same question with tools/measure_tap_displacement.py: ungated
taps run -0.12R, a 1.0x-ATR fresh-displacement requirement runs +0.14R. This asks whether
that transfers to crypto, where the answer is NOT obvious — crypto pays taker fees on both
sides, and this bot is already starved for taps.

Same scoring as the stock version so the two tables can be read side by side: 2R-before-1R
from the zone edge, displacement measured over the 3 bars ending at the tap. Two things
the stock version did NOT model are added because crypto makes them decisive:

  • FEES, in R. fee_R = 2 * taker_rate * entry / (entry - stop). The live ETH loss was
    -1.00R of market and -0.52R of fees.
  • the CURRENT live gate (the opposing-bar veto) as its own row, so "what we do now" is
    on the same table as "what we could do".

    python3 tools/measure_crypto_tap_displacement.py --days 60

RESULT (2026-09-30, 85,176 taps, all 13 symbols at a true 60 days):

            gate  passes  %kept   win    gross    fees   net exp      total
 none (LIVE-ish)   85176    100%   36%   +0.08R   0.36R   -0.28R   -23994R
veto only (LIVE)   58188     68%   37%   +0.11R   0.36R   -0.24R   -14071R
        0.5x ATR   23829     28%   38%   +0.13R   0.34R   -0.21R    -5098R
        1.0x ATR    9843     12%   38%   +0.14R   0.35R   -0.21R    -2042R
        1.4x ATR    4406      5%   40%   +0.19R   0.38R   -0.18R     -808R
        1.8x ATR    2039      2%   39%   +0.17R   0.40R   -0.22R     -458R
        2.5x ATR     697      1%   35%   +0.05R   0.45R   -0.40R     -282R

Every row is net negative. The gross edge is real but small (+0.08R to +0.19R) and fee
drag is 0.34-0.45R — two to four times larger. No displacement threshold closes that,
because a gate moves gross by ~0.1R at best and does nothing to fees.

So the question this tool was built to answer ("should the direct tap require fresh
momentum?") has no useful answer: the path is not tradeable at 0.25%/side either way.
Break-even is linear in the fee rate:

    veto only  <= 0.076%/side   (3.3x cheaper than now)
    1.4x ATR   <= 0.125%/side   (2.0x cheaper)

DO NOT quote the 14-day version of this table (veto only +0.09R net, +847R total). It was
a favourable window — 46% win vs 36% over 60 days — not a finding. Two weeks of crypto is
not enough to measure expectancy here.

Related dead ends on the same drag: 33f5ec5 (wider stops -- fee drag collapses and the
edge collapses with it) and e6cca6f (maker fills -- work mechanically, still do not pay).
"""
import argparse
import sys
import time

import ccxt
import pandas as pd

sys.path.insert(0, ".")
import backtest_crypto as bc
from bot import indicators
from binance_bot import (TAKER_FEE_RATE, STALE_TRADE_HOURS, DEFAULT_SYMBOLS,
                         SL_ATR_MULT, MIN_STOP_ATR_MULT_HTF, SWING_LOOKBACK)

MAXB = int(STALE_TRADE_HOURS * 3600 / 300)      # the bot's own hold cap, in 5m bars


def fetch(ex, symbol, tf, secs, days):
    """bc.fetch_paginated handles its own transient retries and stops at the present."""
    need = int(days * 24 * 3600 / secs) + 100
    since = int((pd.Timestamp.utcnow() - pd.Timedelta(seconds=need * secs)).timestamp() * 1000)
    return bc.fetch_paginated(ex, symbol, tf, secs, since, need)


def collect(ex, symbol, days):
    d = fetch(ex, symbol, "5m", 300, days)
    if d is None or len(d) < 300:
        return [], [], None
    # 1H ATR — the live stop is FLOORED against it (MIN_STOP_ATR_MULT_HTF). Without this
    # the stop collapses onto the zone height (~0.28% of price) and the modelled fee drag
    # comes out 3.4x too large. The real ETH trade on 2026-09-30 had a 0.95% stop.
    h1 = fetch(ex, symbol, "1h", 3600, days + 3)
    a1 = ((h1["high"] - h1["low"]).rolling(14).mean()).to_numpy()
    t1 = h1["ts"].to_numpy()
    # ACTUAL coverage, reported per symbol. Three separate fetch bugs in one day each
    # removed symbols from the sample silently; the fix that sticks is making the span
    # visible, not trying to guarantee it.
    span = (pd.to_datetime(d["ts"].iloc[0], unit="ms"),
            pd.to_datetime(d["ts"].iloc[-1], unit="ms"), len(d))
    out, stop_pcts = [], []
    for i in range(60, len(d) - MAXB - 1):
        w = d.iloc[:i + 1]
        px = float(w["close"].iloc[-1])
        f, lo, hi, _k = indicators.find_demand_zone(w, px, max_distance_pct=0.08)
        if not f or hi <= lo:
            continue
        atr = indicators.range_atr(w)
        if atr <= 0:
            continue
        # ── the bot's ACTUAL stop, not the zone edge ──────────────────────────────
        entry = hi
        j1 = int(t1.searchsorted(w["ts"].iloc[-1], side="right")) - 1
        atr1h = float(a1[j1]) if 0 <= j1 < len(a1) and not pd.isna(a1[j1]) else 0.0
        swing = float(w["low"].tail(SWING_LOOKBACK).min()) * 0.999
        zone_lvl = indicators.crypto_zone_stop_level(
            entry, True, lo, SL_ATR_MULT * atr, swing)
        stop = indicators.structural_stop_price(
            entry, zone_lvl, atr1h or atr, True, MIN_STOP_ATR_MULT_HTF)
        R = entry - stop
        if R <= 0:
            continue
        tgt2 = entry + 2 * R
        stop_pcts.append(R / entry * 100)
        # locate the tap
        tap_n = None
        for n in range(1, MAXB + 1):
            if i + n >= len(d):
                break
            if float(d.iloc[i + n]["low"]) <= entry:
                tap_n = i + n
                break
        if tap_n is None:
            continue
        # displacement over the 3 bars ending at the tap, in the trade direction
        best = 0.0
        for j in range(max(0, tap_n - 2), tap_n + 1):
            b = d.iloc[j]
            o, h, l, cl = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
            rng = h - l
            if rng <= 0 or cl <= o:
                continue
            body = abs(cl - o)
            if body / rng < 0.5:
                continue
            best = max(best, body / atr)
        # the CURRENT live gate: is the tap bar a decisive bar AGAINST a long?
        tapb, prevb = d.iloc[tap_n], d.iloc[max(0, tap_n - 1)]
        try:
            ctype = indicators.classify_candle(tapb, prevb)
            vetoed = indicators.tap_candle_opposes_bias(ctype, "BULLISH")
        except Exception:
            ctype, vetoed = "?", False
        # outcome from the tap, 2R before 1R, capped at the bot's own hold limit
        out2 = None
        for j in range(tap_n, min(tap_n + MAXB, len(d))):
            h, l = float(d.iloc[j]["high"]), float(d.iloc[j]["low"])
            if l <= stop:
                out2 = "LOSS"
                break
            if h >= tgt2:
                out2 = "WIN"
                break
        if out2 is None:
            continue
        fee_r = 2 * TAKER_FEE_RATE * entry / R if R > 0 else 0.0
        out.append((best, out2, fee_r, vetoed))
    return out, stop_pcts, span


def row(label, sel, total_n):
    if len(sel) < 20:
        return f"  {label:>16} {len(sel):>7}   (too few to read)"
    w = sum(1 for t in sel if t[1] == "WIN") / len(sel)
    gross = w * 2 - (1 - w)
    fees = sum(t[2] for t in sel) / len(sel)
    net = gross - fees
    return (f"  {label:>16} {len(sel):>7} {len(sel)/total_n*100:>6.0f}% "
            f"{w*100:>6.0f}% {gross:>+8.2f}R {fees:>7.2f}R {net:>+8.2f}R "
            f"{net*len(sel):>+9.0f}R")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--symbols", type=str, default=",".join(DEFAULT_SYMBOLS))
    args = ap.parse_args()
    ex = ccxt.coinbase({"enableRateLimit": True})
    taps, stops = [], []
    for s in [x.strip() for x in args.symbols.split(",") if x.strip()]:
        try:
            got, sp, span = collect(ex, s, args.days)
            taps += got
            stops += sp
            if span:
                lo, hi, nb = span
                cov = (hi - lo).total_seconds() / 86400
                flag = "" if cov >= args.days * 0.9 else "   <-- SHORT COVERAGE"
                print(f"  {s:12} {len(got):>6} taps  {nb:>6} bars  "
                      f"{lo:%m-%d} to {hi:%m-%d} ({cov:.0f}d){flag}", flush=True)
            else:
                print(f"  {s:12} {'0':>6} taps  (insufficient history)", flush=True)
        except Exception as e:
            print(f"  ! {s}: {type(e).__name__}: {e}", flush=True)
    if not taps:
        print("\n  no taps collected")
        return
    n = len(taps)
    print(f"\n  CRYPTO TAP DISPLACEMENT — {n:,} taps, {args.days}d, 5m, "
          f"hold cap {MAXB} bars ({STALE_TRADE_HOURS}h)")
    print(f"  taker fee {TAKER_FEE_RATE*100:.2f}%/side, scored 2R-before-1R")
    if stops:
        sp = pd.Series(stops)
        print(f"  stop distance: median {sp.median():.2f}% of price "
              f"(live ETH 2026-09-30 was 0.95%) — SANITY CHECK on the fee column\n")
    print(f"  {'gate':>16} {'passes':>7} {'%kept':>6} {'win':>6} {'gross':>9} "
          f"{'fees':>8} {'net exp':>9} {'total':>10}")
    print("  " + "-" * 82)
    print(row("none (LIVE-ish)", taps, n))
    print(row("veto only (LIVE)", [t for t in taps if not t[3]], n))
    print("  " + "-" * 82)
    for thr in (0.5, 0.8, 1.0, 1.4, 1.8, 2.5):
        print(row(f"{thr}x ATR", [t for t in taps if t[0] >= thr], n))
    print("\n  'veto only' is what binance_bot does TODAY on the direct-tap path: enter unless")
    print("  the tap bar is a decisively opposing candle. Rows below add a fresh-displacement")
    print("  requirement instead. 'total' is net exp x passes — the frequency-vs-quality call.")
    print("  No trend filter, no AI gate, no news. Zones via find_demand_zone, as the stock")
    print("  measurement did, so the two tables are comparable.")


if __name__ == "__main__":
    main()

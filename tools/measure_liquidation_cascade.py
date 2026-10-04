#!/usr/bin/env python3
"""Does price revert after a forced-liquidation flush?

WHY THIS HYPOTHESIS. The 6H SMC setup was measured to be a coin flip: capture was flat at
~0.30R across targets from 0.5R to 2.0R, i.e. hit rate fell in exact proportion to reward,
with no convexity anywhere (see crypto-has-no-directional-edge memo / commit b506142).
That strategy bet on CONTINUATION after structure. This one bets the other way, and on a
different kind of event:

  Forced sellers are not informed sellers. A liquidation cascade moves price for
  mechanical reasons — margin engines closing positions at any available bid — so the
  dislocation it creates has a reason to revert that "structure implies continuation"
  never had.

It is also maker-native by construction: in a flush you WANT to be the resting bid that
panic runs into. That inverts the adverse selection which makes maker-ifying the SMC bot
incoherent (you cannot confirm a displacement candle and already be resting in the book).

NO NEW VENUE PLUMBING. A real liquidation feed needs a perps venue; this proxies the
cascade from OHLCV we already paginate:
    1. violent      — adverse range >= FLUSH_ATR_MULT x ATR14
    2. forced       — volume >= VOL_SPIKE_MULT x median volume
    3. exhausted    — close reclaims >= RECLAIM_FRAC of the bar's range off the extreme

THE CONTROL ARM IS THE POINT. Last time I judged the ladder against the driftless
random-walk rate 1/(1+X), and had to caveat it twice: the hold window truncates trades
that would have reached their level later, and MFE is not a clean barrier race. A matched
control arm — same measurement machinery, same stop geometry in volatility units, same
hold window, bars chosen WITHOUT the cascade condition — shares every one of those biases,
so the DIFFERENCE between arms is clean in a way the absolute numbers are not.

Stops are set at a fixed multiple of ATR in BOTH arms deliberately. A structural stop
(below the flush low) is the realistic one, but flush bars are huge, so their R would be
far larger than a control bar's R — and R-normalised travel inside a fixed window depends
on how big R is relative to volatility. Matching R in ATR units is what makes the two
ladders comparable. The cascade arm's structural R is reported separately for realism.

PASS/FAIL, fixed before running: the cascade ladder must show CONVEXITY — capture
(hit% x X) must RISE with target distance, and must beat the control arm. Flat at ~0.30R
like the SMC bot means no edge, and the hypothesis is dead with no tuning.
"""
import argparse
import os
import statistics as st
import sys
from collections import defaultdict

import ccxt
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest_crypto import fetch_paginated          # noqa: E402  (fixed pagination)

TF, TF_SECS = os.getenv("CASCADE_TF", "5m"), 300

FLUSH_ATR_MULT = float(os.getenv("FLUSH_ATR_MULT", "2.5"))   # violent
VOL_SPIKE_MULT = float(os.getenv("VOL_SPIKE_MULT", "3.0"))   # forced
RECLAIM_FRAC   = float(os.getenv("RECLAIM_FRAC",   "0.5"))   # exhausted
STOP_ATR_MULT  = float(os.getenv("STOP_ATR_MULT",  "1.5"))   # R, in ATR units, BOTH arms
HOLD_BARS      = int(os.getenv("HOLD_BARS",        "288"))   # 24h of 5m bars
CONTROL_EVERY  = int(os.getenv("CONTROL_EVERY",    "37"))    # deterministic control sample

LEVELS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0)


def _excursion(hi, lo, entry, stop_dist, is_long):
    """Favourable / adverse excursion of one bar, in R."""
    fav = ((hi - entry) if is_long else (entry - lo)) / stop_dist
    adv = ((entry - lo) if is_long else (hi - entry)) / stop_dist
    return fav, adv


def _walk(h, l, i, entry, stop_dist, is_long, hold):
    """Walk forward from bar i+1. Returns (mfe_r, bars_to_peak, stopped).

    Pessimistic, matching backtest_crypto's convention: the bar that takes out the stop
    contributes NO favourable excursion, so a wick through the stop can never inflate MFE.
    """
    mfe, peak_at, n = 0.0, 0, len(h)
    for j in range(i + 1, min(i + 1 + hold, n)):
        fav, adv = _excursion(h[j], l[j], entry, stop_dist, is_long)
        if adv >= 1.0:                      # stopped out on this bar
            return mfe, peak_at, True
        if fav > mfe:
            mfe, peak_at = fav, j - i
    return mfe, peak_at, False


def scan(df):
    """Yield (arm, is_long, mfe_r, bars_to_peak, stopped, structural_r) per observation."""
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    v = df["volume"].to_numpy(float)
    rng = h - l

    atr = np.full(len(df), np.nan)
    if len(df) >= 15:
        kernel = np.ones(14) / 14.0
        atr[13:] = np.convolve(rng, kernel, mode="valid")
    # Median volume over the trailing day, as a "normal" reference that a spike beats.
    volmed = np.full(len(df), np.nan)
    win = 288
    for i in range(win, len(df)):
        volmed[i] = np.median(v[i - win:i])

    start = max(15, win)
    for i in range(start, len(df) - 1):
        a, vm = atr[i], volmed[i]
        if not (a > 0) or not (vm > 0) or not (rng[i] > 0):
            continue
        stop_dist = STOP_ATR_MULT * a

        # ── cascade arm ──────────────────────────────────────────────────────
        violent = rng[i] >= FLUSH_ATR_MULT * a
        forced  = v[i] >= VOL_SPIKE_MULT * vm
        if violent and forced:
            down = c[i] < o[i]
            # exhaustion: close reclaims off the extreme it was slammed into
            reclaim = ((c[i] - l[i]) / rng[i]) if down else ((h[i] - c[i]) / rng[i])
            if reclaim >= RECLAIM_FRAC:
                is_long = down                      # down-flush -> fade it upward
                entry = c[i]
                mfe, peak, stopped = _walk(h, l, i, entry, stop_dist, is_long, HOLD_BARS)
                struct = (entry - l[i]) if is_long else (h[i] - entry)
                yield ("cascade", is_long, mfe, peak, stopped, struct / a)

        # ── control arm: same geometry, no condition ─────────────────────────
        if i % CONTROL_EVERY == 0:
            for is_long in (True, False):
                mfe, peak, stopped = _walk(h, l, i, c[i], stop_dist, is_long, HOLD_BARS)
                yield ("control", is_long, mfe, peak, stopped, float("nan"))


def ladder(obs):
    n = len(obs)
    out = []
    for lvl in LEVELS:
        hit = sum(1 for o in obs if o[2] >= lvl)
        out.append((lvl, hit, hit / n if n else 0.0, lvl * (hit / n if n else 0.0)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--symbols", type=str, default="BTC/USD,ETH/USD,SOL/USD")
    args = ap.parse_args()

    ex = ccxt.coinbase({"enableRateLimit": True, "timeout": 8000})
    need = int(args.days * 24 * 3600 / TF_SECS) + 400
    since = int((__import__("time").time() - need * TF_SECS) * 1000)

    arms = defaultdict(list)
    per_symbol = {}
    for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        try:
            df = fetch_paginated(ex, sym, TF, TF_SECS, since, need)
        except Exception as e:                        # loud, never silent-short
            print(f"  {sym}: FETCH FAILED — {type(e).__name__}: {e}")
            continue
        if len(df) < 600:
            print(f"  {sym}: only {len(df)} bars — skipped")
            continue
        got = list(scan(df))
        for rec in got:
            arms[rec[0]].append(rec)
        nc = sum(1 for r in got if r[0] == "cascade")
        per_symbol[sym] = (len(df), nc)
        print(f"  {sym:9} {len(df):>6,} bars   {nc:>4} cascades")

    if not arms["cascade"]:
        print("\n  NO CASCADES DETECTED — loosen FLUSH_ATR_MULT / VOL_SPIKE_MULT.")
        return

    print("\n" + "=" * 70)
    print(f"  LIQUIDATION-CASCADE REVERSION — {args.days}d, {TF} bars")
    print(f"  flush >= {FLUSH_ATR_MULT}x ATR | vol >= {VOL_SPIKE_MULT}x median | "
          f"reclaim >= {RECLAIM_FRAC:.0%}")
    print(f"  stop = {STOP_ATR_MULT}x ATR (BOTH arms) | hold {HOLD_BARS} bars "
          f"({HOLD_BARS * TF_SECS / 3600:.0f}h)")
    print("=" * 70)

    casc, ctrl = arms["cascade"], arms["control"]
    print(f"\n  n cascade {len(casc):,}   n control {len(ctrl):,}")
    print(f"  cascade median structural stop  {st.median([r[5] for r in casc]):.2f}x ATR "
          f"(vs the {STOP_ATR_MULT}x used)")
    print(f"  cascade stopped out            {sum(1 for r in casc if r[4]) / len(casc):.0%}"
          f"   control {sum(1 for r in ctrl if r[4]) / len(ctrl):.0%}")
    _pk = [r[3] * TF_SECS / 3600 for r in casc if r[2] > 0]
    if _pk:
        print(f"  cascade median hours to peak   {st.median(_pk):.1f}h")

    lc, ll = ladder(casc), ladder(ctrl)
    print(f"\n  {'level':>7} | {'cascade hit%':>12} {'capture':>8} | "
          f"{'control hit%':>12} {'capture':>8} | {'edge':>7}")
    print("  " + "-" * 68)
    for (lvl, _, hc, capc), (_, _, hk, capk) in zip(lc, ll):
        print(f"  {lvl:>6.2f}R | {hc:>11.0%} {capc:>8.3f} | {hk:>11.0%} {capk:>8.3f} | "
              f"{capc - capk:>+7.3f}")

    caps = [c for _, _, _, c in lc]
    rising = caps[-1] > caps[0] and max(caps) > caps[0] * 1.15
    beats = all(c >= k for (_, _, _, c), (_, _, _, k) in zip(lc, ll))
    print("\n  CONVEXITY: capture " + ("RISES" if rising else "is FLAT/FALLING") +
          f" with target distance  (min {min(caps):.3f} -> max {max(caps):.3f})")
    print("  VS CONTROL: cascade " + ("beats" if beats else "does NOT beat") +
          " the control arm at every level")
    print("\n  VERDICT: " + ("WORTH BUILDING — convexity present and above control."
                             if (rising and beats) else
                             "HYPOTHESIS DEAD on this sample — no convexity above control. "
                             "Per the agreed rule: no tuning, stop here."))
    print("\n  Caveat: entry is modelled as a TAKER fill at the flush bar's close, which is\n"
          "  pessimistic for a maker thesis but isolates the SIGNAL from fill modelling.\n"
          "  Control pools a long and a short per sampled bar, so the two are\n"
          "  anti-correlated — fine for the mean, understates the variance.")


if __name__ == "__main__":
    main()

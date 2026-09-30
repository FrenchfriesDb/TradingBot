"""Sweep STALE_ZONE_BARS and score what each value actually buys.

WHY (2026-09-30). STALE_ZONE_BARS = 6 is a hardcoded constant in binance_bot.py — never
measured, never env-overridable. It does not discard a zone; past that age the zone
survives but the tap must now be backed by a FRESH displacement candle (binance_bot.py
:2717, mirrored by the sniper at :3135). So the constant is really asking:

    "how long after arming do I still trust the setup WITHOUT fresh momentum?"

6 bars is 30 minutes on 5m, while the zones themselves are derived from 6H structure.
Those two timescales were never reconciled by measurement, which is what this fixes.

    python3 tools/measure_stale_zone_bars.py --days 30

RESULT (2026-09-30, 30d, 13 symbols, full coverage after e77c1e5):

     bars  zones  tap-bars  stale  entries   win   exp R   tot R    net $   fees $
        3   3154       150    149        2   50%   -0.35    -0.7   -13.95    4.94
        6   3154       150    149        2   50%   -0.35    -0.7   -13.95    4.94  <- live
       12   3071       115    113        3   67%   -0.17    -0.5   -10.08    9.20
       24   2977        36     31        6   33%   -0.29    -1.8   -35.13   16.14
       36   2791        21     13        9   44%   -0.04    -0.4    -7.81   21.47
       48   2744        10      0       10   30%   -0.14    -1.4   -27.93   22.73

STILL UNSETTLED, and now also moot. Two to ten trades per configuration cannot decide
anything, exactly as the first (truncated) run could not — clean data raised the counts
without making them usable. And every row is net negative for the same reason the tap
table is: at 36 bars the gross is +$13.66 across 9 trades while fees are $21.47. The
constant selects among taps whose net expectancy is -0.24R; no value of it rescues that.

Scored in R (pnl / dollars-risked-at-entry), so it is comparable with the tap-threshold
sweep. Fees ARE modelled; the AI gate and news are not, and only ONE of the live bot's 11
arming paths is replayed — see backtest_crypto's own caveat. Read the shape, not the cents.
"""
import argparse
import sys

import ccxt

sys.path.insert(0, ".")
import backtest_crypto as bc


def _cache_fetches():
    """One network fetch per (symbol, timeframe) for the whole sweep.

    Keyed WITHOUT since/total: every pass asks for the same window seconds apart, so the
    drift is irrelevant, and re-fetching 13 symbols x 8 values would be ~200 paginated
    calls for identical candles.
    """
    real, cache = bc.fetch_paginated, {}

    def cached(ex, symbol, timeframe, tf_secs, since_ms, total):
        key = (symbol, timeframe)
        if key not in cache:
            cache[key] = real(ex, symbol, timeframe, tf_secs, since_ms, total)
        return cache[key]

    bc.fetch_paginated = cached


def _r(t):
    """R = pnl / dollars risked at entry. Uniform $-risk sizing makes this ~pnl/20."""
    risked = abs(t.entry - t.stop) * t.qty
    return t.pnl / risked if risked else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbols", type=str, default=",".join(bc.DEFAULT_SYMBOLS))
    ap.add_argument("--values", type=str, default="3,6,9,12,18,24,36,48")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    values = [int(v) for v in args.values.split(",")]
    _cache_fetches()
    ex = ccxt.coinbase({"enableRateLimit": True})
    baseline = bc.STALE_ZONE_BARS

    print(f"\n  STALE_ZONE_BARS sweep — {args.days}d, {len(symbols)} symbols, "
          f"live value = {baseline}")
    # 'tap-bars', not 'taps': stage 8 increments once per BAR that price sits inside an
    # armed zone, and a zone that never converts keeps accruing them. So this column falls
    # as the threshold rises — zones convert to trades sooner and stop accumulating — which
    # reads backwards if the column is labelled 'taps'.
    print(f"  {'bars':>5} {'≈time':>7} {'zones':>6} {'tap-bars':>9} {'stale':>6} "
          f"{'entries':>8} {'win':>5} {'exp R':>7} {'tot R':>8} {'net $':>9} {'fees $':>8}")
    print("  " + "-" * 78)

    rows = []
    for v in values:
        bc.STALE_ZONE_BARS = v
        bc.FUNNEL.clear()
        trades = []
        for sym in symbols:
            try:
                trades += bc.backtest_symbol(ex, sym, args.days)
            except Exception as e:
                print(f"    ! {sym}: {type(e).__name__}: {e}")
        zones = bc.FUNNEL.get("5 FVG agrees with BOS", 0)
        taps = bc.FUNNEL.get("8 price TAPS the zone", 0)
        stale = bc.FUNNEL.get("9 stale tap, needs displacement", 0)
        rs = [_r(t) for t in trades]
        n = len(rs)
        wins = sum(1 for r in rs if r > 0)
        exp = sum(rs) / n if n else 0.0
        net = sum(t.pnl for t in trades)
        fees = sum(getattr(t, "fees", 0.0) for t in trades)
        mark = "  <- live" if v == baseline else ""
        print(f"  {v:>5} {v*5:>6}m {zones:>6} {taps:>9} {stale:>6} {n:>8} "
              f"{(wins/n*100 if n else 0):>4.0f}% {exp:>+7.2f} {sum(rs):>+8.1f} "
              f"{net:>+9.2f} {fees:>8.2f}{mark}")
        rows.append((v, n, exp, sum(rs), net))

    bc.STALE_ZONE_BARS = baseline
    print("\n  Scored in R. 'stale' = taps that landed past the threshold and therefore had")
    print("  to show a fresh displacement candle. A HIGHER value trusts older zones with no")
    print("  fresh momentum; a LOWER one demands momentum sooner. Both the AI gate and 10 of")
    print("  the 11 live arming paths are absent, so treat counts as a lower bound.")
    if rows:
        best = max(rows, key=lambda r: r[3])
        print(f"\n  Most total R: STALE_ZONE_BARS={best[0]}  "
              f"({best[1]} trades, {best[2]:+.2f}R each, {best[3]:+.1f}R total)")
        if best[1] < 20:
            print(f"  ...on {best[1]} trades, which decides NOTHING. Scoring one arming path")
            print("  at trade level cannot produce a usable n here. To actually settle this,")
            print("  score every TAP directly the way tools/measure_tap_displacement.py does")
            print("  (that reached n=8289), or replay more of the 11 arming paths.")


if __name__ == "__main__":
    main()

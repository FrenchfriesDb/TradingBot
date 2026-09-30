"""Why do ~1,841 armed zones produce only ~68 bars of zone contact?

WHY (2026-09-30). The STALE_ZONE_BARS sweep showed the scarce resource is not zone
FRESHNESS but zone CONTACT: thousands of arming events, almost no taps. Tuning any
tap-side constant is pointless while that holds. This replays the same arming path as
backtest_crypto.py and instruments what happens to each armed zone instead of trading it.

    python3 tools/diagnose_zone_contact.py --days 30

Answers four questions, in the order they can kill a zone:
  1. Is "1,841 zones" really 1,841 DISTINCT zones, or one zone re-armed 1,841 times?
  2. How many arm already expired (carried_zone_age >= the 48-bar cap)?
  3. Of the rest, how FAR is price from the zone when it arms?
  4. Of the reachable ones, how many does price ever actually touch?
"""
import argparse
import sys
from collections import Counter

import time

import ccxt
import pandas as pd

sys.path.insert(0, ".")
import backtest_crypto as bc
from bot import indicators

EXPIRY_BARS = 48        # mirrors backtest_crypto.py's own cap


def diagnose(ex, symbol, days):
    now = pd.Timestamp.utcnow()
    ltf_needed = int(days * 24 * 3600 / bc.LTF_SECS) + 100
    htf_needed = int(days * 24 * 3600 / bc.HTF_SECS) + 200
    since_ltf = int((now - pd.Timedelta(seconds=ltf_needed * bc.LTF_SECS)).timestamp() * 1000)
    since_htf = int((now - pd.Timedelta(seconds=htf_needed * bc.HTF_SECS)).timestamp() * 1000)
    # bc.fetch_paginated now retries every transient ccxt.NetworkError itself and stops at
    # the present; the local RateLimitExceeded-only backoff that used to live here was
    # exactly the too-narrow catch that deleted five symbols from a 60-day run.
    ltf = bc.fetch_paginated(ex, symbol, bc.LTF_TF, bc.LTF_SECS, since_ltf, ltf_needed)
    htf = bc.fetch_paginated(ex, symbol, bc.HTF_TF, bc.HTF_SECS, since_htf, htf_needed)
    if len(ltf) < 100 or len(htf) < 60:
        return None
    # Report REAL coverage. The first version of this diagnosis ran on silently truncated
    # history and its headline numbers (1,861 armings / 32 zones / 3 touched) were wrong.
    _lo = pd.to_datetime(ltf["ts"].iloc[0], unit="ms")
    _hi = pd.to_datetime(ltf["ts"].iloc[-1], unit="ms")
    _cov = (_hi - _lo).total_seconds() / 86400
    print(f"  {symbol:12} {len(ltf):>6} ltf bars  {_lo:%m-%d} to {_hi:%m-%d} "
          f"({_cov:.0f}d){'' if _cov >= days * 0.9 else '   <-- SHORT COVERAGE'}", flush=True)

    st = Counter()
    distinct = set()
    gaps = []            # distance price->zone at arming, in % of price
    gaps_atr = []        # ...and in LTF ATRs
    zone = None
    last_zone = (None, None, 0)
    bars_wait = 0
    touched_this_zone = False
    alive_zone_bars = Counter()

    for i in range(len(ltf)):
        bar = ltf.iloc[i]
        price = float(bar["close"])
        htf_upto = htf[htf["ts"] <= bar["ts"]]
        if len(htf_upto) < 40:
            continue
        htf_closed = indicators.drop_forming_candle(htf_upto)
        ltf_upto = ltf.iloc[max(0, i - 200):i + 1]

        if zone is None:
            is_bos, direction, _ = indicators.detect_displacement_bos(htf_closed, lookback=15)
            if not (is_bos and direction):
                continue
            found, d, z_lo, z_hi, _ = indicators.detect_displacement_fvg(htf_closed)
            if not (found and d == direction):
                continue
            st["armings"] += 1
            distinct.add((round(z_lo, 8), round(z_hi, 8)))
            bars_wait = indicators.carried_zone_age(z_lo, z_hi, *last_zone)
            if bars_wait >= EXPIRY_BARS:
                st["DEAD ON ARRIVAL (carried age >= 48)"] += 1
            else:
                st["arms alive"] += 1
                # how far must price travel to reach this zone, right now?
                is_long = d == "bullish"
                edge = z_hi if is_long else z_lo      # the near edge price must reach
                gap = (price - edge) if is_long else (edge - price)
                gap = max(0.0, gap)
                rng = ltf_upto["high"] - ltf_upto["low"]
                atr = float(rng.rolling(14).mean().iloc[-1])
                gaps.append(gap / price * 100 if price else 0.0)
                if atr and not pd.isna(atr):
                    gaps_atr.append(gap / atr)
            zone = (z_lo, z_hi, "BULLISH" if d == "bullish" else "BEARISH")
            touched_this_zone = False
            continue

        z_lo, z_hi, bias = zone
        bars_wait += 1
        is_long = bias == "BULLISH"
        if bars_wait > EXPIRY_BARS:
            st["expired"] += 1
            if touched_this_zone:
                st["...and HAD been touched"] += 1
            last_zone = (z_lo, z_hi, bars_wait); zone = None; continue
        alive_zone_bars["bars a live zone waited"] += 1
        if indicators.price_in_entry_zone(price, z_lo, z_hi, is_long):
            st["tap-bars"] += 1
            if not touched_this_zone:
                st["zones EVER touched"] += 1
                touched_this_zone = True

    st["distinct zone levels"] = len(distinct)
    st.update(alive_zone_bars)
    return st, gaps, gaps_atr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbols", type=str, default=",".join(bc.DEFAULT_SYMBOLS))
    args = ap.parse_args()
    ex = ccxt.coinbase({"enableRateLimit": True})
    total = Counter()
    all_gaps, all_gaps_atr = [], []
    for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        try:
            r = diagnose(ex, sym, args.days)
        except Exception as e:
            print(f"  ! {sym}: {type(e).__name__}: {e}")
            continue
        if r is None:
            continue
        st, gaps, gaps_atr = r
        total.update(st)
        all_gaps += gaps
        all_gaps_atr += gaps_atr

    print(f"\n  ZONE CONTACT DIAGNOSIS — {args.days}d, "
          f"{len(args.symbols.split(','))} symbols")
    print("  " + "=" * 58)
    order = ["armings", "distinct zone levels", "DEAD ON ARRIVAL (carried age >= 48)",
             "arms alive", "bars a live zone waited", "tap-bars", "zones EVER touched",
             "expired", "...and HAD been touched"]
    for k in order:
        print(f"  {k:<38} {total.get(k, 0):>10,}")

    if total.get("armings"):
        reuse = total["armings"] / max(1, total["distinct zone levels"])
        print(f"\n  Each distinct zone is re-armed {reuse:,.0f}x on average.")
    if all_gaps:
        s = pd.Series(all_gaps)
        a = pd.Series(all_gaps_atr)
        print(f"\n  DISTANCE from price to the zone's near edge, at arming "
              f"(n={len(s):,}, live arms only)")
        print(f"    {'pctile':>8} {'% of price':>12} {'LTF ATRs':>10}")
        for q in (0.10, 0.25, 0.50, 0.75, 0.90):
            print(f"    {q*100:>7.0f}% {s.quantile(q):>11.2f}% "
                  f"{(a.quantile(q) if len(a) else float('nan')):>10.1f}")
        print(f"    {'already in':>8} {(s <= 0).mean()*100:>11.1f}% of arms had price "
              f"ALREADY at/inside the zone")


if __name__ == "__main__":
    main()

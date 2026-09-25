#!/usr/bin/env python3
"""Score the setups the retest rule refused, across several candidate fills.

    python3 tools/score_shadow_setups.py [--bars 30]

Reads the shadow ledger the bots append to, replays candles forward from each refusal,
and reports what each candidate entry WOULD have returned. Nothing here trades; the live
rule stays strict until this has a sample worth acting on.

READ THE SAMPLE SIZE FIRST. The decision this feeds — whether a no-retest setup is worth
taking, and on what fill — was previously answered on n=5, which is tarot, not data. Do
not act on this until n is in the dozens and one candidate separates clearly.

WHY SEVERAL FILLS. A no-retest setup does not have to be taken at market, and market was
the worst case measured (15% on setups that later retested). part25/part50 are shallow
pullbacks that a dip can fill without price ever reaching the zone.
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.shadow_ledger import load


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=30, help="forward bars to resolve over")
    ap.add_argument("--tf", default="5m")
    a = ap.parse_args()

    rows = load()
    print(f"shadow setups recorded: {len(rows)}")
    if not rows:
        print("  nothing yet — the bots append here every time the retest rule refuses.")
        return
    by_sym = {}
    for r in rows:
        by_sym.setdefault(r.get("symbol"), []).append(r)
    print(f"  symbols: {len(by_sym)}")
    for s, v in sorted(by_sym.items(), key=lambda x: -len(x[1]))[:10]:
        print(f"    {s:12} {len(v)}")

    if len(rows) < 20:
        print(f"\n  n={len(rows)} is too small to conclude anything. The previous attempt "
              f"at this question ran on n=5.\n  Let it collect.")
        return

    import ccxt, pandas as pd, datetime as dt
    ex = ccxt.coinbase({"enableRateLimit": True, "timeout": 20000})
    tally = {}
    for r in rows:
        ents = r.get("entries") or {}
        stop = r.get("stop")
        if not ents or not stop:
            continue
        try:
            t0 = dt.datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00"))
            o = ex.fetch_ohlcv(r["symbol"], a.tf,
                               since=int(t0.timestamp() * 1000), limit=a.bars + 1)
        except Exception:
            continue
        if not o:
            continue
        is_long = str(r.get("side", "")).upper() == "LONG"
        for name, entry in ents.items():
            R = abs(entry - stop)
            if R <= 0:
                continue
            tp = entry + 2 * R if is_long else entry - 2 * R
            filled = False
            res = None
            for b in o:
                hi, lo = float(b[2]), float(b[3])
                if not filled:
                    filled = (lo <= entry) if is_long else (hi >= entry)
                    if not filled:
                        continue
                if is_long:
                    if lo <= stop: res = "LOSS"; break
                    if hi >= tp:   res = "WIN";  break
                else:
                    if hi >= stop: res = "LOSS"; break
                    if lo <= tp:   res = "WIN";  break
            d = tally.setdefault(name, {"WIN": 0, "LOSS": 0, "nofill": 0, "open": 0})
            if not filled:   d["nofill"] += 1
            elif res is None: d["open"] += 1
            else:             d[res] += 1

    print(f"\n{'fill':8} {'filled':>7} {'win':>6} {'loss':>6} {'never filled':>13} {'win rate':>9}")
    for name in ("market", "part25", "part50"):
        d = tally.get(name)
        if not d: continue
        dec = d["WIN"] + d["LOSS"]
        wr = f"{d['WIN']/dec*100:.0f}%" if dec else "n/a"
        print(f"{name:8} {dec:7d} {d['WIN']:6d} {d['LOSS']:6d} {d['nofill']:13d} {wr:>9}")
    print("\n  A candidate only earns a rule change if it beats the live retest path's "
          "61% AND fills often enough to matter.")


if __name__ == "__main__":
    main()

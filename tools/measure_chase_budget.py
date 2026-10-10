"""Is the chase guard too tight, or is the LOOP too slow? Measures both at once.

WHY (2026-10-02). "Tap not actionable — the move left without us" was the single biggest
refusal on the stock bot: 12 of 30 in one session, more than any other gate, and
TAP_MAX_CHASE_ATR = 0.5 had never been measured.

The guard bounds how far price may have rebounded ABOVE a demand zone by the time the bot
looks (tap_chase_ok). So there are TWO ways to stop losing those setups and they are not
equivalent:

    LOOSEN THE BUDGET   fill further from the zone. Buys frequency with R:R — the stop is
                        anchored near the zone, so a further entry means a wider stop and
                        a worse trade.
    SPEED UP THE LOOP   look sooner after the tap, while price is still near the edge.
                        Costs nothing in R:R. The bot runs every 15 min on 15m bars.

Sweeping the constant alone cannot tell them apart, so this reconstructs the true tap
moment from 1-MINUTE bars and then asks what the bot would have SEEN under each cadence.

    python3 tools/measure_chase_budget.py --days 45

Entry at the observed price, stop via the live structural_stop_price (1.5x 1H ATR floor),
scored to the live 1R target, session-capped. Nothing here changes live behaviour.

RESULT (2026-10-02, 1,466 zone taps, 45d, 12 symbols, 1m reconstruction):

   cadence  median chase   <=0.25x      <=0.5x      <=1.0x      <=1.5x        any
       15m        0.31x   584 +0.10R  910 +0.05R 1176 +0.05R 1253 +0.03R 1336 +0.02R
        5m        0.00x  1086 +0.19R 1239 +0.19R 1329 +0.19R 1370 +0.19R 1398 +0.19R
        1m        0.00x  1277 +0.21R 1334 +0.22R 1358 +0.23R 1369 +0.22R 1375 +0.22R

CADENCE WINS, AND IT IS NOT CLOSE. Down the live budget column (<=0.5x), 15m -> 5m buys
+36% MORE trades at nearly 4x the expectancy (+0.05R -> +0.19R). That is free: the fill is
closer to the zone, so the stop is tighter and R:R improves rather than degrades.

LOOSENING THE BUDGET DOES THE OPPOSITE. Across the 15m row, going 0.25x -> unbounded adds
trades and destroys edge (+0.10R -> +0.02R) exactly as the asymmetry predicts — a further
fill means a wider stop on the same structure. Note 0.25x BEATS the live 0.5x on both
expectancy and total R (584 x 0.10 = 58R vs 910 x 0.05 = 45R), so if anything the budget
should TIGHTEN, never loosen.

The median chase tells the whole story: 0.31x ATR at 15m, 0.00x at 5m. On a 5-minute look
price is still AT the zone edge. The 15-minute loop is not refusing bad setups — it is
arriving after good ones have left, and then the guard correctly refuses the stale fill.

So "the move left without us" was never a gate-tuning problem.
"""
import argparse
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
from bot.strategy import MIN_STOP_ATR_MULT, TAP_MAX_CHASE_ATR

SYMS = ["AAPL", "QQQ", "SPY", "NVDA", "TSLA", "GOOGL", "META", "MSFT",
        "AMZN", "AMD", "PLTR", "NFLX"]
CADENCES = [15, 5, 1]                         # minutes between bot observations
BUDGETS = [0.25, 0.5, 1.0, 1.5, 2.0, 99.0]    # x ATR; 99 = effectively unbounded
MAXB = 26 * 15                                # session cap, in MINUTES


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    args = ap.parse_args()
    end = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    start = end - dt.timedelta(days=args.days)
    cli = StockHistoricalDataClient(API_KEY, API_SECRET)

    def grab(tf, unit):
        return cli.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=SYMS, feed=DataFeed.IEX,
            timeframe=TimeFrame(tf, unit), start=start, end=end)).df

    b15, b1, h1 = grab(15, TimeFrameUnit.Minute), grab(1, TimeFrameUnit.Minute), grab(1, TimeFrameUnit.Hour)

    # taps[(cadence)] -> list of (chase_in_atr, realized_R_at_1R_target)
    taps = defaultdict(list)
    n_zones = 0
    for s in SYMS:
        try:
            d, m, hd = b15.loc[s].reset_index(), b1.loc[s].reset_index(), h1.loc[s].reset_index()
        except KeyError:
            continue
        d["day"] = pd.to_datetime(d["timestamp"]).dt.date
        # Everything UTC-NAIVE before any searchsorted. Mixing tz-aware and tz-naive
        # raises "Cannot compare tz-naive and tz-aware timestamps" — the same bug hit in
        # measure_news_direction the same day, so it is centralised here as _naive().
        def _naive(col):
            return pd.to_datetime(col, utc=True).dt.tz_localize(None)
        m["t"] = _naive(m["timestamp"])
        m["day"] = m["t"].dt.date
        hd["atr"] = (hd["high"] - hd["low"]).rolling(14).mean()
        h_ts = _naive(hd["timestamp"]).to_numpy()
        h_atr = hd["atr"].to_numpy()
        mt = m["t"].to_numpy()
        m_lo, m_hi, m_cl = m["low"].to_numpy(), m["high"].to_numpy(), m["close"].to_numpy()

        last_zone = None
        for i in range(60, len(d) - 30):
            w = d.iloc[:i + 1]
            f, zlo, zhi, _k = find_demand_zone(w, float(w["close"].iloc[-1]), max_distance_pct=0.08)
            if not f or zhi <= zlo:
                continue
            if last_zone and abs(zhi - last_zone) / zhi < 0.002:
                continue
            atr15 = range_atr(w)
            if atr15 <= 0:
                continue
            t0 = pd.Timestamp(w["timestamp"].iloc[-1])
            t0 = t0.tz_convert("UTC").tz_localize(None) if t0.tzinfo else t0
            j0 = int(mt.searchsorted(t0.to_datetime64(), side="right"))
            # TRUE tap moment: first 1m bar whose low enters the zone
            tap = None
            for j in range(j0, min(j0 + MAXB, len(mt))):
                if m["day"].iloc[j] != m["day"].iloc[j0] if j0 < len(m) else True:
                    break
                if m_lo[j] <= zhi:
                    tap = j
                    break
            if tap is None:
                continue
            n_zones += 1
            last_zone = zhi
            k = int(h_ts.searchsorted(mt[tap], side="right")) - 1
            atr1h = float(h_atr[k]) if 0 <= k < len(h_atr) and h_atr[k] == h_atr[k] else 0.0
            if atr1h <= 0:
                continue
            day = m["day"].iloc[tap]

            for cad in CADENCES:
                # the bot next LOOKS at the first wall-clock multiple of `cad` after the tap
                obs = tap
                while obs < len(mt) and (pd.Timestamp(mt[obs]).minute % cad) != 0:
                    obs += 1
                if obs >= len(mt) or m["day"].iloc[obs] != day:
                    continue
                px = float(m_cl[obs])
                if px < zlo:                       # zone failed — refused at any budget
                    continue
                chase_atr = max(0.0, px - zhi) / atr15
                stop = structural_stop_price(px, zlo, atr1h, True, MIN_STOP_ATR_MULT, None)
                R = px - stop
                if R <= 0:
                    continue
                tgt = px + R                       # live target is 1R
                out = None
                for j in range(obs + 1, min(obs + 1 + MAXB, len(mt))):
                    if m["day"].iloc[j] != day:
                        break
                    if m_lo[j] <= stop:
                        out = -1.0
                        break
                    if m_hi[j] >= tgt:
                        out = 1.0
                        break
                if out is None:
                    out = (float(m_cl[min(obs + MAXB, len(mt) - 1)]) - px) / R
                taps[cad].append((chase_atr, out))
        print(f"  {s:6} done", flush=True)

    print(f"\n  CHASE BUDGET vs LOOP CADENCE — {n_zones} zone taps, {args.days}d, "
          f"{len(SYMS)} symbols, 1m reconstruction")
    print(f"  live: every {CADENCES[0]}m, TAP_MAX_CHASE_ATR={TAP_MAX_CHASE_ATR}; "
          f"entry at the observed price, stop floored at {MIN_STOP_ATR_MULT}x 1H ATR, 1R target\n")
    print(f"  {'cadence':>8} {'median chase':>13} " +
          " ".join(f"{'<=' + str(b) + 'x':>16}" for b in BUDGETS[:-1]) + f"{'any':>16}")
    print("  " + "-" * (24 + 16 * len(BUDGETS)))
    for cad in CADENCES:
        rows = taps[cad]
        if not rows:
            continue
        med = pd.Series([r[0] for r in rows]).median()
        cells = []
        for b in BUDGETS:
            sel = [r for r in rows if r[0] <= b]
            if len(sel) < 10:
                cells.append(f"{len(sel):>6} (thin) ")
                continue
            exp = sum(r[1] for r in sel) / len(sel)
            cells.append(f"{len(sel):>5} {exp:>+6.2f}R" + ("*" if exp > 0 else " "))
        print(f"  {str(cad)+'m':>8} {med:>12.2f}x " + " ".join(f"{c:>16}" for c in cells))
    print("  " + "-" * (24 + 16 * len(BUDGETS)))
    print("  Each cell: trades that pass that budget, and their expectancy at a 1R target.")
    print("  Read DOWN a column for the cadence effect at a fixed budget (free in R:R).")
    print("  Read ACROSS a row for the budget effect at a fixed cadence (buys frequency")
    print("  with entry quality — a further fill means a wider stop).")


if __name__ == "__main__":
    main()

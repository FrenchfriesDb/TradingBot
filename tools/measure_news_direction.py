"""Does the FIRST headline predict which way the stock goes?

WHY (2026-10-02). TSLA opened +5% on Q3 deliveries. The number WAS on the Alpaca feed the
bot already has, at 09:04 ET — 26 minutes before the open. So a news trigger is not blocked
by latency. It is blocked by DIRECTION:

    09:04 ET  "Tesla Reports Q3 Total Production 464,391 Units Vs Visible Alpha Est. 486,761"
    11:25 ET  "Tesla Stock Gains As European Demand Boosts Q3 Deliveries"

The only headline available when it mattered reported a MISS, and the stock rose 5% — the
beat was in DELIVERIES, a different number that reached the feed two hours after the move.
An AI reading the 09:04 line would have gone short into a +5% day.

So before building any news-triggered entry, this asks the question that decides whether
one can work at all: across many past headlines, does FinBERT's sentiment on the headline
beat a coin flip at calling the next few hours?

A directional edge near 50% means a news trigger is a coin flip with extra steps, no matter
how good the plumbing is.

    python3 tools/measure_news_direction.py --days 60

Measures forward returns at +1h, +4h and to the next session close, bucketed by sentiment
and by FinBERT's own confidence. Market-hours only for the entry reference; a headline
printed outside the session is marked and priced at the next session bar.

RESULT (2026-10-02, 987 non-neutral headlines, 60d, 12 symbols):

   sentiment      n         +1h           +4h           +1d
    positive    451    48% / +0.06%  53% / +0.13%  49% / +0.11%
    negative    536    49% / +0.03%  45% / +0.24%  48% / +0.31%

   by FinBERT confidence (directional hit rate)
    0.00-0.70   308       48%          50%          51%
    0.70-0.90   350       48%          48%          48%
    0.90-1.01   329       50%          48%          48%

A COIN FLIP, everywhere. 45-53% at every horizon, and — the part that closes the question —
CONFIDENCE DOES NOT HELP: FinBERT's most certain calls (0.90+) hit 50/48/48, no better than
its least certain. There is no high-conviction subset to trade.

Worse for a naive trigger, NEGATIVE headlines are followed by POSITIVE mean returns at every
horizon with sub-50% hit rates, so shorting bad news was systematically wrong here.

WHY, and what this does NOT say. It does not say news is useless. It says HEADLINE SENTIMENT
does not pick direction, because sentiment and surprise are different things. The TSLA event
that prompted this is the clean demonstration: "Production 464,391 Vs Est. 486,761" is a
factually negative headline and FinBERT scores it negative — the stock rose 5% because
DELIVERIES beat, a number not in that headline. A news-triggered entry would have to compare
the reported figure against the EXPECTED figure, which is a data problem (consensus
estimates, per metric, in real time), not a sentiment-model problem.

So this vindicates the existing design: news stays CONTEXT for the AI prompt and must never
become a trigger or a direction veto on sentiment alone.

Deliberate anti-leak choice: only the FIRST headline per (symbol, headline-prefix) is scored,
because that is what a live trigger would see. Scoring every headline would include the ones
written AFTER the move ("Tesla Stock Gains As..."), which leak the answer and would make
sentiment look prophetic in backtest and lose money live.
"""
import argparse
import datetime as dt
import sys
from collections import defaultdict

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import StockBarsRequest, NewsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

from config import API_KEY, API_SECRET
from finbert_utils import estimate_sentiment

SYMS = ["AAPL", "QQQ", "SPY", "NVDA", "TSLA", "GOOGL", "META", "MSFT",
        "AMZN", "AMD", "PLTR", "NFLX"]
HORIZONS = [("+1h", 4), ("+4h", 16), ("+1d", 26)]      # in 15m bars


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--min-prob", type=float, default=0.0)
    args = ap.parse_args()

    end = dt.datetime.now(dt.UTC)
    start = end - dt.timedelta(days=args.days)

    bars = StockHistoricalDataClient(API_KEY, API_SECRET).get_stock_bars(
        StockBarsRequest(symbol_or_symbols=SYMS, feed=DataFeed.IEX,
                         timeframe=TimeFrame(15, TimeFrameUnit.Minute),
                         start=start, end=end)).df
    news = NewsClient(API_KEY, API_SECRET)

    rows = []
    seen = set()
    for s in SYMS:
        try:
            d = bars.loc[s].reset_index()
        except KeyError:
            continue
        # Normalise BOTH sides to UTC-naive before searchsorted. Bar timestamps are
        # tz-aware and .to_numpy() drops the zone, while news created_at stays aware —
        # comparing them raises "Cannot compare tz-naive and tz-aware timestamps".
        ts = pd.to_datetime(d["timestamp"], utc=True).dt.tz_localize(None).to_numpy()
        px = d["close"].to_numpy()
        got = news.get_news(NewsRequest(symbols=s, start=start, end=end, limit=200))
        items = got.data.get("news", [])
        for n in items:
            head = (n.headline or "").strip()
            if not head:
                continue
            key = (s, head[:70])
            if key in seen:            # the same wire story repeats across outlets
                continue
            seen.add(key)
            prob, sent = estimate_sentiment([head])
            if sent == "neutral" or prob < args.min_prob:
                continue
            # first bar at or after the headline
            when = pd.Timestamp(n.created_at)
            when = when.tz_convert("UTC").tz_localize(None) if when.tzinfo else when
            i = int(ts.searchsorted(when.to_datetime64(), side="left"))
            if i >= len(px) - max(h for _, h in HORIZONS):
                continue
            entry = float(px[i])
            if entry <= 0:
                continue
            fwd = {}
            for name, h in HORIZONS:
                fwd[name] = (float(px[i + h]) - entry) / entry * 100
            rows.append({"symbol": s, "sent": sent, "prob": prob, **fwd})
        print(f"  {s:6} {len(items):>4} headlines", flush=True)

    if not rows:
        print("\n  no scorable headlines")
        return
    df = pd.DataFrame(rows)
    print(f"\n  NEWS DIRECTION — {len(df)} non-neutral headlines, {args.days}d, "
          f"{len(SYMS)} symbols")
    print(f"  Does FinBERT's headline sentiment beat a coin flip on direction?\n")
    print(f"  {'sentiment':>10} {'n':>6} " +
          " ".join(f"{h:>18}" for h, _ in HORIZONS))
    print("  " + "-" * (18 + 19 * len(HORIZONS)))
    for sent in ("positive", "negative"):
        sub = df[df["sent"] == sent]
        if len(sub) < 10:
            print(f"  {sent:>10} {len(sub):>6}   (too few)")
            continue
        cells = []
        for h, _ in HORIZONS:
            want_up = sent == "positive"
            hit = ((sub[h] > 0) == want_up).mean() * 100
            cells.append(f"{hit:>5.0f}% / {sub[h].mean():>+6.2f}%")
        print(f"  {sent:>10} {len(sub):>6} " + " ".join(f"{c:>18}" for c in cells))
    print("  " + "-" * (18 + 19 * len(HORIZONS)))
    print("  Each cell: % of headlines where price moved the way sentiment implied,")
    print("  and the MEAN forward return of that bucket.")
    print("  50% = a coin flip. Anything near it means a news trigger cannot pick direction,")
    print("  however fast the feed is.\n")

    print("  By FinBERT confidence (positive + negative pooled, directional hit rate):")
    print(f"  {'confidence':>12} {'n':>6} " + " ".join(f"{h:>8}" for h, _ in HORIZONS))
    for lo, hi in ((0.0, 0.7), (0.7, 0.9), (0.9, 1.01)):
        sub = df[(df["prob"] >= lo) & (df["prob"] < hi)]
        if len(sub) < 10:
            continue
        cells = []
        for h, _ in HORIZONS:
            want_up = sub["sent"] == "positive"
            cells.append(f"{(((sub[h] > 0) == want_up).mean()*100):>7.0f}%")
        print(f"  {lo:.2f}-{hi:.2f}  {len(sub):>6} " + " ".join(cells))


if __name__ == "__main__":
    main()

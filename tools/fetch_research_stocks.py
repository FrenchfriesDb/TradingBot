"""2 years of 15m stock bars from Alpaca, IEX feed (the free plan's). Needs ALPACA_API_KEY/SECRET in .env.

    python3 tools/fetch_research_stocks.py     # -> /stocks_15m

IEX is ONE venue, ~2-3% of consolidated volume: highs/lows are narrower than the real tape
and quiet intervals can have no bar. Coverage is measured and thin symbols are excluded
rather than allowed to contribute less.
"""
import os, sys
from datetime import datetime, timedelta, timezone
import pandas as pd
from dotenv import load_dotenv
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed, Adjustment
CACHE = os.environ.get("RESEARCH_CACHE", os.path.expanduser("~/Library/Caches/debbiela-research"))
OUT = os.path.join(CACHE, "stocks_15m"); os.makedirs(OUT, exist_ok=True)
SYMS = ["SPY","QQQ","AAPL","NVDA","TSLA","GOOGL","META","MSFT","AMD","PLTR","NFLX","AMZN"]
c = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_API_SECRET"))
end = datetime.now(timezone.utc) - timedelta(days=1); start = end - timedelta(days=730)
for i, s in enumerate(SYMS, 1):
    path = os.path.join(OUT, s + ".csv")
    if os.path.exists(path): df = pd.read_csv(path)
    else:
        try:
            # 120-day chunks. One 730-day request hung for 6+ minutes with no output while a
            # 120-day request returns in ~1s, and a silent stall is indistinguishable from
            # slow. Chunking makes progress visible and bounds any single request.
            parts, cs = [], start
            while cs < end:
                ce = min(cs + timedelta(days=120), end)
                r = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=s, timeframe=TimeFrame(15, TimeFrameUnit.Minute),
                                                      start=cs, end=ce, feed=DataFeed.IEX, adjustment=Adjustment.SPLIT))
                if not r.df.empty: parts.append(r.df)
                print(f"      {s} {cs:%Y-%m-%d}..{ce:%Y-%m-%d}  +{len(r.df)} bars", flush=True)
                cs = ce
            if not parts: raise RuntimeError("no bars")
            d = pd.concat(parts)
            d = d.reset_index(level=0, drop=True).reset_index()
            d["ts"] = pd.to_datetime(d["timestamp"], utc=True).astype("int64") // 10**6
            df = d[["ts", "open", "high", "low", "close", "volume"]].sort_values("ts").drop_duplicates("ts")
            df.to_csv(path, index=False)
        except Exception as e:
            print(f"[{i:2d}/{len(SYMS)}] {s:6s} FAILED {type(e).__name__}: {str(e)[:80]}", flush=True); continue
    print(f"[{i:2d}/{len(SYMS)}] {s:6s} {len(df):6d} bars", flush=True)
print("FETCH DONE", flush=True)

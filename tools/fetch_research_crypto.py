"""Fetch + cache Coinbase candles for the bot's DEFAULT_SYMBOLS. Public data, no key.

    python3 tools/fetch_research_crypto.py            # 1h x 730 days  -> /crypto_1h   (resampled to 4h/1d later)
    TF=15m python3 tools/fetch_research_crypto.py     # 15m x 120 days -> /crypto_15m
Coinbase has no 4h bars, hence 1h then resample. Finished symbols are cached, so a rerun only
fetches what is missing (one symbol failed with a network error on the first pass).

Differs from the 15m fetcher in ONE important way: an EMPTY page advances the cursor instead
of ending the walk. A coin listed 200 days into the window returns empty pages until its
first candle, and 'empty means done' would silently hand back zero bars for it — the same
silent-partial-history failure backtest_crypto.fetch_paginated has produced twice.
"""
import ast, os, re, time
import ccxt, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.environ.get("RESEARCH_CACHE", os.path.expanduser("~/Library/Caches/debbiela-research"))
TF = os.environ.get("TF", "1h")
TF_S = {"15m": 900, "1h": 3600}[TF]
DAYS = int(os.environ.get("DAYS", "120" if TF == "15m" else "730"))
OUT = os.path.join(CACHE, f"crypto_{TF}"); os.makedirs(OUT, exist_ok=True)
src = open(os.path.join(ROOT, "binance_bot.py")).read()
SYMS = ast.literal_eval(re.sub(r"#[^\n]*", "", re.search(r"^DEFAULT_SYMBOLS\s*=\s*(\[.*?\n\])", src, re.S | re.M).group(1)))
ex = ccxt.coinbase({"enableRateLimit": True})
now_ms = int(time.time() * 1000); since0 = now_ms - DAYS * 86400 * 1000; WIN = 300 * TF_S * 1000

def fetch(sym):
    out, cur = [], since0
    while cur < now_ms:
        batch = None
        for a in range(5):
            try: batch = ex.fetch_ohlcv(sym, TF, since=cur, limit=300); break
            except ccxt.NetworkError:
                if a == 4: raise
                time.sleep(2 ** a)
        if batch:
            out.extend(batch); nxt = batch[-1][0] + TF_S * 1000
            cur = nxt if nxt > cur else cur + WIN
        else:
            cur += WIN                      # empty page: not listed yet, or a gap — keep walking
    df = pd.DataFrame(out, columns=["ts", "open", "high", "low", "close", "volume"])
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)

rows = []
for i, s in enumerate(SYMS, 1):
    path = os.path.join(OUT, s.replace("/", "_") + ".csv")
    if os.path.exists(path): df = pd.read_csv(path)
    else:
        try: df = fetch(s); df.to_csv(path, index=False)
        except Exception as e:
            print(f"[{i:2d}/{len(SYMS)}] {s:10s} FAILED {type(e).__name__}", flush=True); continue
    days = (df["ts"].iloc[-1] - df["ts"].iloc[0]) / 86_400_000 if len(df) > 1 else 0
    gaps = 1 - len(df) / max(1, (df["ts"].iloc[-1] - df["ts"].iloc[0]) / (TF_S * 1000) + 1) if len(df) > 1 else 1
    print(f"[{i:2d}/{len(SYMS)}] {s:10s} {len(df):6d} bars  {days:5.0f} days of history  missing {gaps:5.1%} of bars inside it", flush=True)
print("FETCH DONE", flush=True)

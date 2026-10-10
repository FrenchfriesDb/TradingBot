"""How far does price actually travel toward the target before the 6-hour timer fires?

The backtest showed every simulated trade exiting on the clock rather than on price, so
optimising an exit policy (scale-outs, TP1) would be tuning the wrong end of the trade.
This asks the same question of REAL closed trades instead of simulated ones: pair each
open with its close from the bot's own log, then replay candles over the hold to get the
maximum favourable excursion as a fraction of the entry-to-target distance.

READ THE SAMPLE SIZE BEFORE THE NUMBER. Coinbase serves ~6 days of 5m history, so older
trades fall back to 1h bars, where an intrabar spike toward the target can hide inside an
hourly candle — MFE is UNDERSTATED for those, which biases the result AGAINST a scale-out
rather than for it.

    python3 tools/measure_travel_to_target.py
"""
import re, sys, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

LOG = "/Users/usahealthlife/Library/Logs/debbiela/binance_bot.log"
HDR   = re.compile(r"^\s*(\d{4}-\d\d-\d\d \d\d:\d\d) UTC\s*$")
OPEN  = re.compile(r"\[ALERT\] firing: .*?(?:SNIPER|TRADE OPENED).*?—?\s*(LONG|SHORT)\s+(\w+)[^|]*\|\s*@?\s*\$([\d,.]+)\s+SL \$([\d,.]+)\s+TP \$([\d,.]+)")
CLOSE = re.compile(r"\[ALERT\] firing: (⏰ STALE|🔴 SL|🟢 TP|🛡 BREAK-EVEN)[^—]*—\s*(\w+)")

def f(x): return float(x.replace(",", ""))

ts, opens, trades = None, {}, []
for line in open(LOG, encoding="utf-8", errors="replace"):
    m = HDR.match(line)
    if m:
        ts = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
        continue
    m = OPEN.search(line)
    if m and ts:
        side, sym, e, sl, tp = m.group(1), m.group(2), f(m.group(3)), f(m.group(4)), f(m.group(5))
        opens[sym] = dict(sym=sym, side=side, entry=e, sl=sl, tp=tp, t_in=ts)
        continue
    m = CLOSE.search(line)
    if m and ts:
        kind, sym = m.group(1), m.group(2)
        if sym in opens:
            t = opens.pop(sym); t["kind"] = kind; t["t_out"] = ts
            trades.append(t)

print(f"paired trades: {len(trades)}")
by = {}
for t in trades: by.setdefault(t["kind"], []).append(t)
for k, v in sorted(by.items(), key=lambda x: -len(x[1])):
    print(f"   {k:14} {len(v)}")

# MFE for the stale ones, where 5m history still reaches
import ccxt
ex = ccxt.coinbase({"enableRateLimit": True, "timeout": 20000})
now = dt.datetime.now(dt.timezone.utc)
rows = []
for t in trades:
    if "STALE" not in t["kind"]: continue
    # 5m history only reaches ~6 days on Coinbase; fall back to 1h for older trades.
    # Coarser, so MFE is UNDERSTATED for them (an intrabar spike toward TP can hide
    # inside an hourly bar) — which biases the result against the scale-out, not for it.
    tf = "5m" if (now - t["t_in"]).days <= 5 else "1h"
    try:
        o = ex.fetch_ohlcv(f'{t["sym"]}/USD', tf,
                           since=int(t["t_in"].timestamp()*1000), limit=100)
    except Exception:
        continue
    bars = [b for b in o if t["t_in"].timestamp()*1000 <= b[0] <= t["t_out"].timestamp()*1000]
    if not bars: continue
    long = t["side"] == "LONG"
    mfe = (max(b[2] for b in bars) - t["entry"]) if long else (t["entry"] - min(b[3] for b in bars))
    mae = (t["entry"] - min(b[3] for b in bars)) if long else (max(b[2] for b in bars) - t["entry"])
    dist_tp = abs(t["tp"] - t["entry"]); dist_sl = abs(t["entry"] - t["sl"])
    if dist_tp <= 0: continue
    rows.append((t["sym"], t["side"], mfe/dist_tp, mae/dist_sl, dist_tp/dist_sl, tf))

if rows:
    print(f"\n{'sym':7} {'side':6} {'travelled toward TP':>20} {'went against':>13} {'R:R':>6}")
    for s, sd, mf, ma, rr, tf in rows:
        bar = "█" * max(0, min(20, int(mf*20)))
        print(f"{s:7} {sd:6} {mf*100:6.1f}% {bar:<20} {ma*100:6.0f}% of SL  1:{rr:.1f}  [{tf}]")
    import statistics as st
    ms = [r[2] for r in rows]
    print(f"\n  n={len(ms)}  median travel toward TP = {st.median(ms)*100:.1f}%  "
          f"best {max(ms)*100:.1f}%  worst {min(ms)*100:.1f}%")
    print(f"  would a TP1 at 50% of the way have filled?  "
          f"{sum(1 for m in ms if m >= 0.5)}/{len(ms)}")
else:
    print("\n  no stale trades inside the 5m history window")

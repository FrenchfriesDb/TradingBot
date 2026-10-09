"""Fixed-parameter hypothesis test (crypto or stocks) against three random-entry controls.

USAGE (data comes from tools/fetch_research_crypto.py / fetch_research_stocks.py):
    MODE=crypto TFX=4  RR=1 python3 tools/measure_hypotheses.py     # crypto 4h  (1h bars x4)
    MODE=crypto TFX=24 RR=2 python3 tools/measure_hypotheses.py     # crypto daily
    MODE=stock  TFX=1  RR=1 python3 tools/measure_hypotheses.py     # stocks 15m, flat by 15:45 ET
    MODE=crypto DATASET=crypto_15m BASE_MIN=15 MIN_DAYS=100 TFX=1 RR=1 python3 tools/measure_hypotheses.py
Data lives OUTSIDE the repo, in  (default ~/Library/Caches/debbiela-research).

RESULT OF THE 2026-10-09 RUN: no strategy passed (see memory note simple-hypotheses-have-no-edge).
The point of keeping this is the METHOD: criteria fixed before results, three controls (random /
long-only / short-only) to expose drift, a discovery/holdout split, and a day-clustered t-stat.
A per-trade t-stat on 4h crypto trend-breakout read +3.3; the day-clustered one read +0.1.

PRE-DECLARED before any result: stop = k x ATR14 (primary k=2.0; 1.0 and 3.0 are robustness,
not a search), target = RR x stop, timeout 32 bars, one position per symbol, a bar containing
both levels counts as STOP, stops fill at their level.
A strategy is a CANDIDATE only if ALL hold at the primary k:
  1. gross expectancy > 0 in BOTH the discovery (first 60% of time) and holdout (last 40%)
  2. day-clustered t >= 2        (coins/stocks move together; per-trade t overstates)
  3. gross expectancy above the best of the three controls' upper range
  4. gross expectancy > 0 at >= 2 of the 3 stop widths
  5. crypto only: net of Intro-tier fees, best case (all-maker 0.5%/side), also > 0
Stocks: regular session only, no entries in the last 30 minutes, everything flat by 15:45 ET
(the bot's own rule). Costs there: $0 commission; a 1bp/side slippage column is an assumption.
"""
import glob, os
import numpy as np, pandas as pd

CACHE = os.environ.get("RESEARCH_CACHE", os.path.expanduser("~/Library/Caches/debbiela-research"))
MODE = os.environ.get("MODE", "crypto")
TFX = int(os.environ.get("TFX", "4" if MODE == "crypto" else "1"))
RR = float(os.environ.get("RR", "1.0"))
MAXBARS, K_PRIMARY = 32, 2.0
DATASET = os.environ.get("DATASET", "crypto_1h" if MODE == "crypto" else "stocks_15m")
BASE_MIN = int(os.environ.get("BASE_MIN", "15" if DATASET.endswith("15m") else "60"))   # minutes per RAW bar
BASE_MS = BASE_MIN * 60_000
MIN_DAYS = float(os.environ.get("MIN_DAYS", "365"))          # crypto: shorter histories are excluded
TAKER, MAKER, SLIP = 0.009, 0.005, 0.0001
CTRL_P = float(os.environ.get("CTRL_P", "0.01" if BASE_MIN * TFX <= 15 else "0.02"))

def load():
    data, dropped = {}, []
    for p in sorted(glob.glob(os.path.join(CACHE, DATASET, "*.csv"))):
        sym = os.path.basename(p)[:-4].replace("_", "/")
        df = pd.read_csv(p).sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
        if len(df) < 500: dropped.append((sym, f"{len(df)} bars")); continue
        span_d = (df["ts"].iloc[-1] - df["ts"].iloc[0]) / 86_400_000
        if MODE == "crypto":
            miss = 1 - len(df) / ((df["ts"].iloc[-1] - df["ts"].iloc[0]) / BASE_MS + 1)
            if span_d < MIN_DAYS or miss > 0.05:
                dropped.append((sym, f"{span_d:.0f}d history, {miss:.1%} missing")); continue
            g = df.groupby(df["ts"] // (TFX * BASE_MS))
            df = g.agg(ts=("ts", "first"), open=("open", "first"), high=("high", "max"), low=("low", "min"),
                       close=("close", "last"), volume=("volume", "sum"), cnt=("ts", "count"))
            df = df[df["cnt"] == TFX].drop(columns="cnt").reset_index(drop=True)
        else:
            t = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert("America/New_York")
            df["mod"] = t.dt.hour * 60 + t.dt.minute
            df["day"] = pd.factorize(t.dt.date)[0]
            df = df[(df["mod"] >= 570) & (df["mod"] < 960)].reset_index(drop=True)
            ndays_present = df["day"].nunique()
            expect = span_d * 5 / 7 * 0.965
            med = df.groupby("day").size().median()
            if ndays_present < 0.9 * expect or med < 23:
                dropped.append((sym, f"{ndays_present}/{expect:.0f} days, median {med:.0f} bars/day")); continue
            df["day"] = pd.factorize(df["day"])[0]
            last_ok = df["mod"] <= 930
            df["daylast"] = df.index.to_series().where(last_ok).groupby(df["day"]).transform("max")
            df["daylast"] = df["daylast"].ffill().astype(int)
        data[sym] = df
    return data, dropped

def prep(df):
    h, l, c = df["high"], df["low"], df["close"]; pc = c.shift(1)
    df["atr"] = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1).rolling(14).mean()
    for s in (20, 50, 200): df[f"ema{s}"] = c.ewm(span=s, adjust=False).mean()
    d = c.diff()
    up, dn = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean(), (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    mid, sd = c.rolling(20).mean(), c.rolling(20).std()
    df["bb_lo"], df["bb_hi"] = mid - 2.5 * sd, mid + 2.5 * sd
    df["hh20"], df["ll20"] = h.rolling(20).max().shift(1), l.rolling(20).min().shift(1)

def signals(df):
    c, o, h, l = df["close"], df["open"], df["high"], df["low"]
    up, dn = df["ema50"] > df["ema200"], df["ema50"] < df["ema200"]
    sig = lambda a, b: np.where(a, 1, np.where(b, -1, 0))
    sw_l = (l < df["ll20"]) & (c > df["ll20"]) & (c > o); sw_s = (h > df["hh20"]) & (c < df["hh20"]) & (c < o)
    return {"S1 trend breakout": sig((c > df["hh20"]) & up, (c < df["ll20"]) & dn),
            "S2 trend pullback": sig(up & (l <= df["ema20"]) & (c > df["ema20"]) & (c > o),
                                     dn & (h >= df["ema20"]) & (c < df["ema20"]) & (c < o)),
            "S3 mean reversion": sig((c < df["bb_lo"]) & (df["rsi"] < 25), (c > df["bb_hi"]) & (df["rsi"] > 75)),
            "S4 sweep & reclaim": sig(sw_l, sw_s), "S5 sweep + trend": sig(sw_l & up, sw_s & dn)}

def simulate(df, sig, k):
    o, h, l, c, a, ts = (df[x].values for x in ("open", "high", "low", "close", "atr", "ts"))
    stock = MODE == "stock"
    if stock: day, mod, dlast = df["day"].values, df["mod"].values, df["daylast"].values
    n, out, i = len(df), [], 0
    while i < n - 2:
        s = sig[i]
        if s == 0 or not np.isfinite(a[i]) or a[i] <= 0: i += 1; continue
        e = i + 1
        if stock and (day[e] != day[i] or not (600 <= mod[e] <= 915)): i += 1; continue
        entry, risk = o[e], k * a[i]
        stop, tgt = entry - s * risk, entry + s * risk * RR
        last = min(i + MAXBARS, dlast[e]) if stock else min(i + MAXBARS, n - 1)
        res = None
        for j in range(e, last + 1):
            hs = (l[j] <= stop) if s == 1 else (h[j] >= stop)
            ht = (h[j] >= tgt) if s == 1 else (l[j] <= tgt)
            if hs: res = (-1.0, "S", j); break
            if ht: res = (RR, "T", j); break
        if res is None: res = (s * (c[last] - entry) / risk, "X", last)
        out.append((ts[e], res[0], res[1], risk / entry))
        i = res[2] + 1
    return out

def tstat(x): return x.mean() / (x.std(ddof=1) / np.sqrt(len(x))) if len(x) > 2 and x.std(ddof=1) > 0 else np.nan

def summarize(rows, ndays, t_split):
    if not rows: return None
    d = pd.DataFrame(rows, columns=["sym", "ts", "R", "kind", "risk"])
    if MODE == "crypto":
        d["net_a"], d["net_b"] = d["R"] - 2 * TAKER / d["risk"], d["R"] - 2 * MAKER / d["risk"]
    else:
        d["net_a"] = d["R"] - 2 * SLIP / d["risk"]; d["net_b"] = np.nan
    day = d.groupby(d["ts"] // 86_400_000)["R"].mean()
    per = d.groupby("sym")["R"].agg(["count", "mean"]); per = per[per["count"] >= 10]
    a, b = d[d["ts"] < t_split]["R"], d[d["ts"] >= t_split]["R"]
    return dict(n=len(d), per_day=len(d) / ndays, win=(d["kind"] == "T").mean(), tmo=(d["kind"] == "X").mean(),
                exp=d["R"].mean(), t=tstat(d["R"]), t_day=tstat(day),
                breadth=(per["mean"] > 0).mean() if len(per) else np.nan,
                disc=a.mean() if len(a) else np.nan, hold=b.mean() if len(b) else np.nan, hold_t=tstat(b),
                net_a=d["net_a"].mean(), net_b=d["net_b"].mean(), risk=d["risk"].median() * 100)

def main():
    data, dropped = load()
    for s, why in dropped: print(f"  EXCLUDED {s}: {why}")
    for s in data: prep(data[s])
    syms = list(data)
    t0 = min(d["ts"].iloc[0] for d in data.values()); t1 = max(d["ts"].iloc[-1] for d in data.values())
    ndays = (t1 - t0) / 86_400_000; t_split = t0 + 0.6 * (t1 - t0)
    mins = BASE_MIN * TFX
    lbl = f"{mins // 1440}d" if mins % 1440 == 0 else f"{mins // 60}h" if mins % 60 == 0 else f"{mins}m"
    print(f"  MODE={MODE} bars={lbl} RR=1:{RR:g}  {len(syms)} symbols, {ndays:.0f} days; discovery = first 60%, holdout = last 40%\n")
    sigs = {s: signals(data[s]) for s in syms}
    names = list(next(iter(sigs.values())))
    res = {}
    for k in (K_PRIMARY, 1.0, 3.0):
        arms = {}
        for nm in names:
            rows = []
            for s in syms: rows += [(s, *r) for r in simulate(data[s], sigs[s][nm], k)]
            arms[nm] = summarize(rows, ndays, t_split)
        for cname, choices in (("CONTROL random dir", [-1, 1]), ("CONTROL long-only", [1]), ("CONTROL short-only", [-1])):
            cs = []
            for seed in range(5):
                rng = np.random.default_rng(seed); rows = []
                for s in syms:
                    m = len(data[s]); sg = np.where(rng.random(m) < CTRL_P, rng.choice(choices, m), 0)
                    rows += [(s, *r) for r in simulate(data[s], sg, k)]
                r = summarize(rows, ndays, t_split)
                if r: cs.append(r)
            if cs:
                arms[cname] = {**{key: np.mean([c[key] for c in cs]) for key in cs[0]},
                               "exp_lo": min(c["exp"] for c in cs), "exp_hi": max(c["exp"] for c in cs)}
        res[k] = arms
    for k in (K_PRIMARY, 1.0, 3.0):
        print(f"=== stop = {k} x ATR ({'PRIMARY' if k == K_PRIMARY else 'robustness'}), RR 1:{RR:g}, {MAXBARS}-bar timeout, GROSS then net ===")
        nets = "netTaker netMaker" if MODE == "crypto" else "net@1bp "
        print(f"  {'arm':22s} {'n':>6s} {'/day':>5s} {'win%':>6s} {'tmo%':>5s} {'grossR':>7s} {'t':>6s} {'t_day':>6s} {'sym+':>5s} "
              f"{'disc':>7s} {'hold':>7s} {'hold_t':>6s}  {nets}  risk%")
        for nm, r in res[k].items():
            if r is None: print(f"  {nm:22s} no trades"); continue
            nb = f"{r['net_b']:+8.3f}" if MODE == "crypto" else ""
            print(f"  {nm:22s} {r['n']:6.0f} {r['per_day']:5.1f} {r['win']:6.1%} {r['tmo']:5.0%} {r['exp']:+7.3f} {r['t']:+6.2f} "
                  f"{r['t_day']:+6.2f} {r['breadth']:5.0%} {r['disc']:+7.3f} {r['hold']:+7.3f} {r['hold_t']:+6.2f}  "
                  f"{r['net_a']:+8.3f} {nb}  {r['risk']:5.2f}"
                  + (f"  [ctrl range {r['exp_lo']:+.3f}..{r['exp_hi']:+.3f}]" if "exp_lo" in r else ""))
        print()
    print("=== CANDIDATE CHECK (pre-declared criteria, mechanical) ===")
    ctrl_hi = max(res[K_PRIMARY][c]["exp_hi"] for c in res[K_PRIMARY] if c.startswith("CONTROL"))
    found = False
    for nm in names:
        r = res[K_PRIMARY].get(nm)
        if not r: continue
        npos = sum(1 for k in (K_PRIMARY, 1.0, 3.0) if res[k].get(nm) and res[k][nm]["exp"] > 0)
        checks = {"disc>0 & hold>0": r["disc"] > 0 and r["hold"] > 0, "t_day>=2": r["t_day"] >= 2,
                  f"beats best control ({ctrl_hi:+.3f})": r["exp"] > ctrl_hi, ">0 at 2 of 3 stops": npos >= 2}
        if MODE == "crypto": checks["net (all-maker) > 0"] = r["net_b"] > 0
        ok = all(checks.values()); found |= ok
        print(f"  {nm:22s} {'CANDIDATE' if ok else 'no':9s} " + "  ".join(f"{'✓' if v else '✗'} {kk}" for kk, v in checks.items()))
    print(f"\n  -> {'at least one candidate' if found else 'NO candidate passes the pre-declared criteria'}")

if __name__ == "__main__":
    main()

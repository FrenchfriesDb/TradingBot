#!/usr/bin/env python3
"""
Backtest the stock bot's SMC strategy on Alpaca INTRADAY bars.

Why this file exists
--------------------
tradingbot.py's run_backtest() uses YahooDataBacktesting, which serves DAILY bars
only. DebbieLaSMC is a 15-minute execution strategy hanging off a 4H bias, so daily
candles cannot exercise its entry logic at all — the sweep/BOS/FVG sequence it looks
for simply isn't visible at that resolution. That backtest has therefore never tested
this strategy. This one runs the SAME DebbieLaSMC class against Alpaca minute bars,
so what gets measured is the code that actually trades.

Nothing here re-implements the strategy. backtest_crypto.py mirrors binance_bot's
entry logic by hand (it has to — that bot has no Lumibot harness), and that mirror
can silently drift from production. Here we drive the real class, so drift is
impossible by construction.

What is neutralized, and why it's safe
--------------------------------------
The live entry/exit path has side effects that must not run in a backtest:

  * Google Sheets      — would append hundreds of FAKE trades to your real
                         "Stock Ledger" tab, corrupting the live record.
  * TradingView shots  — Playwright screenshot per closed trade. Minutes each.
  * GitHub upload      — pushes chart images to the chart-images branch.
  * FinBERT / news     — loads a transformer and hits the news API per symbol per
                         iteration. Slow, and reading today's news while replaying
                         a past bar is look-ahead bias.

Stubbing the news read is BEHAVIOR-PRESERVING, not a simplification: _get_sentiment
already returns confirm=True unconditionally (see its docstring — news stopped being
a veto on 2026-08-13 and is now context for the AI only). So removing it changes
nothing about which trades are taken.

The AI gate
-----------
get_ai_confirmation() short-circuits to (True, MIN_AI_RR, "no AI key") when
NVIDIA_API_KEY is empty. We force that by default, which makes the run deterministic
and repeatable — and, more importantly, honest: the AI's prompt includes today's news,
so letting it vote while replaying history would leak the future into past decisions.
The default run therefore measures the TECHNICAL edge. Pass --ai to let the live model
vote anyway (slow, non-reproducible, and look-ahead-contaminated — use it to eyeball
the AI's reasoning on specific setups, not to judge expectancy).
"""

import argparse
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta

# Lumibot reads credentials at import time, so load .env before anything else.
from dotenv import load_dotenv
load_dotenv()

FILLS = []      # every simulated fill, in order — the authoritative trade record


def _force_feed(feed_name: str):
    """Make lumibot ask Alpaca for a data feed it is actually entitled to.

    lumibot builds its StockBarsRequest with no `feed` argument
    (backtesting/alpaca_backtesting.py:623), so alpaca-py defaults it to SIP — the full
    consolidated tape. On the free data plan every such request is a hard 403:

        403 Forbidden .../v2/stocks/bars?...timeframe=1Min&symbols=SPY
        {"message":"subscription does not permit querying recent SIP data"}

    and the strategy thread dies before the first bar, while the progress bar keeps
    drawing and the process still exits 0. Patched here rather than in the library, and
    only inside the backtest sandbox — nothing live goes through this path.

    CAVEAT, and it is not a small one: IEX is a single venue carrying roughly 2-3% of
    consolidated volume. Its bar highs, lows and closes are genuinely different from
    SIP's, thinner and gappier, so a backtest on IEX is an approximation of the tape the
    live bot trades. Directional conclusions survive that; precise fills and win rates do
    not. Use --feed sip if the account is ever upgraded.
    """
    import lumibot.backtesting.alpaca_backtesting as _ab
    from alpaca.data.enums import DataFeed
    feed = DataFeed.SIP if str(feed_name).lower() == "sip" else DataFeed.IEX
    _orig = _ab.StockBarsRequest

    def _with_feed(*a, **kw):
        kw.setdefault("feed", feed)
        return _orig(*a, **kw)

    _ab.StockBarsRequest = _with_feed
    if feed is DataFeed.IEX:
        print("  data feed: IEX (free plan) — ~2-3% of consolidated volume; "
              "treat fills and win rate as approximate")
    else:
        print("  data feed: SIP (full consolidated tape)")


def sandbox_strategy(use_ai: bool):
    """Replace every live side effect with a no-op, and hook trade closes.

    Patching module globals in bot.strategy (rather than editing the strategy) keeps
    production code untouched — the backtest cannot change how the live bot behaves.
    """
    import bot.strategy as S

    if not use_ai:
        # get_ai_confirmation reads this module global; emptying it takes the
        # documented "no AI key -> proceed on technicals at the R:R floor" path.
        S.NVIDIA_API_KEY = ""

    # ── Google Sheets: never touch the live ledger ────────────────────────────
    S.get_sheet_client       = lambda *a, **k: None
    S.ensure_tabs            = lambda *a, **k: None
    S.log_daily_snapshot     = lambda *a, **k: None
    S.log_trade              = lambda *a, **k: None
    S.missing_snapshot_dates = lambda *a, **k: []

    # ── Charts: Playwright + GitHub push, far too slow per trade ──────────────
    S.render_trade_chart       = lambda *a, **k: None
    S.render_stock_tradingview = lambda *a, **k: None
    S.save_chart_locally       = lambda *a, **k: None
    S.upload_chart_to_github   = lambda *a, **k: None

    # ── State files: a backtest must not read or clobber LIVE position state ──
    tmp = tempfile.mkdtemp(prefix="bt_state_")
    # Ledgers MUST be redirected, not just Sheets. bot/strategy.py appends every close to
    # the durable local ledger and every retest refusal to the shadow ledger BEFORE any
    # network call, so an un-sandboxed backtest writes simulated rows into the real files.
    # It already did: 27 of 51 shadow rows were backtest artifacts, identifiable only
    # because the live stock bot had logged zero refusals that day.
    from bot import trade_ledger as _tl, shadow_ledger as _sl
    _tl.DEFAULT_DIR = os.path.join(tmp, "ledger")
    _sl.shadow_path = lambda base_dir=None: os.path.join(tmp, "ledger", "shadow.jsonl")
    S.STRATEGY_STATE_FILE = os.path.join(tmp, "strategy_state.json")
    S.LOGGED_CLOSES_FILE  = os.path.join(tmp, "logged_closes.json")

    # ── News: log-only in production (never vetoes), so this is a pure speedup ─
    S.DebbieLaSMC._get_sentiment = lambda self, symbol: (True, "backtest", 0.0)

    # ── Sheets write becomes a no-op; it is NOT a usable trade source ─────────
    # Measured 2026-08-19: over one month the strategy closed 39 positions but this
    # hook fired for only 14 of them. _log_broker_side_close (the common exit path)
    # asks the LIVE Alpaca REST API for the real closing fill; in a backtest no such
    # order exists, so it throws and its fail-soft `except` swallows the trade whole.
    # Capturing here undercounted by ~64% and skewed every statistic. We therefore
    # capture FILLS instead — the simulated executions themselves, which is what a
    # backtest actually knows to be true.
    S.DebbieLaSMC._log_trade_close_to_sheet = lambda self, *a, **k: None

    orig_filled = S.DebbieLaSMC.on_filled_order

    def _on_filled(self, position, order, price, quantity, multiplier):
        try:
            sym = getattr(getattr(order, "asset", None), "symbol", None) or str(order.asset)
            # Capture the STOP the strategy planned for this symbol. Without it a
            # round-trip's P&L can only be read in DOLLARS, and dollars cannot tell
            # "R is asymmetric" apart from "position sizes differ" — risk is capped at
            # $30/trade but sized in WHOLE shares, so every trade risks a different
            # amount. A 45d run came back avg win +$16.77 vs avg loss -$22.09 and that
            # gap is unreadable without R. (It also disproved the EOD-truncation theory:
            # 0 of 14 exits were in the flatten window.)
            FILLS.append({
                "symbol": sym,
                "side":   str(getattr(order, "side", "")).lower(),
                "price":  float(price),
                "qty":    abs(float(quantity)),
                "dt":     self.get_datetime(),
                "stop":   (self.stop_loss or {}).get(sym),
                "target": (self.take_profit or {}).get(sym),
            })
        except Exception:
            pass          # never let bookkeeping break the run
        return orig_filled(self, position, order, price, quantity, multiplier)

    S.DebbieLaSMC.on_filled_order = _on_filled
    return S


def pair_round_trips(fills):
    """Fold a fill stream into closed round-trips, FIFO per symbol.

    A fill that increases exposure from flat opens a lot; offsetting fills close it,
    possibly across several partial fills. Anything still open when the backtest ends
    is dropped (it never realised a P&L, so it cannot count toward expectancy).
    """
    from collections import defaultdict, deque
    open_lots = defaultdict(deque)      # symbol -> deque of [side, qty, price, dt]
    trades = []

    for f in sorted(fills, key=lambda x: (x["dt"] is None, x["dt"])):
        sym, side, qty, px, dt = f["symbol"], f["side"], f["qty"], f["price"], f["dt"]
        if qty <= 0:
            continue
        lots = open_lots[sym]
        # Same direction as the resting lot (or none) -> opening/adding.
        if not lots or lots[0][0] == side:
            lots.append([side, qty, px, dt, f.get("stop")])
            continue
        # Opposite direction -> close against the oldest lot(s).
        remaining = qty
        while remaining > 0 and lots:
            lot = lots[0]
            take = min(remaining, lot[1])
            is_long = lot[0] == "buy"
            pnl = (px - lot[2]) * take if is_long else (lot[2] - px) * take
            trades.append({
                "symbol": sym,
                "side":   "LONG" if is_long else "SHORT",
                "entry":  lot[2],
                "exit":   px,
                "qty":    take,
                "pnl":    pnl,
                "entry_iso": lot[3],
                "exit_iso":  dt,
                "stop":   lot[4],          # planned at ENTRY, so R is the risk taken on
            })
            lot[1] -= take
            remaining -= take
            if lot[1] <= 1e-9:
                lots.popleft()
        if remaining > 1e-9:            # flipped straight through flat into a reversal
            lots.append(["buy" if side == "buy" else "sell", remaining, px, dt, f.get("stop")])
    return trades


FUNNEL = Counter()
GATES = Counter()   # state-transition tallies, filled when --funnel is on


def instrument_funnel(S):
    """Tally every state-machine transition.

    A zero-trade backtest is ambiguous on its own: it could mean the tape offered
    no setups, or that one specific gate is rejecting everything. Counting
    IDLE->SWEEP_HUNT->ENTRY_WAIT->POSITION_OPEN transitions tells those apart, and
    points at WHICH gate leaks. We read self.state rather than hooking internals so
    this stays valid if _process_symbol is refactored.
    """
    # Gate-level tally. The transition counts below tell you setups armed and never
    # converted; they do NOT tell you WHICH gate ate them, which is the whole question a
    # zero-trade run raises. Wrapping the pure gates answers it: on 2026-09-29 this
    # localised the wall to has_displacement, 2 passes out of 264, matching the live log.
    from bot import indicators as _I
    for _name in ("zone_tapped", "tap_chase_ok", "zone_left_since_arming",
                  "has_displacement", "zone_broken", "price_in_entry_zone"):
        _f = getattr(_I, _name, None)
        if _f is None:
            continue

        def _mk(n, f):
            def _w(*a, **k):
                r = f(*a, **k)
                GATES[f"{n}:{'pass' if (r[0] if isinstance(r, tuple) else r) else 'FAIL'}"] += 1
                return r
            return _w
        setattr(_I, _name, _mk(_name, _f))

    # DATA COVERAGE. Two runs of an IDENTICAL config (same window, same symbols, same
    # threshold) produced 39 trades and 28 trades. AMD, NVDA and TSLA reproduced exactly;
    # GOOGL went from 8 trades to 1. The simulation is deterministic — the DATA is not.
    # IEX is a single venue carrying ~2-3% of consolidated volume, and when a symbol's
    # bars arrive short nothing says so: the run just reports a different answer with the
    # same confident formatting. That is how a threshold sweep turns into noise.
    # Same failure as backtest_crypto's short-page pagination bug: silent partial history.
    COVERAGE = defaultdict(lambda: {"calls": 0, "bars": 0, "empty": 0, "short": 0})
    _orig_hist = S.DebbieLaSMC.get_historical_prices

    def _counted_hist(self, asset, length, timestep="minute", **kw):
        out = _orig_hist(self, asset, length, timestep, **kw)
        try:
            key = f"{getattr(asset, 'symbol', asset)}:{timestep}"
            c = COVERAGE[key]
            c["calls"] += 1
            n = 0 if out is None or getattr(out, "df", None) is None else len(out.df)
            c["bars"] += n
            if n == 0:
                c["empty"] += 1
            elif n < length:
                c["short"] += 1
        except Exception:
            pass          # instrumentation must never break the run it measures
        return out

    S.DebbieLaSMC.get_historical_prices = _counted_hist

    orig = S.DebbieLaSMC._process_symbol

    def wrapped(self, symbol):
        before = self.state.get(symbol)
        out = orig(self, symbol)
        after = self.state.get(symbol)
        FUNNEL["iterations"] += 1
        if before != after:
            FUNNEL[f"{before} -> {after}"] += 1
        return out

    S.DebbieLaSMC._process_symbol = wrapped


def report_funnel():
    if not FUNNEL:
        return
    print("\n  Funnel (where setups died):")
    print(f"    iterations            {FUNNEL['iterations']}")
    for k, v in sorted(FUNNEL.items()):
        if k != "iterations":
            print(f"    {k:<34}{v}")
    if GATES:
        # zone_broken is INVERTED: a "pass" there means the zone WAS broken and the
        # symbol was released, so a low rate is healthy, not a bottleneck. Excluded from
        # the tightest-gate pick and labelled, or it wins the ranking every time.
        INVERTED = {"zone_broken"}
        print("\n  Gates (pass/FAIL at each check):")
        names = sorted({k.rsplit(":", 1)[0] for k in GATES})
        for n in names:
            p_, f_ = GATES.get(f"{n}:pass", 0), GATES.get(f"{n}:FAIL", 0)
            tot = p_ + f_
            if not tot:
                continue
            bar = "#" * int(round(20 * p_ / tot))
            tag = "  (inverted: pass = released)" if n in INVERTED else ""
            print(f"    {n:<26}{p_:5d} pass /{f_:5d} FAIL  {p_/tot*100:3.0f}% {bar}{tag}")
        worst = min((n for n in names
                     if n not in INVERTED
                     and GATES.get(f"{n}:pass", 0) + GATES.get(f"{n}:FAIL", 0) >= 20),
                    key=lambda n: GATES.get(f"{n}:pass", 0) /
                                  max(1, GATES.get(f"{n}:pass", 0) + GATES.get(f"{n}:FAIL", 0)),
                    default=None)
        if worst:
            p_ = GATES.get(f"{worst}:pass", 0)
            t_ = p_ + GATES.get(f"{worst}:FAIL", 0)
            print(f"\n    -> tightest gate: {worst} ({p_}/{t_} pass). A zero-trade run is "
                  f"that gate,\n       not an absence of setups.")
    if not FUNNEL.get("ENTRY_WAIT -> POSITION_OPEN"):
        print("    -> zones armed but never converted: price never tapped the zone,")
        print("       or the R:R floor / stop distance rejected every attempt.")


def report(trades, start, end, symbols):
    """Expectancy-first summary — deliberately the same shape as backtest_crypto.py
    so crypto and stock results can be read side by side."""
    print("\n" + "=" * 66)
    if not trades:
        print("  NO TRADES — the entry rules never triggered in this window.")
        print("=" * 66)
        print("  That is a RESULT, not a failure. This strategy stacks a daily-trend")
        print("  filter, a 4H BOS, a sweep, an FVG tap and an R:R floor; long dry")
        print("  spells are expected. Widen --start/--end or add symbols before")
        print("  concluding anything.")
        report_funnel()
        return

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw, gl = sum(wins), abs(sum(losses))
    equity = peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    _cov = [(k, v) for k, v in sorted(COVERAGE.items()) if v["calls"]]
    if _cov:
        _bad = [(k, v) for k, v in _cov if v["empty"] or v["short"]]
        print("\n  DATA COVERAGE — a run whose bars arrived short is not comparable")
        print(f"    {'symbol:tf':<18} {'calls':>7} {'avg bars':>9} {'empty':>6} {'short':>6}")
        for k, v in _cov:
            flag = "  <-- INCOMPLETE" if (v["empty"] or v["short"]) else ""
            print(f"    {k:<18} {v['calls']:>7,} {v['bars']/v['calls']:>9.0f} "
                  f"{v['empty']:>6,} {v['short']:>6,}{flag}")
        if _bad:
            print(f"    {len(_bad)} of {len(_cov)} series arrived incomplete — treat any "
                  f"comparison against another run as UNSAFE until this is clean.")
    print(f"\n  RESULTS — {len(trades)} trades   {start:%Y-%m-%d} → {end:%Y-%m-%d}")
    print("=" * 66)
    print(f"  net P&L         {sum(pnls):+,.2f}")
    print(f"  expectancy      {sum(pnls)/len(pnls):+,.2f} per trade   <-- the number that matters")
    print(f"  win rate        {len(wins)/len(pnls)*100:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  avg win         {gw/len(wins):+,.2f}" if wins else "  avg win         n/a")
    print(f"  avg loss        {-gl/len(losses):+,.2f}" if losses else "  avg loss        n/a")
    print(f"  profit factor   {gw/gl:.2f}" if gl else "  profit factor   inf")
    print(f"  max drawdown    {max_dd:,.2f}")

    per = {}
    for t in trades:
        per.setdefault(t["symbol"], []).append(t["pnl"])
    print("  per symbol:     " + "  ".join(
        f"{k}={len(v)} ({sum(v):+,.0f})" for k, v in sorted(per.items())))

    # ── R SYMMETRY ────────────────────────────────────────────────────────────────────
    # At a 1:1 target, wins and losses must be the SAME SIZE IN R. Dollars cannot show
    # that (whole-share sizing varies the risk per trade), so score each round-trip
    # against the stop that was planned at ENTRY.
    rs = []
    for t in trades:
        try:
            stop = float(t.get("stop"))
            R = abs(float(t["entry"]) - stop)
            if R <= 0:
                continue
            d = (float(t["exit"]) - float(t["entry"]))
            rs.append(d / R if t["side"] == "LONG" else -d / R)
        except (TypeError, ValueError):
            continue
    if rs:
        w = [r for r in rs if r > 0]
        l = [r for r in rs if r <= 0]
        print(f"\n  in R (n={len(rs)} of {len(trades)} with a recorded stop):")
        print(f"    avg win   {sum(w)/len(w):>+6.2f}R" if w else "    avg win   n/a")
        print(f"    avg loss  {sum(l)/len(l):>+6.2f}R" if l else "    avg loss  n/a")
        print(f"    expectancy{sum(rs)/len(rs):>+6.2f}R")
        # SIGNIFICANCE. An expectancy without its standard error is not a result, and this
        # project has repeatedly read thin samples as edges: a +0.19R figure from a proxy
        # population came back quoted as this bot's measured edge, and a 64% win rate over
        # 45 trades turned out to be two symbols. Added to backtest_crypto first; a
        # displacement-threshold sweep then compared +0.31R (n=39) against +0.08R (n=30)
        # with no way to say whether the gap was real.
        if len(rs) > 2:
            import statistics as _st
            _m = sum(rs) / len(rs)
            _se = _st.stdev(rs) / (len(rs) ** 0.5)
            _t = _m / _se if _se else 0.0
            _need = int((2 * _st.stdev(rs) / abs(_m)) ** 2) + 1 if _m else 0
            print(f"    s.e.      {_se:>6.3f}R  (sd {_st.stdev(rs):.2f})   t = {_t:+.2f}")
            print(f"    verdict   {'DISTINGUISHABLE from zero' if abs(_t) >= 2 else 'INDISTINGUISHABLE from zero'}"
                  + (f" — needs n ~ {_need:,}" if abs(_t) < 2 and _need else ""))
        if w and l:
            sym = abs(sum(w)/len(w)) / abs(sum(l)/len(l))
            # The first version printed "wins really are smaller" for BOTH tails, so a
            # ratio of 1.17 — wins LARGER than losses — was reported as wins being
            # smaller. Say which direction.
            if 0.85 <= sym <= 1.15:
                note = "symmetric"
            elif sym > 1.15:
                note = "wins are LARGER than losses"
            else:
                note = "wins are SMALLER than losses"
            print(f"    win/loss size ratio {sym:.2f}  ({note})")
    else:
        print("\n  (no stops recorded — R symmetry unavailable)")

    # ── PER-SYMBOL AND PER-MONTH ─────────────────────────────────────────────────────
    # Added 2026-10-03. The 120d run showed +$406 net at a 64% win rate, but a P&L split
    # revealed NFLX (+232) and PLTR (+171) carried 99% of it while the other five symbols
    # netted +$3 between them — and the most recent 45 days of the same window ran -$37.
    # A headline win rate hides both. These two cuts answer "is the edge broad?" and
    # "is it still working?", which have opposite responses if the answer is no.
    def _rmult(t):
        try:
            stop = float(t.get("stop")); R = abs(float(t["entry"]) - stop)
            if R <= 0:
                return None
            d = float(t["exit"]) - float(t["entry"])
            return d / R if t["side"] == "LONG" else -d / R
        except (TypeError, ValueError):
            return None

    def _bucketed(title, keyfn, order=None):
        buckets = {}
        for t in trades:
            r = _rmult(t)
            if r is None:
                continue
            buckets.setdefault(keyfn(t), []).append((r, t["pnl"]))
        if not buckets:
            return
        keys = order(buckets) if order else sorted(buckets)
        print(f"\n  {title}")
        print(f"    {'key':<9} {'n':>4} {'win%':>6} {'exp R':>8} {'tot R':>8} {'net $':>9}")
        for k in keys:
            v = buckets[k]
            w = sum(1 for r, _ in v if r > 0)
            e = sum(r for r, _ in v) / len(v)
            print(f"    {str(k):<9} {len(v):>4} {w/len(v)*100:>5.0f}% {e:>+8.2f} "
                  f"{e*len(v):>+8.1f} {sum(p for _, p in v):>+9.0f}")

    _bucketed("PER SYMBOL — is the edge broad, or two lucky names?",
              lambda t: t["symbol"],
              order=lambda b: sorted(b, key=lambda k: -sum(r for r, _ in b[k])))

    def _month(t):
        try:
            x = t["entry_iso"]
            x = x if hasattr(x, "strftime") else _dtmod.fromisoformat(str(x))
            return x.strftime("%Y-%m")
        except Exception:
            return "?"
    from datetime import datetime as _dtmod
    _bucketed("PER MONTH — is it decaying, or was one stretch just good?", _month)

    # ── PER-TRADE DUMP ────────────────────────────────────────────────────────────────
    # Added 2026-10-03 to find what closes trades before either level. Averages said
    # +0.69R / -0.77R — both short of a full R — and the averages alone cannot say whether
    # that is "everything exits early" or "some hit +-1R and others exit tiny". HOLD
    # DURATION separates them: STALE_TRADE_HOURS is the only early-exit path left (EOD was
    # 0 of 14, and this bot has no breakeven move), so a cluster at ~6h IS the timer.
    try:
        from datetime import datetime as _dt
        print(f"\n  per trade ({'R':>6} {'hold':>7} {'exit vs':>9}):")
        print(f"  {'sym':<6} {'side':<5} {'R':>6} {'hold h':>7} {'ended':>9}")
        for t in sorted(trades, key=lambda x: str(x.get("entry_iso"))):
            try:
                stop = float(t.get("stop")); R = abs(float(t["entry"]) - stop)
                d = float(t["exit"]) - float(t["entry"])
                rm = (d / R) if t["side"] == "LONG" else (-d / R)
            except (TypeError, ValueError, ZeroDivisionError):
                rm = float("nan")
            try:
                a, b = t["entry_iso"], t["exit_iso"]
                a = a if hasattr(a, "timestamp") else _dt.fromisoformat(str(a))
                b = b if hasattr(b, "timestamp") else _dt.fromisoformat(str(b))
                hrs = (b - a).total_seconds() / 3600
            except Exception:
                hrs = float("nan")
            # label by how close the exit landed to a full R
            if rm == rm:
                tag = "TARGET" if rm >= 0.95 else ("STOP" if rm <= -0.95 else "early")
            else:
                tag = "?"
            print(f"  {t['symbol']:<6} {t['side']:<5} {rm:>+6.2f} {hrs:>7.1f} {tag:>9}")
        # how much of the book never reached either level
        early = [t for t in trades
                 if t.get("stop") and abs(float(t["entry"]) - float(t["stop"])) > 0
                 and abs(((float(t["exit"]) - float(t["entry"])) /
                          abs(float(t["entry"]) - float(t["stop"]))) *
                         (1 if t["side"] == "LONG" else -1)) < 0.95]
        if trades:
            print(f"\n  -> {len(early)}/{len(trades)} ({len(early)/len(trades)*100:.0f}%) ended "
                  f"at NEITHER the target nor the stop.")
    except Exception as _e:
        print(f"\n  (per-trade dump unavailable: {type(_e).__name__}: {_e})")

    # ── EXIT TIMING ───────────────────────────────────────────────────────────────────
    # Added 2026-10-03. The 45d run came back 50% win with the average LOSS 32% bigger
    # than the average WIN — impossible if the bot were taking clean +1R wins against
    # clean -1R losses at a 1:1 target. The suspect is the EOD flatten truncating winners
    # while losers run the full distance to the stop: a position up +0.4R at 15:45 books
    # +0.4R, a losing one takes the whole -1R.
    #
    # The fills carry no exit REASON, but they carry the exit TIME, and that is enough:
    # anything closing inside the flatten window was closed by the clock, not by the
    # setup. Split the P&L on that line and the asymmetry either explains itself or does
    # not.
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        from bot.strategy import EOD_FLATTEN_MIN
        _ET = ZoneInfo("America/New_York")
        buckets = {"EOD flatten": [], "intraday": [], "unknown": []}
        for t in trades:
            try:
                x = t["exit_iso"]
                x = x if hasattr(x, "astimezone") else _dt.fromisoformat(str(x))
                et = x.astimezone(_ET)
                mins_left = (16 * 60) - (et.hour * 60 + et.minute)
                key = "EOD flatten" if 0 <= mins_left <= EOD_FLATTEN_MIN else "intraday"
            except Exception:
                key = "unknown"
            buckets[key].append(t["pnl"])
        shown = {k: v for k, v in buckets.items() if v}
        if shown:
            print(f"\n  exits by TIMING (flatten window = last {EOD_FLATTEN_MIN} min):")
            print(f"  {'bucket':>14} {'n':>4} {'share':>7} {'net $':>10} {'avg $':>9} "
                  f"{'W/L':>8}")
            for k, v in sorted(shown.items(), key=lambda kv: -len(kv[1])):
                w = sum(1 for x in v if x > 0)
                print(f"  {k:>14} {len(v):>4} {len(v)/len(trades)*100:>6.0f}% "
                      f"{sum(v):>+10.2f} {sum(v)/len(v):>+9.2f} {w:>3}W/{len(v)-w:<3}L")
            eod = shown.get("EOD flatten", [])
            if eod and len(eod) / len(trades) > 0.3:
                print(f"  -> {len(eod)/len(trades)*100:.0f}% of trades are closed by the CLOCK, "
                      f"not by the setup. A 1:1 target cannot pay if most")
                print(f"     winners are cut before they reach it while losers run to the stop.")
    except Exception as _e:
        print(f"\n  (exit-timing breakdown unavailable: {type(_e).__name__}: {_e})")

    print("\n  Caveats — read before trusting the sign:")
    print("   * Slippage/commission only modelled if --fee-pct was passed; fills are otherwise ideal.")
    print("   * AI gate bypassed (technicals only) unless --ai was passed.")
    print("   * A handful of trades is noise. Judge the sign, not the cents.")
    report_funnel()


def main():
    ap = argparse.ArgumentParser(
        description="Backtest DebbieLaSMC on Alpaca intraday bars.")
    ap.add_argument("--days", type=int, default=60,
                    help="lookback window ending today (ignored if --start given)")
    ap.add_argument("--start", type=str, default=None, help="YYYY-MM-DD")
    ap.add_argument("--end", type=str, default=None, help="YYYY-MM-DD")
    ap.add_argument("--symbols", type=str, default="SPY,QQQ,AAPL,NVDA,TSLA,GOOGL")
    ap.add_argument("--cash-at-risk", type=float, default=0.02)
    ap.add_argument("--budget", type=float, default=100_000.0)
    ap.add_argument("--fee-pct", type=float, default=0.0,
                    dest="fee_pct",
                    help="per-side cost as a PERCENT of trade value (e.g. 0.05 for 5bps). "
                         "Default 0 because US equities are commission-free at Alpaca — but "
                         "set it to model spread/slippage, which are NOT free. The crypto bot "
                         "audit showed unmodelled costs flipping +$725 gross to -$15 net.")
    ap.add_argument("--warm-up", type=int, default=90, dest="warm_up",
                    help="trading days of history loaded before the window opens. "
                         "Must exceed 60 or the daily EMA50 trend filter vetoes "
                         "everything (default 90)")
    ap.add_argument("--funnel", action="store_true",
                    help="tally state-machine transitions to show WHERE setups died")
    ap.add_argument("--feed", type=str, default="iex", choices=["iex", "sip"],
                    help="Alpaca data feed. Free plans only have IEX; SIP 403s. "
                         "IEX is one venue (~2-3%% of volume) so fills are approximate.")
    ap.add_argument("--ai", action="store_true",
                    help="let the live Llama gate vote (slow, non-reproducible, "
                         "and look-ahead-contaminated — see module docstring)")
    args = ap.parse_args()

    if args.start:
        start = datetime.strptime(args.start, "%Y-%m-%d")
        end = datetime.strptime(args.end, "%Y-%m-%d") if args.end else datetime.now()
    else:
        end = datetime.now()
        start = end - timedelta(days=args.days)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if not os.getenv("ALPACA_API_KEY"):
        print("ALPACA_API_KEY is not set — Alpaca historical data needs it. "
              "Check your .env.", file=sys.stderr)
        return 1

    if args.warm_up < 65:
        print(f"WARNING: --warm-up {args.warm_up} is below the 60 daily bars "
              f"get_daily_trend needs; expect zero trades.", file=sys.stderr)

    S = sandbox_strategy(args.ai)
    if args.funnel:
        instrument_funnel(S)
    from lumibot.backtesting import AlpacaBacktesting
    _force_feed(args.feed)

    print("=" * 66)
    print(f"  DEBBIE-LA SMC — ALPACA INTRADAY BACKTEST")
    print(f"  {start:%Y-%m-%d} → {end:%Y-%m-%d}   |   {len(symbols)} symbols")
    print(f"  15m execution / 4H bias | risk {args.cash_at_risk:.0%} | "
          f"lev {S.PAPER_LEVERAGE}x | cap ${S.MAX_STOCK_RISK_DOLLARS:.0f}/trade")
    print(f"  AI gate: {'LIVE (look-ahead!)' if args.ai else 'bypassed (technicals only)'}")
    print("=" * 66)

    config = {
        "API_KEY":    os.getenv("ALPACA_API_KEY"),
        "API_SECRET": os.getenv("ALPACA_API_SECRET"),
        "PAPER":      True,
    }

    from lumibot.entities import TradingFee
    fees = [TradingFee(percent_fee=args.fee_pct / 100.0)] if args.fee_pct else []
    if fees:
        print(f"  modelling {args.fee_pct:.3f}%/side trading cost")

    S.DebbieLaSMC.backtest(
        AlpacaBacktesting,
        start,
        end,
        config=config,
        budget=args.budget,
        buy_trading_fees=fees,
        sell_trading_fees=fees,
        parameters={
            "symbols": symbols,
            "cash_at_risk": args.cash_at_risk,
            "timeframe_htf": "4 hours",
            "timeframe_ltf": "15 minutes",
        },
        # Minute bars are the whole point — "day" would reproduce the Yahoo problem.
        timestep="minute",
        # MUST cover the DAILY EMA50 trend filter, not just the 4H bias. get_daily_trend
        # asks for 60 daily bars; anything less makes it return None, which the trend
        # filter treats as "no confirmed trend" and vetoes EVERY setup — a backtest that
        # silently reports zero trades while the strategy is actually fine. Measured:
        # warm_up=10 yields 15 daily bars and never arms a single zone.
        warm_up_trading_days=args.warm_up,
        market="NYSE",
        show_progress_bar=True,
        show_plot=False,
        show_tearsheet=False,
        save_tearsheet=False,
        save_logfile=False,
        quiet_logs=True,
    )

    report(pair_round_trips(FILLS), start, end, symbols)
    return 0


if __name__ == "__main__":
    sys.exit(main())

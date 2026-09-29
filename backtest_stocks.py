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
            FILLS.append({
                "symbol": sym,
                "side":   str(getattr(order, "side", "")).lower(),
                "price":  float(price),
                "qty":    abs(float(quantity)),
                "dt":     self.get_datetime(),
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
            lots.append([side, qty, px, dt])
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
            })
            lot[1] -= take
            remaining -= take
            if lot[1] <= 1e-9:
                lots.popleft()
        if remaining > 1e-9:            # flipped straight through flat into a reversal
            lots.append(["buy" if side == "buy" else "sell", remaining, px, dt])
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

    print(f"  RESULTS — {len(trades)} trades   {start:%Y-%m-%d} → {end:%Y-%m-%d}")
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

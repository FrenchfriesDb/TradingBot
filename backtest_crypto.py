"""Replay-based backtester for binance_bot.py's SMC entry logic.

    python3 backtest_crypto.py                      # default: 30d, all symbols
    python3 backtest_crypto.py --days 60 --symbols BTC/USD,ETH/USD
    python3 backtest_crypto.py --days 14 --verbose  # print every trade

WHY THIS EXISTS
    The live bot only learns whether a rule works by risking money on it for weeks. This
    replays the SAME pure functions from bot/indicators.py over historical candles, so a
    change to the entry rules can be measured in seconds instead of a month of forward
    testing. Every incident this project has debugged (stale zones re-arming, fake FVGs,
    fills below the zone) would have been visible here as a drop in expectancy.

WHAT IT FAITHFULLY REPRODUCES
    • the real indicator functions — detect_displacement_bos, detect_displacement_fvg,
      detect_amd_phase, find_supply_zone / find_demand_zone, structural_stop_price,
      structural_take_profit, find_next_liquidity_target, price_in_entry_zone
    • closed-candle-only zone derivation (drop_forming_candle)
    • zone age carried across re-arms (carried_zone_age) so STALE_ZONE_BARS really bites
    • the $-risk cap (cap_qty_for_risk), stop/target geometry, and the stale-trade timeout

WHAT IT DELIBERATELY DOES NOT — read this before trusting a number
    • NO AI confirmation gate. get_ai_confirmation() calls a live LLM and cannot be
      replayed, so this measures the TECHNICAL setup quality BEFORE that filter. The live
      bot takes a subset of these trades. That is useful on purpose: if the raw structure
      logic has no edge, no filter on top will save it.
    • NO news context (same reason).
    • Fills are assumed at the signal price, and stop/target are checked against candle
      high/low. Real slippage, spread and partial fills are not modelled, so results are
      optimistic — treat the sign and shape of the edge as meaningful, not the exact $.
    • Intrabar order is unknowable at 5m: if a candle touches BOTH stop and target, the
      STOP is assumed hit first (the pessimistic, honest assumption).
"""
import argparse
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import ccxt
import pandas as pd

from bot import indicators

# Mirror the live bot's constants so the backtest can't silently drift from production.
from binance_bot import (
    TAKER_FEE_RATE,
    STALE_ZONE_BARS, MIN_AI_RR, MAX_AI_RR, MAX_RISK_DOLLARS,
    SL_ATR_MULT, MIN_STOP_ATR_MULT_HTF, SWING_LOOKBACK, STALE_TRADE_HOURS,
    DEFAULT_SYMBOLS,
    DISPLACEMENT_BODY_FRAC, DISPLACEMENT_ATR_MULT, DISPLACEMENT_MIN_PCT,
    TAP_DISPLACEMENT_ATR_MULT,
)

# Sequential drop-off tally. Added 2026-09-30 after "No trades generated" turned out to
# be the ONLY thing this tool said about a BTC sweep the operator watched happen — the
# same defect backtest_stocks.py had until 6b2da44. A zero here is a finding, but it is
# only useful if it names the gate that produced it.
FUNNEL = Counter()

# Every stage, pre-seeded to zero. Without this the stages that NEVER fire are simply
# absent from the Counter, so the table stops at the last stage that happened and the
# biggest drop in the chain — the one that matters — is invisible. That is how the first
# version of this report claimed the binding constraint was a gate that discarded nothing.
FUNNEL_STAGES = [
    "1 bars seen",
    "2 htf warm",
    "3 idle, hunting",
    "4 6h displacement BOS",
    "5 FVG agrees with BOS",
    "6 armed, waiting",
    "7 zone not expired",
    "8 price TAPS the zone",
    "9 tap reached momentum check",
    "A tap momentum passed",
    "B atr readable",
    "C risk > 0  -> ENTRY",
]

LTF_TF, LTF_SECS = "5m", 300
HTF_TF, HTF_SECS = "6h", 6 * 3600
PER_CALL_CAP = 300          # Coinbase hard-caps a single fetch_ohlcv at 300 candles


def fetch_paginated(ex, symbol, timeframe, tf_secs, since_ms, total):
    """Coinbase returns at most 300 candles per call regardless of `limit`, so walk
    forward in pages until we have the full window.

    DO NOT re-add a "short page means we are done" break (2026-09-30). Coinbase returns a
    SHORT first page the further back you ask — 280 bars at -60d on AVAX, 272 at -90d —
    and the old code treated that as end-of-data and stopped the whole walk. Symptom: a
    60-day run returned FEWER bars than a 14-day one (DOGE 1,625 taps -> 908; AVAX 1,937
    -> 0), which is impossible from market data. Every long-window crypto measurement was
    silently running on truncated history.

    Walk ends on: reaching the PRESENT, an EMPTY page, a cursor that fails to advance, or
    having `total` bars. Transient errors are retried, not fatal.

    The now-guard is not optional (added after the first fix overshot). Dropping the
    short-page break let the cursor run past the present, and Coinbase answers a future
    `start` with BadRequest "start must not be in the future" — which RAISES, killing the
    whole symbol. That silently removed DOGE, AVAX and POL from a 60-day run, leaving only
    the five majors. A walk that ends cleanly at the present cannot make that request.

    Transient retries cover ccxt.NetworkError, which is the base class of RequestTimeout,
    ExchangeNotAvailable, DDoSProtection and RateLimitExceeded — catching only the last of
    those let one timeout delete HYPE, INJ, SEI, DRIFT and ASTER from the same run.

    On exhausted retries this RAISES rather than returning short. Silent partial history is
    the failure this whole function has now produced twice; a loud one the caller reports is
    strictly better."""
    now_ms = int(time.time() * 1000)
    out, cursor = [], since_ms
    while len(out) < total and cursor < now_ms:
        batch = None
        for attempt in range(5):
            try:
                batch = ex.fetch_ohlcv(symbol, timeframe, since=cursor,
                                       limit=min(total - len(out), PER_CALL_CAP))
                break
            except ccxt.NetworkError:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
        if not batch:
            break
        out.extend(batch)
        nxt = batch[-1][0] + tf_secs * 1000
        if nxt <= cursor:
            break
        cursor = nxt
    df = pd.DataFrame(out, columns=["ts", "open", "high", "low", "close", "volume"])
    return df.drop_duplicates(subset="ts").reset_index(drop=True)


class Trade:
    __slots__ = ("symbol", "side", "entry", "stop", "target", "qty",
                 "entry_ts", "exit_ts", "exit", "reason", "pnl", "fees",
                 "partial_qty", "banked", "tp1")

    def __init__(self, symbol, side, entry, stop, target, qty, entry_ts):
        self.symbol, self.side = symbol, side
        self.entry, self.stop, self.target, self.qty = entry, stop, target, qty
        self.entry_ts, self.exit_ts, self.exit, self.reason, self.pnl = entry_ts, None, None, None, 0.0
        self.fees = 0.0
        self.partial_qty, self.banked, self.tp1 = 0.0, 0.0, None

    def take_partial(self, price, frac):
        """Bank `frac` of the position at `price`. Returns True the first time only.

        The removed live version took HALF at 1R, which clipped every winner while losers
        still took the full stop — that asymmetry is why it was deleted. This models the
        operator's version instead: a SMALLER tranche (1/3), and only when the target is
        far enough that riding the whole position to it is the doubtful part.
        """
        if self.partial_qty or frac <= 0 or frac >= 1:
            return False
        q = self.qty * frac
        gross = ((price - self.entry) if self.side == "LONG" else (self.entry - price)) * q
        self.banked = gross - (self.entry + price) * q * TAKER_FEE_RATE
        self.partial_qty, self.qty = q, self.qty - q
        return True

    def close(self, price, ts, reason):
        self.exit, self.exit_ts, self.reason = price, ts, reason
        gross = ((price - self.entry) if self.side == "LONG" else (self.entry - price)) * self.qty
        # Charge the same round-trip cost the live PaperTrader now charges, on NOTIONAL at
        # each side. Without this the backtest reports the gross number that made the live
        # strategy look profitable while it sat exactly on its 0.245%/side break-even.
        self.fees = (self.entry + price) * self.qty * TAKER_FEE_RATE
        self.pnl = gross - self.fees + self.banked
        return self


def backtest_symbol(ex, symbol, days, verbose=False):
    """Replay one symbol bar-by-bar. Returns a list of closed Trades."""
    now = datetime.now(timezone.utc)
    ltf_needed = int(days * 24 * 3600 / LTF_SECS) + 100
    htf_needed = int(days * 24 * 3600 / HTF_SECS) + 200      # +200 for indicator warmup

    since_ltf = int((now - timedelta(seconds=ltf_needed * LTF_SECS)).timestamp() * 1000)
    since_htf = int((now - timedelta(seconds=htf_needed * HTF_SECS)).timestamp() * 1000)
    ltf = fetch_paginated(ex, symbol, LTF_TF, LTF_SECS, since_ltf, ltf_needed)
    htf = fetch_paginated(ex, symbol, HTF_TF, HTF_SECS, since_htf, htf_needed)
    if len(ltf) < 100 or len(htf) < 60:
        print(f"  {symbol}: insufficient history ({len(ltf)} ltf / {len(htf)} htf) — skipped")
        return []

    trades, open_trade = [], None
    # Zone state, mirroring SymbolState's fields that actually affect entries.
    zone = None          # (lo, hi, bias)
    bars_wait = 0
    last_zone = (None, None, 0)   # (lo, hi, age) — feeds carried_zone_age across re-arms

    start_i = 60
    for i in range(start_i, len(ltf)):
        bar = ltf.iloc[i]
        ts = datetime.fromtimestamp(bar["ts"] / 1000, tz=timezone.utc)
        price = float(bar["close"])

        # ── manage an open trade first (stop/target/stale) ──────────────────────
        if open_trade:
            hi, lo = float(bar["high"]), float(bar["low"])
            hit_stop = (lo <= open_trade.stop) if open_trade.side == "LONG" else (hi >= open_trade.stop)
            hit_tgt  = (hi >= open_trade.target) if open_trade.side == "LONG" else (lo <= open_trade.target)
            # TP1 first: a bar that reaches both TP1 and the stop is assumed to have hit
            # TP1 on the way, which is the optimistic read — flagged rather than hidden,
            # and it can only FLATTER the scale-out, so a negative result is safe.
            if open_trade.tp1 is not None and not open_trade.partial_qty:
                got_tp1 = ((hi >= open_trade.tp1) if open_trade.side == "LONG"
                           else (lo <= open_trade.tp1))
                if got_tp1:
                    open_trade.take_partial(open_trade.tp1, SCALE_FRAC)
            if hit_stop:          # pessimistic: stop wins a same-bar tie
                trades.append(open_trade.close(open_trade.stop, ts, "SL")); open_trade = None
            elif hit_tgt:
                trades.append(open_trade.close(open_trade.target, ts, "TP")); open_trade = None
            elif (ts - open_trade.entry_ts).total_seconds() / 3600 >= STALE_TRADE_HOURS:
                trades.append(open_trade.close(price, ts, "STALE")); open_trade = None
            if open_trade:
                continue

        # HTF frame as the live bot sees it: only candles CLOSED by this moment.
        htf_upto = htf[htf["ts"] <= bar["ts"]]
        FUNNEL["1 bars seen"] += 1
        if len(htf_upto) < 40:
            continue
        FUNNEL["2 htf warm"] += 1
        htf_closed = indicators.drop_forming_candle(htf_upto)
        ltf_upto = ltf.iloc[max(0, i - 200):i + 1]

        # ── arm a zone (IDLE) ──────────────────────────────────────────────────
        if zone is None:
            FUNNEL["3 idle, hunting"] += 1
            is_bos, direction, _lvl = indicators.detect_displacement_bos(htf_closed, lookback=15)
            if not (is_bos and direction):
                continue
            FUNNEL["4 6h displacement BOS"] += 1
            found, d, z_lo, z_hi, _ = indicators.detect_displacement_fvg(htf_closed)
            if not (found and d == direction):
                continue
            FUNNEL["5 FVG agrees with BOS"] += 1
            bias = "BULLISH" if direction == "bullish" else "BEARISH"
            bars_wait = indicators.carried_zone_age(z_lo, z_hi, *last_zone)
            zone = (z_lo, z_hi, bias)
            continue

        # ── ENTRY_WAIT ─────────────────────────────────────────────────────────
        z_lo, z_hi, bias = zone
        bars_wait += 1
        is_long = bias == "BULLISH"

        FUNNEL["6 armed, waiting"] += 1
        if bars_wait > 48:                       # zone expired
            last_zone = (z_lo, z_hi, bars_wait); zone = None; continue
        FUNNEL["7 zone not expired"] += 1
        if not indicators.price_in_entry_zone(price, z_lo, z_hi, is_long):
            continue
        FUNNEL["8 price TAPS the zone"] += 1

        # TAP MOMENTUM — mirrors binance_bot.py:2752. This has now been wrong TWICE:
        #   1. it DISCARDED a stale zone outright (live keeps it) — fixed 2026-09-30;
        #   2. it demanded displacement only when STALE, while live (2026-10-02) demands
        #      it on EVERY tap, at TAP_DISPLACEMENT_ATR_MULT (1.4x), not the formation
        #      1.8x. Measuring the looser rule would have scored a bot that does not exist.
        # The live change came from SOL and DOGE both filling FRESH zones on 0.14x and
        # 0.36x ATR bars after three down bars each, and both stopping.
        FUNNEL["9 tap reached momentum check"] += 1
        _rng = ltf_upto["high"] - ltf_upto["low"]
        _atr_t = float(_rng.rolling(14).mean().iloc[-1])
        _px_t  = float(ltf_upto["close"].iloc[-1])
        if pd.isna(_atr_t) or not indicators.has_displacement(
                ltf_upto.tail(3)[["open", "high", "low", "close"]].values.tolist(),
                is_long, min_body_frac=DISPLACEMENT_BODY_FRAC,
                min_body_abs=indicators.displacement_min_body(
                    _atr_t, _px_t, TAP_DISPLACEMENT_ATR_MULT, DISPLACEMENT_MIN_PCT)):
            continue              # keep waiting — do NOT discard the zone
        FUNNEL["A tap momentum passed"] += 1

        # ── structural stop / target, same helpers as live ─────────────────────
        rng5 = ltf_upto["high"] - ltf_upto["low"]
        atr5 = float(rng5.rolling(14).mean().iloc[-1])
        rng6 = htf_closed["high"] - htf_closed["low"]
        atr6 = float(rng6.rolling(14).mean().iloc[-1])
        if not atr5 or not atr6 or pd.isna(atr5) or pd.isna(atr6):
            continue
        FUNNEL["B atr readable"] += 1
        swing = (float(ltf_upto["low"].tail(SWING_LOOKBACK).min()) * 0.999 if is_long
                 else float(ltf_upto["high"].tail(SWING_LOOKBACK).max()) * 1.001)
        zone_lvl = indicators.crypto_zone_stop_level(
            price, is_long, z_lo if is_long else z_hi, SL_ATR_MULT * atr5, swing)
        stop = indicators.structural_stop_price(price, zone_lvl, atr6, is_long, MIN_STOP_ATR_MULT_HTF)
        risk = abs(price - stop)
        if risk <= 0:
            last_zone = (z_lo, z_hi, bars_wait); zone = None; continue
        FUNNEL["C risk > 0  -> ENTRY"] += 1
        pool = indicators.find_next_liquidity_target(
            htf_closed, price + MIN_AI_RR * risk if is_long else price - MIN_AI_RR * risk,
            "bullish" if is_long else "bearish")
        target = indicators.structural_take_profit(price, risk, pool, is_long, MIN_AI_RR, MAX_AI_RR)
        _tp1 = None
        if SCALE_FRAC > 0 and risk > 0:
            _rr = abs(target - price) / risk
            if _rr >= SCALE_MIN_RR:            # only when the target is genuinely far
                _tp1 = price + (target - price) * SCALE_TP1_FRAC
        qty = indicators.cap_qty_for_risk(MAX_RISK_DOLLARS / risk, risk, MAX_RISK_DOLLARS)

        open_trade = Trade(symbol, "LONG" if is_long else "SHORT", price, stop, target, qty, ts)
        open_trade.tp1 = _tp1
        if verbose:
            print(f"    {ts:%m-%d %H:%M} {symbol:9} {open_trade.side:5} @ {price:>11,.4f} "
                  f"SL {stop:>11,.4f} TP {target:>11,.4f} (age {bars_wait}b)")
        last_zone = (z_lo, z_hi, bars_wait)
        zone = None

    return trades


def report_funnel():
    """Where the candles died. A sequential chain, so each row is a subset of the one above
    and the big drop between two rows IS the binding constraint."""
    if not FUNNEL:
        return
    rows = [(nm, FUNNEL.get(nm, 0)) for nm in FUNNEL_STAGES]

    # Two phases, and they do NOT nest. Stages 1-5 count bars while IDLE (hunting for a
    # zone); stages 6-B count bars while a zone is ALREADY ARMED, which is a different
    # population — one armed zone contributes many waiting bars. Printing them as one
    # chain produced a "-54.1% lost" row, i.e. a drop-off table reporting a gain. Split
    # them, and compute each phase's drops only within itself.
    def _phase(title, chunk, unit):
        if not chunk:
            return None
        print(f"\n  {title}   ({unit})")
        top = chunk[0][1] or 1
        prev = None
        for name, n in chunk:
            lost = ""
            if prev is not None:
                pct = (1 - n / prev) * 100 if prev else 0.0
                lost = f"  -{prev - n:,} ({pct:5.1f}% lost)"
            print(f"    {name[2:]:<26} {n:>8,}  {n/top*100:5.1f}%{lost}")
            prev = n
        # Rank by SHARE lost, not raw count. A stage that takes the funnel to zero is the
        # binding one even if an earlier stage discarded more bars in absolute terms —
        # ranking by count reported a 42.9% gate over one losing 100%.
        drops = [((chunk[i-1][1] - chunk[i][1]) / chunk[i-1][1],
                  chunk[i-1][1] - chunk[i][1], chunk[i][0][2:], chunk[i-1][1])
                 for i in range(1, len(chunk)) if chunk[i-1][1] > 0]
        if not drops:
            return None
        share, lost, name, reaching = max(drops)
        return (lost, name, reaching, share)

    print("\n" + "=" * 62)
    print("  FUNNEL — where the bars stopped")
    print("=" * 62)
    w1 = _phase("ARMING", rows[:5], "bars while idle, hunting for a zone")
    w2 = _phase("ENTRY",  rows[5:], "bars while a zone is armed — NOT a subset of above")
    for w in (x for x in (w1, w2) if x and x[0]):
        lost, name, reaching, share = w
        tag = "  ← TAKES THE FUNNEL TO ZERO" if share >= 1.0 else ""
        print(f"\n  BINDING CONSTRAINT: '{name}' — discards {lost:,} of the {reaching:,} "
              f"bars reaching it ({share*100:.1f}%).{tag}")
    print("\n  CAVEAT: this replays ONE of the live bot's arming paths (displacement BOS +\n"
          "  agreeing FVG). binance_bot.py has 11. A zero here does NOT mean the live bot\n"
          "  would have stood aside — it means THIS path would have.")


def report(all_trades, days):
    if not all_trades:
        print("\nNo trades generated — the entry rules never triggered over this window.")
        print("That is a RESULT, not a failure: rules this selective may simply be rare.")
        report_funnel()
        return
    pnls = [t.pnl for t in all_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw, gl = sum(wins), abs(sum(losses))
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    print("\n" + "=" * 62)
    print(f"  RESULTS — {len(all_trades)} trades over {days} days")
    print("=" * 62)
    print(f"  net P&L         {sum(pnls):+,.2f}")
    print(f"  expectancy      {sum(pnls)/len(pnls):+,.2f} per trade   <-- the number that matters")
    print(f"  win rate        {len(wins)/len(pnls)*100:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  avg win         {gw/len(wins):+,.2f}" if wins else "  avg win         n/a")
    print(f"  avg loss        {-gl/len(losses):+,.2f}" if losses else "  avg loss        n/a")
    print(f"  profit factor   {gw/gl:.2f}" if gl else "  profit factor   inf")
    print(f"  max drawdown    {max_dd:,.2f}")
    tf = sum(getattr(t, "fees", 0.0) for t in all_trades)
    print(f"  fees paid       {tf:,.2f}  (at {TAKER_FEE_RATE*100:.2f}%/side)   "
          f"gross would be {sum(pnls)+tf:+,.2f}")
    by_reason = {}
    for t in all_trades:
        by_reason.setdefault(t.reason, []).append(t.pnl)
    print("  exits:          " + "  ".join(
        f"{k}={len(v)} ({sum(v):+,.0f})" for k, v in sorted(by_reason.items())))
    # ── PER-SYMBOL AND PER-MONTH, in R ────────────────────────────────────────────────
    # Added 2026-10-03, mirroring backtest_stocks. The stock side showed a 64% win rate
    # whose profit was 99% two symbols, with the most recent third of the window negative
    # — neither visible from net P&L. Same two questions here: is the edge broad, and is
    # it still working? R, not dollars, because $-risk per trade varies with sizing.
    def _r(t):
        risk = abs(t.entry - t.stop)
        return (t.pnl / (risk * t.qty)) if risk and t.qty else None

    def _bucket(title, keyfn, order=None):
        b = {}
        for t in all_trades:
            r = _r(t)
            if r is None:
                continue
            b.setdefault(keyfn(t), []).append((r, t.pnl))
        if not b:
            return
        keys = order(b) if order else sorted(b)
        print(f"\n  {title}")
        print(f"    {'key':<10} {'n':>4} {'win%':>6} {'exp R':>8} {'tot R':>8} {'net $':>9}")
        for k in keys:
            v = b[k]
            w = sum(1 for r, _ in v if r > 0)
            e = sum(r for r, _ in v) / len(v)
            print(f"    {str(k):<10} {len(v):>4} {w/len(v)*100:>5.0f}% {e:>+8.2f} "
                  f"{e*len(v):>+8.1f} {sum(x for _, x in v):>+9.2f}")

    _bucket("PER SYMBOL — is the edge broad?", lambda t: t.symbol,
            order=lambda b: sorted(b, key=lambda k: -sum(r for r, _ in b[k])))
    _bucket("PER MONTH — is it still working?",
            lambda t: t.entry_ts.strftime("%Y-%m") if t.entry_ts else "?")

    print("\n  Reminder: no AI gate, no news, no slippage (fees ARE modelled) — this is")
    print("  before filtering, and it is optimistic. Judge the sign, not the cents.")
    report_funnel()


# Scale-out policy under test. 0 = the live behaviour (full position rides to target).
SCALE_FRAC    = float(os.getenv("SCALE_FRAC", "0"))       # 0.333 = take a third at TP1
SCALE_TP1_FRAC = float(os.getenv("SCALE_TP1_FRAC", "0.5"))  # TP1 at half the way to TP
SCALE_MIN_RR  = float(os.getenv("SCALE_MIN_RR", "3.0"))     # "far" target only


def main():
    ap = argparse.ArgumentParser(description="Backtest binance_bot's SMC entry logic.")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbols", type=str, default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--verbose", action="store_true", help="print each trade as it opens")
    args = ap.parse_args()

    import ccxt
    ex = ccxt.coinbase({"enableRateLimit": True})
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    print("=" * 62)
    print(f"  BINANCE_BOT SMC BACKTEST — {args.days}d — {len(symbols)} symbols")
    print(f"  risk/trade ${MAX_RISK_DOLLARS:.0f} | R:R {MIN_AI_RR}-{MAX_AI_RR} | "
          f"stale zone {STALE_ZONE_BARS}b | stale trade {STALE_TRADE_HOURS}h")
    print("=" * 62)

    all_trades = []
    for sym in symbols:
        try:
            t = backtest_symbol(ex, sym, args.days, args.verbose)
            print(f"  {sym:10} {len(t):3} trades  {sum(x.pnl for x in t):+9,.2f}")
            all_trades.extend(t)
        except Exception as e:
            print(f"  {sym:10} ERROR: {type(e).__name__}: {e}")
    report(all_trades, args.days)


if __name__ == "__main__":
    sys.exit(main())

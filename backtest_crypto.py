"""Replay-based backtester for binance_bot.py's SMC entry logic.

CORRECTION 2026-10-03, read before trusting commit e43f738. That commit added "Route B"
(trend-follow demand/supply), measured 239 trades with all 13 symbols negative, and
concluded the LIVE bot runs it and that this explained the live losses. THAT CONCLUSION IS
WRONG. binance_bot.py:368 sets ENABLE_TREND_FOLLOW = False — the path is gated OFF in
production and has been, with a comment naming the same failure the backtest found ("the
dip-buy in consolidation that bleeds to the 6h timer"). Route B's 239 trades describe a
code path nobody is running.

What that measurement IS good for: it independently confirms the earlier decision to gate
that path was correct, on 239 trades rather than intuition. Keep it behind a flag here too.

LIVE REALITY, which also corrects the "we only replay 1 of 11 arming paths" claim repeated
throughout 2026-10-03. There are 9 real arming sites (two of the eleven amd_zone_type
assignments are a state restore and a reset). Of those 9, FIVE ARE DELIBERATELY OFF:
ENABLE_TREND_FOLLOW gates 4, ENABLE_WEDGE_BREAKOUT gates 1. The bot runs in "pure-sniper
mode" — the AMD liquidity-sweep engine and the BOS displacement retest only.

So the crypto bot's low trade count is NOT an artifact of under-measurement. It is the
design. The remaining replay gap is the AMD sweep-gated demand/supply path
(binance_bot.py:2208 and :2231), which is the priority engine and the one worth building
next — not "the other ten paths".


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
import numpy as np
import pandas as pd

from bot import indicators

# Mirror the live bot's constants so the backtest can't silently drift from production.
from binance_bot import (
    TAKER_FEE_RATE,
    STALE_ZONE_BARS, MIN_AI_RR, MAX_AI_RR, MAX_RISK_DOLLARS,
    SL_ATR_MULT, MIN_STOP_ATR_MULT_HTF, SWING_LOOKBACK, STALE_TRADE_HOURS,
    DEFAULT_SYMBOLS,
    DISPLACEMENT_BODY_FRAC, DISPLACEMENT_ATR_MULT, DISPLACEMENT_MIN_PCT,
    TAP_DISPLACEMENT_ATR_MULT, atr_gate_for,
    ENABLE_TREND_FOLLOW, MAX_TARGET_ATR_MULT,
    TARGET_NEAREST_POOL, POOL_MIN_RR, MIN_TRADE_RR, STOP_SLIPPAGE_R,
    max_target_atr_mult_for, amd_distribution_confirmed, AMD_REQUIRE_DISPLACEMENT,
)

# Sequential drop-off tally. Added 2026-09-30 after "No trades generated" turned out to
# be the ONLY thing this tool said about a BTC sweep the operator watched happen — the
# same defect backtest_stocks.py had until 6b2da44. A zero here is a finding, but it is
# only useful if it names the gate that produced it.
FUNNEL = Counter()
ARMED_BY = Counter()        # which arming route produced each armed zone

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
    "5b trend-follow zone",
    "5c AMD sweep zone",
    "6 armed, waiting",
    "7 zone not expired",
    "8 price TAPS the zone",
    "9 tap reached momentum check",
    "A tap momentum passed",
    "B atr readable",
    "C risk > 0",
    "E target reachable -> TRADE",
]

# MEASUREMENT-ONLY override. Defaults to the LIVE flag, so a plain run replays exactly
# what production arms. Set REPLAY_TREND_FOLLOW=1 only to widen the sample for a
# structural question (e.g. "does 6H crypto travel 2R in 18h?"); a result from that run
# is NOT a statement about the live bot.
_REPLAY_TREND_FOLLOW = (os.getenv("REPLAY_TREND_FOLLOW") == "1") or ENABLE_TREND_FOLLOW

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
                 "partial_qty", "banked", "tp1", "mfe_r", "mae_r", "hrs_to_mfe",
                 "sl_overshoot_r", "struct_dist", "floor_dist")

    def __init__(self, symbol, side, entry, stop, target, qty, entry_ts):
        self.symbol, self.side = symbol, side
        self.entry, self.stop, self.target, self.qty = entry, stop, target, qty
        self.entry_ts, self.exit_ts, self.exit, self.reason, self.pnl = entry_ts, None, None, None, 0.0
        self.fees = 0.0
        self.partial_qty, self.banked, self.tp1 = 0.0, 0.0, None
        # Excursion tracking, in units of R (R = |entry - stop|). mfe_r answers the
        # question "how far did this setup EVER go our way before it died", which is
        # what distinguishes "the target was too far" from "the structure picked the
        # wrong direction". Updated only on bars that did NOT hit the stop, so a
        # favourable wick on the stop bar can never inflate it.
        self.mfe_r, self.mae_r, self.hrs_to_mfe = 0.0, 0.0, 0.0
        # How far PAST the stop the bar actually traded, in R. Both this replay and the
        # live paper trader (binance_bot.py:3477) fill a stop-out AT the stop price even
        # though they detect the hit by seeing the candle trade THROUGH it. A stop is a
        # market exit: it fills at-or-worse. This measures the size of that fiction.
        self.sl_overshoot_r = 0.0
        # WHICH ANCHOR SET THE STOP. structural_stop_price takes the WIDER of the
        # structural invalidation and the ATR noise floor. If the floor wins almost always,
        # the stop is a volatility multiple wearing a structure label — and because the
        # target is then placed at N x that inflated risk, BOTH legs get stretched by the
        # same error. The operator spotted this on two live trades: AERO's zone-edge stop
        # was 1.18% and the actual stop 3.56% (3x), TAO 0.22% vs 1.99% (9x).
        self.struct_dist, self.floor_dist = 0.0, 0.0

    @property
    def risk_per_unit(self):
        return abs(self.entry - self.stop)

    @property
    def target_r(self):
        r = self.risk_per_unit
        return abs(self.target - self.entry) / r if r else 0.0

    def track_excursion(self, hi, lo, ts):
        r = self.risk_per_unit
        if not r:
            return
        fav = ((hi - self.entry) if self.side == "LONG" else (self.entry - lo)) / r
        adv = ((self.entry - lo) if self.side == "LONG" else (hi - self.entry)) / r
        if fav > self.mfe_r:
            self.mfe_r = fav
            self.hrs_to_mfe = (ts - self.entry_ts).total_seconds() / 3600
        self.mae_r = max(self.mae_r, adv)

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
    # DAILY bars for get_daily_trend — needed by the TREND-FOLLOW arming route, which this
    # backtest did not replay at all until 2026-10-03.
    since_day = int((now - timedelta(days=int(days) + 120)).timestamp() * 1000)
    day = fetch_paginated(ex, symbol, "1d", 86400, since_day, int(days) + 120)
    # 1H bars. binance_bot.py:2594 and :2863 floor the structural stop against the 1H
    # ATR (`atr_1h or _atr_s`). This replay floored it against the 6H ATR instead, which
    # made every stop ~2.4x wider than live, every 2R target ~2.4x further away, and
    # turned the 18h timer into the only reachable exit. Found 2026-10-03 while asking
    # why no setup ever resolved: the answer was partly that this file never replayed the
    # bot's actual stop.
    h1_needed = int(days * 24) + 200
    since_h1 = int((now - timedelta(hours=h1_needed)).timestamp() * 1000)
    h1 = fetch_paginated(ex, symbol, "1h", 3600, since_h1, h1_needed)
    if len(ltf) < 100 or len(htf) < 60:
        print(f"  {symbol}: insufficient history ({len(ltf)} ltf / {len(htf)} htf) — skipped")
        return []

    # Rolling-14 1H ATR as a flat array + its open-times, so each 5m bar can look up the
    # last FULLY CLOSED 1H candle in O(log n) — the frame live sees after
    # drop_forming_candle. A 1H candle with open time T is closed once T+3600s <= now.
    _h1_ts = h1["ts"].to_numpy() if len(h1) else np.empty(0, dtype="int64")
    _h1_atr = ((h1["high"] - h1["low"]).rolling(14).mean().to_numpy()
               if len(h1) else np.empty(0))

    def atr_1h_at(bar_ts):
        k = int(np.searchsorted(_h1_ts, bar_ts - 3_600_000, side="right")) - 1
        if k < 14:
            return None
        v = _h1_atr[k]
        return float(v) if v == v and v > 0 else None

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
            if not hit_stop:      # pessimistic: a stop bar contributes no favourable R
                open_trade.track_excursion(hi, lo, ts)
            if hit_stop:          # pessimistic: stop wins a same-bar tie
                _r = open_trade.risk_per_unit
                if _r:
                    _past = ((open_trade.stop - lo) if open_trade.side == "LONG"
                             else (hi - open_trade.stop))
                    open_trade.sl_overshoot_r = max(0.0, _past / _r)
                # Same honest stop fill the live watcher now uses.
                _sl_fill = indicators.stop_fill_price(
                    open_trade.entry, open_trade.stop,
                    open_trade.side == "LONG", STOP_SLIPPAGE_R)
                trades.append(open_trade.close(_sl_fill, ts, "SL")); open_trade = None
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
            # TWO arming routes, not one. binance_bot has 9 real arming sites built from
            # three detector families: displacement-FVG (4 sites), demand/supply zones
            # (4 sites) and a wedge retest (1). This replayed ONLY the displacement-FVG
            # family until 2026-10-03, which is why nine of thirteen symbols showed zero
            # trades and every crypto frequency number described a fraction of the bot.
            armed_by = None
            # Route A — displacement BOS + an agreeing FVG (what this always did).
            is_bos, direction, _lvl = indicators.detect_displacement_bos(htf_closed, lookback=15)
            if is_bos and direction:
                FUNNEL["4 6h displacement BOS"] += 1
                found, d, z_lo, z_hi, _ = indicators.detect_displacement_fvg(htf_closed)
                if found and d == direction:
                    FUNNEL["5 FVG agrees with BOS"] += 1
                    armed_by = "fvg"
            # Route B — TREND-FOLLOW demand/supply (binance_bot.py:2302).
            # GATED ON THE LIVE FLAG. ENABLE_TREND_FOLLOW is False in production, and the
            # first run with Route B unconditional armed 19,059 zones through it against
            # 402 for the AMD priority engine — so the measured population was almost
            # entirely a path nobody runs, and Route C only ever saw leftovers. Mirroring
            # the live flag is the only way this file measures the live bot.
            if armed_by is None and _REPLAY_TREND_FOLLOW:
                dly = day[day["ts"] <= bar["ts"]]
                dtrend = indicators.get_daily_trend(dly) if len(dly) >= 52 else None
                if dtrend == "bullish":
                    f2, z_lo, z_hi, _k = indicators.find_demand_zone(
                        htf_closed, price, max_distance_pct=0.08)
                    if f2:
                        direction, armed_by = "bullish", "trend"
                elif dtrend == "bearish":
                    f2, z_lo, z_hi, _k = indicators.find_supply_zone(
                        htf_closed, price, max_distance_pct=0.08)
                    if f2:
                        direction, armed_by = "bearish", "trend"
                if armed_by == "trend":
                    FUNNEL["5b trend-follow zone"] += 1
            # ── Route C: AMD sweep-gated demand/supply (binance_bot.py:2208 / :2231) ──
            # THE PRIORITY ENGINE and the live bot's main path — pure-sniper mode leaves
            # only this and Route A enabled. Mirrors the live gates exactly:
            #   HTF high-sweep + daily BULLISH + depth >= 0.3% -> demand zone below
            #   HTF low-sweep  + daily BEARISH + depth >= 0.3% -> supply zone above
            # Also applies the live per-symbol ATR floor (atr_gate_for), which Routes A
            # and B in this file never did.
            if armed_by is None:
                dly2 = day[day["ts"] <= bar["ts"]]
                dt2 = indicators.get_daily_trend(dly2) if len(dly2) >= 52 else None
                _rng6 = htf_closed["high"] - htf_closed["low"]
                _atr6 = float(_rng6.rolling(14).mean().iloc[-1])
                _px6 = float(htf_closed["close"].iloc[-1])
                _atrp = (_atr6 / _px6) if (_atr6 == _atr6 and _px6) else 0.0
                if _atrp >= atr_gate_for(symbol):
                    hi_sw, res_lvl, hi_wick = indicators.check_liquidity_sweep_high(
                        htf_closed, sweep_window=5)
                    lo_sw, sup_lvl, lo_wick = indicators.check_liquidity_sweep(
                        htf_closed, sweep_window=5)
                    if hi_sw and dt2 == "bullish" and res_lvl and res_lvl > 0 and hi_wick:
                        if (hi_wick - res_lvl) / res_lvl >= 0.003:
                            # AMD's DISTRIBUTION leg (binance_bot.py). A sweep is
                            # manipulation; without a displacement out of it there is no
                            # setup. Added live 2026-10-05 and mirrored here the same day —
                            # a gate that runs in production and not in the replay is how
                            # this file has drifted from live three times already.
                            if amd_distribution_confirmed(htf_closed, True)[0]:
                                f3, z_lo, z_hi, _k = indicators.find_demand_zone(htf_closed, price)
                                if f3:
                                    direction, armed_by = "bullish", "amd"
                            else:
                                FUNNEL["5d AMD refused — no distribution"] += 1
                    elif lo_sw and dt2 == "bearish" and sup_lvl and sup_lvl > 0 and lo_wick:
                        if (sup_lvl - lo_wick) / sup_lvl >= 0.003:
                            if not amd_distribution_confirmed(htf_closed, False)[0]:
                                FUNNEL["5d AMD refused — no distribution"] += 1
                                f3 = False
                            else:
                                f3, z_lo, z_hi, _k = indicators.find_supply_zone(htf_closed, price)
                            if f3:
                                direction, armed_by = "bearish", "amd"
                if armed_by == "amd":
                    FUNNEL["5c AMD sweep zone"] += 1
            if armed_by is None:
                continue
            ARMED_BY[armed_by] += 1
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
        _atr_1h = atr_1h_at(int(bar["ts"]))
        _atr_1h_ref = _atr_1h or atr5
        _struct_d = abs(price - zone_lvl) if zone_lvl else 0.0
        _floor_d  = MIN_STOP_ATR_MULT_HTF * _atr_1h_ref
        stop = indicators.structural_stop_price(
            price, zone_lvl, _atr_1h_ref, is_long, MIN_STOP_ATR_MULT_HTF)
        risk = abs(price - stop)
        if risk <= 0:
            last_zone = (z_lo, z_hi, bars_wait); zone = None; continue
        FUNNEL["C risk > 0"] += 1
        # Aim AT the nearest pool, not past it — mirrors binance_bot.py:1652 and :2627.
        _tp_floor = POOL_MIN_RR if TARGET_NEAREST_POOL else MIN_AI_RR
        pool = indicators.find_next_liquidity_target(
            htf_closed, price + _tp_floor * risk if is_long else price - _tp_floor * risk,
            "bullish" if is_long else "bearish")
        target = indicators.structural_take_profit(
            price, risk, pool, is_long, MIN_AI_RR, MAX_AI_RR, pool_min_rr=_tp_floor)
        # binance_bot.py:1688 CLAMPS the target to what the hold window can deliver and
        # SKIPS the trade when the honest target no longer pays MIN_AI_RR. This replay
        # had no such gate, so it opened trades live refuses and then scored them dying
        # on the timer — which is exactly the "setups never resolve" symptom.
        target, _rr_reach, _reach_ok = indicators.reachable_target(
            price, price - risk if is_long else price + risk, target,
            indicators.range_atr(htf_closed), max_target_atr_mult_for(HTF_TF),
            MIN_TRADE_RR)
        if not _reach_ok:
            FUNNEL["D reachability veto (live skips)"] += 1
            last_zone = (z_lo, z_hi, bars_wait); zone = None; continue
        FUNNEL["E target reachable -> TRADE"] += 1
        _tp1 = None
        if SCALE_FRAC > 0 and risk > 0:
            _rr = abs(target - price) / risk
            if _rr >= SCALE_MIN_RR:            # only when the target is genuinely far
                _tp1 = price + (target - price) * SCALE_TP1_FRAC
        qty = indicators.cap_qty_for_risk(MAX_RISK_DOLLARS / risk, risk, MAX_RISK_DOLLARS)

        open_trade = Trade(symbol, "LONG" if is_long else "SHORT", price, stop, target, qty, ts)
        open_trade.struct_dist, open_trade.floor_dist = _struct_d, _floor_d
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
            print(f"    {name[2:].strip():<26} {n:>8,}  {n/top*100:5.1f}%{lost}")
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
    # THREE groups, not two. The previous split put "5b trend-follow zone" at the head of
    # the ENTRY chunk, and that row is 0 whenever the path is gated off — so `top` fell
    # back to 1 and every row below it printed as 410800%. Worse, 5b/5c count ZONES while
    # 6..E count BARS, so they could never share a denominator at all. Zone counts are
    # now printed as counts, and ENTRY is based on the armed-bar population.
    w1 = _phase("ARMING", rows[:5], "bars while idle, hunting for a zone")
    print("\n  ZONES ARMED   (count of zones, by route — not bars)")
    for name, n in rows[5:7]:
        print(f"    {name[2:].strip():<26} {n:>8,}")
    w2 = _phase("ENTRY",  rows[7:], "bars while a zone is armed — NOT a subset of above")
    for w in (x for x in (w1, w2) if x and x[0]):
        lost, name, reaching, share = w
        tag = "  ← TAKES THE FUNNEL TO ZERO" if share >= 1.0 else ""
        print(f"\n  BINDING CONSTRAINT: '{name}' — discards {lost:,} of the {reaching:,} "
              f"bars reaching it ({share*100:.1f}%).{tag}")
    print("\n  CAVEAT: this replays route A (displacement BOS + agreeing FVG) and route C\n"
          "  (AMD sweep) — the two binance_bot.py actually arms on — plus route B behind\n"
          "  REPLAY_TREND_FOLLOW=1. Live has 9 arming sites; 5 are gated OFF by\n"
          "  ENABLE_TREND_FOLLOW / ENABLE_WEDGE_BREAKOUT. The wedge-RETEST path is still\n"
          "  unreplayed, so a zero here is strong evidence but not proof of a stand-aside.")


def report_resolution(all_trades):
    """Why don't setups resolve? Compare how far price actually travelled our way
    (MFE, in R) against how far the target was asked to be.

    The counterfactual win rates below are a FIRST-ORDER estimate: a trade whose MFE
    reached X R would have paid X R at a target of X R. It does not model the knock-on
    effect that exiting sooner frees the one-position slot earlier, which would add
    trades — so treat the trade COUNT as fixed and the direction of the result as the
    signal, not the exact dollars.
    """
    ts = [t for t in all_trades if t.risk_per_unit]
    if not ts:
        return
    import statistics as st
    mfe = sorted(t.mfe_r for t in ts)
    tgt = sorted(t.target_r for t in ts)

    def pct(xs, q):
        return xs[min(len(xs) - 1, int(q * len(xs)))]

    print("\n" + "=" * 62)
    print("  WHY SETUPS DON'T RESOLVE — travel vs. target")
    print("=" * 62)
    # SIGNIFICANCE, printed next to the expectancy it qualifies. This project has
    # repeatedly read a thin positive sample as an edge: a +0.19R figure from a 4,406-tap
    # proxy got quoted back as the stock bot's measured edge, and a 64% win rate over 45
    # trades turned out to be two symbols. An expectancy without its standard error is
    # not a result, so the tool now refuses to print one.
    _rs = [t.pnl / (t.risk_per_unit * t.qty) for t in ts if t.qty and t.risk_per_unit]
    if len(_rs) > 2:
        _mean = st.mean(_rs)
        _se = st.stdev(_rs) / (len(_rs) ** 0.5)
        _t = _mean / _se if _se else 0.0
        # n needed for |t| >= 2 at the observed mean and spread
        _need = int((2 * st.stdev(_rs) / abs(_mean)) ** 2) + 1 if _mean else 0
        print(f"  expectancy   {_mean:+.3f} R  +/- {_se:.3f} (1 s.e.)   t = {_t:+.2f}")
        print(f"  verdict      {'DISTINGUISHABLE from zero' if abs(_t) >= 2 else 'INDISTINGUISHABLE from zero'}"
              + (f" — would need n ~ {_need:,} at this mean/spread" if abs(_t) < 2 and _need else ""))
    print(f"  trades measured            {len(ts)}")
    print(f"  target asked (median)      {st.median(tgt):.2f} R"
          f"   [p10 {pct(tgt,.1):.2f}  p90 {pct(tgt,.9):.2f}]")
    print(f"  best travel reached        {st.median(mfe):.2f} R"
          f"   [p10 {pct(mfe,.1):.2f}  p90 {pct(mfe,.9):.2f}  max {mfe[-1]:.2f}]")
    print(f"  median hours to that peak  {st.median([t.hrs_to_mfe for t in ts]):.1f}h"
          f"   (timer fires at {STALE_TRADE_HOURS}h)")
    print(f"  median adverse excursion   {st.median([t.mae_r for t in ts]):.2f} R")
    _an = [t for t in ts if t.floor_dist]
    if _an:
        _floor_won = [t for t in _an if t.floor_dist >= t.struct_dist]
        _infl = [t.floor_dist / t.struct_dist for t in _an if t.struct_dist > 0]
        print(f"\n  WHAT SET THE STOP — structure, or the ATR floor?")
        print(f"    ATR floor won             {len(_floor_won)}/{len(_an)} "
              f"({len(_floor_won)/len(_an):.0%})")
        if _infl:
            _infl.sort()
            print(f"    inflation over structure  {st.median(_infl):.1f}x median "
                  f"[p90 {_infl[min(len(_infl)-1, int(.9*len(_infl)))]:.1f}x  "
                  f"max {_infl[-1]:.1f}x]")
        print(f"    median stop as % of price {st.median([abs(t.entry-t.stop)/t.entry*100 for t in _an]):.2f}%")
    _sl = [t for t in ts if t.reason == "SL"]
    if _sl:
        _ov = sorted(t.sl_overshoot_r for t in _sl)
        _mean_ov = sum(_ov) / len(_ov)
        print(f"  stop-outs                  {len(_sl)}")
        print(f"  traded PAST the stop by    {st.median(_ov):.2f} R median "
              f"[p90 {_ov[min(len(_ov)-1, int(.9*len(_ov)))]:.2f}  max {_ov[-1]:.2f}]")
        print(f"  cost of the AT-STOP fill   {_mean_ov * len(_sl) / len(ts):.3f} R per trade "
              f"if a stop filled at the bar's extreme instead")
    _veto = FUNNEL.get("D reachability veto (live skips)", 0)
    if _veto:
        print(f"  setups live REFUSES        {_veto:,} (target unreachable inside "
              f"{STALE_TRADE_HOURS}h) — this replay now refuses them too")

    print("\n  HOW FAR DID PRICE GET? (share of trades whose peak reached each level)")
    print(f"    {'level':>7} {'reached':>9} {'share':>7}")
    for lvl in (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0):
        n = sum(1 for t in ts if t.mfe_r >= lvl)
        print(f"    {lvl:>6.2f}R {n:>9} {n / len(ts):>6.0%}")

    print("\n  COUNTERFACTUAL — if the target had been fixed at X R")
    print("  (fees charged at the SAME rate; trades that never reached X keep their real exit)")
    print(f"    {'target':>7} {'win%':>6} {'exp R':>8} {'tot R':>8} {'net $':>10}")
    for lvl in (0.5, 0.75, 1.0, 1.5, 2.0):
        rs, net = [], 0.0
        for t in ts:
            if t.mfe_r >= lvl:
                rs.append(lvl)
                gross = lvl * t.risk_per_unit * t.qty
                fee = (t.entry + (t.entry + (lvl if t.side == "LONG" else -lvl)
                                  * t.risk_per_unit)) * t.qty * TAKER_FEE_RATE
                net += gross - fee
            else:
                rs.append(t.pnl / (t.risk_per_unit * t.qty) if t.qty else 0.0)
                net += t.pnl
        wins = sum(1 for r in rs if r > 0)
        print(f"    {lvl:>6.2f}R {wins / len(rs):>5.0%} {sum(rs) / len(rs):>8.2f} "
              f"{sum(rs):>8.1f} {net:>10.2f}")


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

    if ARMED_BY:
        print("\n  ARMED BY ROUTE (zones armed, not trades):")
        for k, n in ARMED_BY.most_common():
            label = {"fvg": "A  displacement BOS + FVG", "trend": "B  trend-follow (OFF live)",
                     "amd": "C  AMD sweep (PRIORITY, live)"}.get(k, k)
            print(f"    {label:<34} {n:>7,}")

    _bucket("PER SYMBOL — is the edge broad?", lambda t: t.symbol,
            order=lambda b: sorted(b, key=lambda k: -sum(r for r, _ in b[k])))
    _bucket("PER MONTH — is it still working?",
            lambda t: t.entry_ts.strftime("%Y-%m") if t.entry_ts else "?")

    print("\n  Reminder: no AI gate, no news, no slippage (fees ARE modelled) — this is")
    print("  before filtering, and it is optimistic. Judge the sign, not the cents.")
    report_resolution(all_trades)
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
    print(f"  risk/trade ${MAX_RISK_DOLLARS:.0f} | "
          f"target {'nearest pool' if TARGET_NEAREST_POOL else 'floored'} "
          f"{MIN_TRADE_RR}-{MAX_AI_RR}R | "
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

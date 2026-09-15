from lumibot.strategies import Strategy
from lumibot.entities import Asset, Order
from bot import indicators
from bot import ai_model as _ai_model
from finbert_utils import estimate_sentiment
from config import (NVIDIA_API_KEY, API_KEY as ALPACA_API_KEY, API_SECRET as ALPACA_API_SECRET,
                    BASE_URL as ALPACA_BASE_URL, GOOGLE_SHEET_URL)
from sheets_logger import (get_sheet_client, ensure_tabs, log_daily_snapshot, log_trade,
                            missing_snapshot_dates, MACRO_HEADER, LEDGER_HEADER)
from chart_renderer import render_trade_chart, render_stock_tradingview, save_chart_locally
from github_chart_uploader import upload_chart_to_github
import json
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

STRATEGY_STATE_FILE = "strategy_state.json"
LOGGED_CLOSES_FILE  = "stock_logged_closes.json"  # dedup keys for broker-side closes (survives restarts)

_ET = ZoneInfo("America/New_York")   # US equities session clock
# No NEW entries inside this many minutes of the 4:00 PM ET close. A position opened
# at 3:59 PM can't be flattened by before_closing_bell in time and rides overnight
# straight into a gap (exactly how QQQ got held) — so we simply don't open that late.
ENTRY_CUTOFF_MIN = 30
# Force-flatten any open position inside this window of the close. This runs from the
# main loop (which we KNOW executes every iteration), not only Lumibot's
# before_closing_bell hook — so an EOD flatten no longer depends on that single hook
# firing at exactly the right minute. (If the process itself is asleep at the close,
# nothing can run — keep the bot awake, e.g. `caffeinate -i`.)
EOD_FLATTEN_MIN = 15

# ── HTF trend filter (Lever #1) ───────────────────────────────────────────────
# Only trade WITH the higher-timeframe trend: longs need a confirmed uptrend, shorts a
# downtrend. Anchored on the reliable daily EMA20/50 (get_daily_trend), with a 4H EMA
# stack as an additional veto. Closes the old loophole where a BOS on a choppy tape
# (daily trend = None) still traded — the QQQ long that got taken with no clear trend.
TREND_EMA_FAST = 20
TREND_EMA_SLOW = 50

# ── Structural stops & targets ────────────────────────────────────────────────
# Stops anchor to the FVG/OB invalidation but are floored OUTSIDE the noise: at least
# MIN_STOP_ATR_MULT × the 1H ATR, so a single 5m/15m wick can't tag them. Targets pin
# to the nearest 4H liquidity pool that offers at least MIN_TP_RR, capped at MAX_TP_RR
# so the target stays reachable within the session (the bot flattens at the close).
# ── Displacement size gates (added 2026-08-22) ────────────────────────────────
# The stock bot armed zones via detect_displacement_fvg's bare defaults: body >= 40% of
# its OWN range and a close 0.15% past the swing — both SCALE-FREE. A tiny bar in dead
# chop scored identically to a decisive one, and unlike binance_bot nothing downstream
# rechecked size, so a junk zone could arm AND fill unopposed. Values mirror the crypto
# bot, where this pairing is already proven.
DISPLACEMENT_BODY_FRAC = 0.5   # displacement body must be >= 50% of its range
# ...and >= max(ATR_MULT x ATR, MIN_PCT x price) -- see indicators.displacement_min_body.
#
# RAISED FROM 0.6 on 2026-09-04, in step with binance_bot.py. A multiple under 1.0 means
# "smaller than an AVERAGE candle", so an ordinary bar cleared the momentum test. Caught
# on the crypto side (POL/USD armed on a 1.07x-ATR bar) but the identical defect was here.
# Keep the two bots on the same numbers -- they have silently diverged before.
DISPLACEMENT_ATR_MULT  = float(os.getenv("DISPLACEMENT_ATR_MULT", "1.8"))
DISPLACEMENT_MIN_PCT   = float(os.getenv("DISPLACEMENT_MIN_PCT", "0.0015"))
# Minimum FVG width -- a thinner gap is a line, not a zone.
MIN_FVG_PCT            = float(os.getenv("MIN_FVG_PCT", "0.0015"))
# Targets beyond this x the HTF ATR cannot resolve before STALE_TRADE_HOURS / the EOD
# flatten, whichever lands first, so they only ever exit on the clock.
MAX_TARGET_ATR_MULT    = float(os.getenv("MAX_TARGET_ATR_MULT", "1.5"))

MIN_STOP_ATR_MULT = 1.5
MIN_TP_RR = 2.0
MAX_TP_RR = 4.0

# ── TEMPORARY risk throttle (while the new crypto logic is being proven) ──────────
# Hard-cap the ACTUAL dollar loss at the stop — measured AFTER leverage, since sizing
# multiplies qty by PAPER_LEVERAGE (so a "$X risk" becomes X×leverage at the stop). This
# is a pure sizing throttle; strategy/entry logic is untouched. Keeps stock stop-outs to
# ~$30 (like the crypto bot's $8-30) instead of $200-500 while we test. Raise/remove once
# the edge is proven. Set to None to disable.
MAX_STOCK_RISK_DOLLARS = 30.0

MIN_AI_RR = 2.0   # hard floor — 1:2 minimum keeps TP reachable intraday on 15m entries
MAX_AI_RR = 15.0  # sanity ceiling — guards against a hallucinated target

# ── Simulated leverage (paper perps / margin mode) ────────────────────────────
# 1  = pure spot / unlevered (default — Alpaca paper account, normal shares)
# 2  = Reg-T intraday margin (legal limit for US stock day-trading accounts)
# 4  = Pattern Day Trader intraday limit (US regulated, requires $25k account)
# 5+ = futures-style (ES/NQ micro contracts, or if porting to a futures broker)
# The bot multiplies qty by this factor; Alpaca paper account tracks the P&L
# on the full position naturally. A liquidation warning fires in logs if the
# loss would exceed the margin posted on a real leveraged account.
PAPER_LEVERAGE = 4   # change to 2, 3, or 4 to simulate leveraged returns


def get_ai_confirmation(symbol, price, daily_trend, bos_dir,
                        fvg_low, fvg_high, sweep_level,
                        sl, risk_amt, pool_tp, ltf_df, htf_df=None,
                        amd_phase=None, zone_type=None):
    """
    Asks Llama 3.3 70B (via NVIDIA API) whether this SMC setup is worth taking,
    and lets it pick the R:R target itself (we only enforce a 1:{MIN_AI_RR} floor).
    Returns (confirm: bool, rr: float, reason: str).

    The AI is a SECONDARY confirmation — the setup already passed every technical
    filter (BOS, sweep, FVG, R:R floor) before we get here. So an AI *failure*
    (timeout, network error, missing key, unparseable reply) falls back to proceeding
    on the technicals at the 1:{MIN_AI_RR} floor, rather than silently killing a valid
    trade. Only an explicit AI "NO" vetoes. (Matches binance_bot — an API hiccup must
    not cost a setup; the stock bot was skipping every timed-out trade "for safety".)
    """
    if not NVIDIA_API_KEY:
        return True, MIN_AI_RR, "no AI key — proceeding on technicals"

    side = "LONG" if bos_dir == "bullish" else "SHORT"
    pool_line = (f"Nearest structural target: ${pool_tp:,.4f}  "
                 f"(implies 1:{abs(pool_tp - price) / risk_amt:.1f} R:R)"
                 if pool_tp else "Nearest structural target: none found")

    # Full 4H chart context — 20 candles so the AI can see the sweep, BOS, FVG, and AMD phase
    if htf_df is not None and len(htf_df) >= 5:
        htf_rows = htf_df.tail(20)
        swing_high = float(htf_df['high'].tail(50).max())
        swing_low  = float(htf_df['low'].tail(50).min())
        htf_block = "4H candles (oldest → newest):\n" + "\n".join(
            f"  {i+1:2d}. O:{r['open']:,.2f} H:{r['high']:,.2f} "
            f"L:{r['low']:,.2f} C:{r['close']:,.2f}"
            for i, (_, r) in enumerate(htf_rows.iterrows())
        ) + f"\n50-bar structure: Low ${swing_low:,.2f}  High ${swing_high:,.2f}"
    else:
        htf_block = "(4H data unavailable)"

    # Last 5 LTF candles for execution precision
    ltf_recent = ltf_df.tail(5)
    ltf_candles = "  ".join(
        f"O:{r['open']:.2f} H:{r['high']:.2f} L:{r['low']:.2f} C:{r['close']:.2f}"
        for _, r in ltf_recent.iterrows()
    )

    # News CONTEXT for the model (policy: context only — never a hardcoded gate). This
    # replaces the old direction-blind FinBERT veto, which blocked SHORTS on bad news —
    # backwards, since bad news argues FOR a short. Fail-soft: any failure renders as
    # "no fresh news" and the setup is judged on structure exactly as before.
    try:
        from bot.news import get_news_context
        news_block, _nh, _ns, _np = get_news_context(
            symbol, ALPACA_API_KEY, ALPACA_API_SECRET, sentiment_fn=estimate_sentiment)
    except Exception:
        news_block = "RECENT NEWS: unavailable (news lookup failed) — judge on structure alone."

    prompt = f"""You are an expert institutional SMC (Smart Money Concepts) trade analyst.

{htf_block}

SETUP SUMMARY:
Symbol       : {symbol}
Direction    : {side}
Current Price: ${price:,.4f}
Daily Trend  : {(daily_trend or 'UNCLEAR').upper()}
4H BOS       : {(bos_dir or 'NONE').upper()}
Swept level  : ${sweep_level:,.4f}  (liquidity grab)
FVG / OB zone: ${fvg_low:,.4f} – ${fvg_high:,.4f}  (entry zone, price is inside)
Stop Loss    : ${sl:,.4f}  (risk = ${risk_amt:,.4f} per share)
{pool_line}
Last 5 × 15m candles: {ltf_candles}

{news_block}

Using the full 4H chart above, analyze this SMC setup:
- Weigh the news above as CONTEXT ONLY: does it support or contradict this {side}?
  Structure decides the trade; news can raise or lower your confidence in it, and should
  never be the sole reason to take or skip a setup. Note that bad news is an argument FOR
  a short, not against one. If there is no fresh news, judge on structure alone.
- Did a genuine liquidity sweep occur at the swept level?
- Is the FVG/OB entry zone structurally valid?
- Does AMD (Accumulation → Manipulation → Distribution) context support this {side}?
- How much room does price have before the next opposing liquidity pool?

Pick a risk:reward ratio of AT LEAST 3.5 — go higher only if structure genuinely supports it.

Reply in EXACTLY this format, one field per line, nothing else:
DECISION: YES or NO
RR: a number >= 3.5
REASON: one concise sentence"""

    try:
        from openai import OpenAI
        client = OpenAI(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key=NVIDIA_API_KEY,
        )
        resp = client.chat.completions.create(
            # Shared resolver — see bot/ai_model.py. The hardcoded id was
            # decommissioned by NVIDIA and this bot, like the crypto one, fails
            # OPEN, so it logged approvals no model ever gave.
            model=(_ai_model.resolve(NVIDIA_API_KEY) or _ai_model.DEFAULT_MODEL),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=120,
            timeout=15,
        )
        text = resp.choices[0].message.content.strip()

        decision_m = re.search(r"DECISION:\s*(YES|NO)", text, re.IGNORECASE)
        rr_m       = re.search(r"RR:\s*([\d.]+)", text, re.IGNORECASE)
        reason_m   = re.search(r"REASON:\s*(.+)", text, re.IGNORECASE | re.DOTALL)

        confirm = bool(decision_m) and decision_m.group(1).upper() == "YES"
        rr      = float(rr_m.group(1)) if rr_m else MIN_AI_RR
        rr      = max(MIN_AI_RR, min(rr, MAX_AI_RR))
        reason  = reason_m.group(1).strip() if reason_m else text

        if decision_m is None:
            # Couldn't read a clear YES/NO — treat as no-signal, fall back to technicals
            # rather than dropping a setup that already passed every technical filter.
            return True, MIN_AI_RR, f"AI reply unparsed — proceeding on technicals ({text[:80]})"

        return confirm, rr, reason
    except Exception as e:
        # Timeout / network / API error — proceed on technicals, don't skip a valid setup.
        return True, MIN_AI_RR, f"AI unavailable ({e}) — proceeding on technicals"

STALE_TRADE_HOURS = 6   # intraday SMC: close stale positions that haven't resolved in 6h


class DebbieLaSMC(Strategy):
    """
    Multi-asset Debbie-La Institutional Setup Bot.
    Each symbol runs an independent state machine with:
    - Time-based exit: closes stale trades after STALE_TRADE_HOURS
    - News-optional sentiment: proceeds if no news is available
    - Startup sync: re-syncs state from live positions on restart
    """

    def initialize(self, symbols: list = None, cash_at_risk: float = 0.03,
                   timeframe_htf: str = "4 hours", timeframe_ltf: str = "15 minutes"):
        self.symbols = symbols or ["AAPL", "QQQ", "SPY", "NVDA", "TSLA", "GOOGL"]
        self.sleeptime = "15M"
        self.timeframe_htf = timeframe_htf
        self.timeframe_ltf = timeframe_ltf
        self.cash_at_risk_per_symbol = cash_at_risk / len(self.symbols)

        self.state              = {s: "IDLE" for s in self.symbols}
        self.bias               = {s: None   for s in self.symbols}
        self.sweep_low          = {s: None   for s in self.symbols}
        self.sweep_high         = {s: None   for s in self.symbols}
        self.sweep_hunt_iter    = {s: 0      for s in self.symbols}
        self.fvg_low            = {s: None   for s in self.symbols}
        self.fvg_high           = {s: None   for s in self.symbols}
        self.fvg_set_iter       = {s: 0      for s in self.symbols}
        self.ote_zone           = {s: None   for s in self.symbols}
        self.entry_price        = {s: None   for s in self.symbols}
        self.entry_qty          = {s: 0      for s in self.symbols}
        self.stop_loss          = {s: None   for s in self.symbols}
        self.take_profit        = {s: None   for s in self.symbols}
        self.entry_time         = {s: None   for s in self.symbols}
        self.sl_order           = {s: None   for s in self.symbols}
        self.tp_order           = {s: None   for s in self.symbols}
        self.oco_ids            = {s: []     for s in self.symbols}  # broker OCO order IDs to cancel on reset
        self.bracket_active     = {s: False  for s in self.symbols}  # True when entry went in as a bracket
        self.entry_order        = {s: None   for s in self.symbols}
        self.ranging_mode       = {s: False  for s in self.symbols}
        self.amd_phase          = {s: None   for s in self.symbols}
        self.amd_zone_type      = {s: None   for s in self.symbols}
        self.zone_set_price     = {s: None   for s in self.symbols}  # price when trend_follow zone armed
        self._iter_count        = 0

        # ── Google Sheets logging ────────────────────────────────────────────
        ensure_tabs(get_sheet_client(), GOOGLE_SHEET_URL, {
            "Stock Macro": MACRO_HEADER, "Stock Ledger": LEDGER_HEADER,
        })
        self._sheet_log_date    = None    # UTC date of the last daily-snapshot row written
        self._daily_open_balance = None   # captured lazily on the first iteration
        self._daily_open_spy    = None    # captured lazily on the first iteration with a SPY price
        self._eod_flattened_date = None   # ET date the EOD flatten last actually ran — lets
        # needs_eod_catchup_flatten tell "haven't flattened today" apart from "already did"

    def on_bot_start(self):
        """Kill the two stale BRACKET orders that spam WARNING every iteration."""
        stale_ids = [
            "cea85ee0-f74f-40b5-a9ac-a5740c5adfdb",
            "e6e0b8c0-1d28-4056-8069-62b6bd0ce8ab",
        ]
        try:
            from alpaca.trading.client import TradingClient
            client = TradingClient(ALPACA_API_KEY, ALPACA_API_SECRET, paper=True)
            for oid in stale_ids:
                try:
                    client.cancel_order_by_id(oid)
                    self.log_message(f"Cancelled stale order {oid}", color="cyan")
                except Exception as e:
                    self.log_message(f"Stale order {oid} already gone: {e}", color="yellow")
        except Exception as e:
            self.log_message(f"Stale order cleanup skipped: {e}", color="yellow")

    def _save_state(self):
        """Persist SL/TP for every open position so restarts don't lose protection.
        Preserves the "_daily_snapshot" key (written by _save_daily_snapshot_flag)
        across this rewrite — this function rebuilds `data` from scratch on every
        call, so without carrying it over, the next position-state save would
        silently erase today's once-per-day dedup flag."""
        try:
            data = {}
            if os.path.exists(STRATEGY_STATE_FILE):
                try:
                    with open(STRATEGY_STATE_FILE) as f:
                        _prev = json.load(f)
                    if "_daily_snapshot" in _prev:
                        data["_daily_snapshot"] = _prev["_daily_snapshot"]
                except Exception:
                    pass
            for s in self.symbols:
                if self.state[s] == "POSITION_OPEN":
                    _qty   = self.entry_qty.get(s) or 0
                    _entry = self.entry_price[s] or 0
                    data[s] = {
                        "state":       self.state[s],
                        "bias":        self.bias[s],
                        "entry_price": _entry,
                        "stop_loss":   self.stop_loss[s],
                        "take_profit": self.take_profit[s],
                        "entry_time":  (self.entry_time[s].isoformat()
                                        if self.entry_time[s] else None),
                        "oco_ids":     self.oco_ids.get(s, []),
                        "margin":      abs(_qty) * _entry / PAPER_LEVERAGE if _entry else None,
                        "leverage":    PAPER_LEVERAGE,
                    }
            with open(STRATEGY_STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.log_message(f"[STATE] save failed: {e}", color="red")

    def _save_daily_snapshot_flag(self):
        """Persist the once-per-day Stock Macro snapshot dedup tracker. Without this,
        every restart resets self._sheet_log_date to None and re-logs "today" again
        — seen live: two rows for the same date after a same-day restart. Merges into
        the existing state file rather than calling _save_state() (which only writes
        when a position is open and doesn't touch these fields itself)."""
        try:
            data = {}
            if os.path.exists(STRATEGY_STATE_FILE):
                with open(STRATEGY_STATE_FILE) as f:
                    data = json.load(f)
            data["_daily_snapshot"] = {
                "date":         self._sheet_log_date.isoformat() if self._sheet_log_date else None,
                "open_balance": self._daily_open_balance,
                "open_spy":     self._daily_open_spy,
            }
            with open(STRATEGY_STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.log_message(f"[STATE] daily-snapshot save failed: {e}", color="red")

    def _load_state(self):
        """Restore SL/TP from last save so Python-side monitoring resumes correctly."""
        if not os.path.exists(STRATEGY_STATE_FILE):
            return
        try:
            with open(STRATEGY_STATE_FILE) as f:
                data = json.load(f)
            ds = data.get("_daily_snapshot")
            if ds:
                if ds.get("date"):
                    try:
                        self._sheet_log_date = datetime.fromisoformat(ds["date"]).date()
                    except Exception:
                        pass
                if ds.get("open_balance") is not None:
                    self._daily_open_balance = ds["open_balance"]
                if ds.get("open_spy") is not None:
                    self._daily_open_spy = ds["open_spy"]
            for s, saved in data.items():
                if s not in self.symbols:
                    continue
                self.state[s]       = saved.get("state", "IDLE")
                self.bias[s]        = saved.get("bias")
                self.entry_price[s] = saved.get("entry_price")
                self.stop_loss[s]   = saved.get("stop_loss")
                self.take_profit[s] = saved.get("take_profit")
                self.oco_ids[s]     = saved.get("oco_ids", [])
                raw_time            = saved.get("entry_time")
                self.entry_time[s]  = (datetime.fromisoformat(raw_time)
                                       if raw_time else None)
                _mgn = saved.get("margin")
                _ep  = saved.get("entry_price")
                _lev = saved.get("leverage", PAPER_LEVERAGE)
                if _mgn and _ep:
                    self.entry_qty[s] = int(_mgn * _lev / _ep)
                if self.state[s] == "POSITION_OPEN":
                    self.log_message(
                        f"[{s}] Restored POSITION_OPEN  SL={self.stop_loss[s]}  TP={self.take_profit[s]}",
                        color="yellow"
                    )
        except Exception as e:
            self.log_message(f"[STATE] load failed: {e}", color="red")

    def before_starting_trading(self):
        """Re-sync state from live positions and saved SL/TP on startup.
        Also detects overnight gaps that already violated the SL (the broker stop
        should have fired, but covers the case where it didn't or the bot restarts
        mid-gap before the fill clears)."""
        self._load_state()
        try:
            for symbol in self.symbols:
                asset    = self._make_asset(symbol)
                position = self.get_position(asset)
                # ── Startup EOD safety net ──────────────────────────────────────
                # The live EOD_FLATTEN_MIN guard only runs INSIDE on_trading_iteration's
                # loop — it can't catch anything if the bot isn't actively running during
                # that 15-min window. A restart landing outside market hours (e.g. the bot
                # gets restarted in the evening — a completely normal, frequent event)
                # silently skips it, so a position opened hours earlier rides overnight
                # with nothing checking it. REAL INCIDENT: NVDA entered 10:19 AM ET,
                # process restarted 5:53 PM ET — 2h after the close — and nothing flattened
                # it. Close immediately at startup if the market isn't open RIGHT NOW,
                # regardless of internal state tracking (broker position is ground truth).
                if position is not None and abs(float(position.quantity)) > 0:
                    now_et = datetime.now(_ET)
                    if not indicators.is_regular_session(now_et.weekday(), now_et.hour, now_et.minute):
                        self.log_message(
                            f"[{symbol}] ⚠️ Startup: position found but market is CLOSED "
                            f"(restarted outside regular hours) — flattening immediately "
                            f"instead of holding it overnight/into the weekend.",
                            color="red")
                        self._cancel_oco(symbol)
                        close_qty  = abs(float(position.quantity))
                        close_side = (Order.OrderSide.BUY if float(position.quantity) < 0
                                      else Order.OrderSide.SELL)
                        submitted = self.submit_order(self.create_order(asset, close_qty, close_side))
                        if submitted is not None:
                            self._reset(symbol)
                            continue
                        else:
                            self.log_message(
                                f"[{symbol}] ⚠️ Startup flatten order submission failed — "
                                f"will retry via the normal loop once market reopens.",
                                color="red")

                if position is not None and self.state[symbol] != "POSITION_OPEN":
                    self.state[symbol] = "POSITION_OPEN"
                    # Recover the REAL entry time from Alpaca, not "now" — otherwise a
                    # restart makes a days-old leftover look freshly entered and the
                    # overnight-leftover flatten below never fires.
                    _real_et = self._broker_entry_time(symbol, float(position.quantity) > 0)
                    try:
                        self.entry_time[symbol] = (datetime.fromisoformat(_real_et.replace("Z", "+00:00"))
                                                   if _real_et else datetime.now(timezone.utc))
                    except Exception:
                        self.entry_time[symbol] = datetime.now(timezone.utc)
                    self.log_message(
                        f"[{symbol}] Startup sync: live position found → POSITION_OPEN "
                        f"(entered {self.entry_time[symbol]:%Y-%m-%d %H:%M}Z)  "
                        f"SL={self.stop_loss[symbol]}  TP={self.take_profit[symbol]}",
                        color="yellow"
                    )
                elif position is None and self.state[symbol] == "POSITION_OPEN":
                    # Position closed while bot was offline (broker stop fired) — log the
                    # round trip (snapped to SL/TP) before cleanup, then clean up.
                    try:
                        last = self.get_last_price(asset)
                        self._log_broker_side_close(symbol, float(last) if last else None)
                    except Exception:
                        pass
                    self._reset(symbol)
                    continue

                # ── Gap-open check ────────────────────────────────────────────
                # If the position survived overnight but the open price has already
                # blown past our SL (e.g., broker stop fill delayed or bot restarted
                # mid-gap), close immediately at market rather than ride it further.
                if self.state[symbol] == "POSITION_OPEN" and position is not None:
                    sl  = self.stop_loss.get(symbol)
                    is_long = float(position.quantity) > 0
                    try:
                        bar = self.get_last_price(asset)
                        open_price = float(bar) if bar else None
                    except Exception:
                        open_price = None
                    if sl and open_price:
                        gap_violated = (is_long and open_price < sl) or (not is_long and open_price > sl)
                        if gap_violated:
                            self.log_message(
                                f"[{symbol}] ⚠️ GAP OPEN: open ${open_price:.2f} has blown past "
                                f"SL ${sl:.2f} — closing immediately at market.",
                                color="red")
                            self._cancel_oco(symbol)
                            close_qty  = abs(float(position.quantity))
                            close_side = (Order.OrderSide.BUY if not is_long
                                          else Order.OrderSide.SELL)
                            submitted = self.submit_order(self.create_order(asset, close_qty, close_side))
                            # Same guard as the manual/stale exits: only clear our tracking
                            # if the close was actually accepted, otherwise _reconcile_position
                            # re-adopts this position (with fresh protection) next iteration
                            # instead of it silently going naked.
                            if submitted is not None:
                                self._reset(symbol)
                            else:
                                self.log_message(
                                    f"[{symbol}] ⚠️ Gap-open close order submission failed — "
                                    f"position still open, NOT resetting state.",
                                    color="red"
                                )
        except Exception as e:
            self.log_message(f"Startup sync skipped (broker not ready): {e}", color="red")

    def _cancel_oco(self, symbol):
        """Cancel ALL broker-side protective orders for this symbol (OCO/bracket legs).

        Two-pass strategy so nothing slips through:
        1. Cancel the stored leg IDs (fast path — known IDs from when the order was posted).
        2. Fetch every open order for this symbol from Alpaca and cancel anything still
           standing. This catches legs whose IDs weren't captured, orders in unexpected
           states, and any race where one ID cancel failed silently.
        """
        import requests as _req
        _headers = {
            "APCA-API-KEY-ID":     ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
        }

        ids = self.oco_ids.get(symbol) or []
        cancelled = 0

        # Pass 1 — cancel stored IDs
        for oid in ids:
            try:
                _req.delete(
                    f"{ALPACA_BASE_URL}/v2/orders/{oid}",
                    headers=_headers, timeout=10,
                )
                cancelled += 1
            except Exception:
                pass  # 404/422 = already filled or cancelled, that's fine

        # Pass 2 — belt-and-suspenders: fetch all open orders for this symbol and
        # cancel any that survived (handles wrong-state failures in pass 1).
        try:
            resp = _req.get(
                f"{ALPACA_BASE_URL}/v2/orders",
                params={"status": "open", "symbols": symbol, "limit": 50},
                headers=_headers, timeout=10,
            )
            if resp.ok:
                for o in (resp.json() or []):
                    oid = o.get("id")
                    if not oid:
                        continue
                    try:
                        _req.delete(
                            f"{ALPACA_BASE_URL}/v2/orders/{oid}",
                            headers=_headers, timeout=10,
                        )
                        cancelled += 1
                    except Exception:
                        pass
        except Exception:
            pass

        if cancelled or ids:
            self.log_message(
                f"[{symbol}] 🧹 Cancelled {cancelled} broker order(s) "
                f"(stored IDs: {len(ids)}).", color="cyan"
            )
        self.oco_ids[symbol] = []

    def _order_reached_broker(self, symbol):
        """True if Alpaca already has a live order OR an open position for `symbol`.

        Guards the bracket-fallback path. The POST can raise AFTER Alpaca accepted the
        order — a read timeout, a connection reset, or an unparseable body all throw once
        the request is already on the wire. The old code treated every exception as "the
        order did not happen" and fired a second, naked market order. Real incident
        2026-08-14: two MSFT entries at the IDENTICAL price 494.77, both closing near
        -3R. Fail CLOSED — if we cannot prove the broker is clean, do not send another
        order; the next iteration's _ensure_protection will attach protection.
        """
        hdr = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_API_SECRET}
        try:
            import requests as _req
            _LIVE = {"new", "held", "accepted", "pending_new", "accepted_for_bidding",
                     "partially_filled", "calculated", "pending_replace", "filled"}
            r = _req.get(f"{ALPACA_BASE_URL}/v2/orders",
                         params={"status": "all", "symbols": symbol, "nested": "true", "limit": 20},
                         headers=hdr, timeout=10)
            has_order = (r.status_code == 200 and
                         any(o.get("status") in _LIVE for o in (r.json() or [])))
            p = _req.get(f"{ALPACA_BASE_URL}/v2/positions/{symbol}", headers=hdr, timeout=10)
            has_pos = (p.status_code == 200 and
                       abs(float((p.json() or {}).get("qty", 0) or 0)) > 0)
            # Pure, unit-tested decision (tests/test_duplicate_fill_guard.py).
            return not indicators.should_resend_entry_after_error(has_order, has_pos, True)
        except Exception as e:
            # Cannot verify -> assume it DID land. A duplicate live position is far worse
            # than a missed entry; the setup will still be there next iteration.
            self.log_message(f"[{symbol}] ⚠️ Could not verify broker state after a failed "
                             f"bracket ({e}) — assuming the order landed, NOT re-sending.",
                             color="red")
            return True

    def _ensure_protection(self, symbol, position, is_long, sl, tp):
        """Guarantee a live position always has a broker-side stop. If none is found on the
        book (legs expired at the close, were cancelled, or lost across a restart), re-post a
        GTC OCO so the position is never left naked overnight."""
        if not (sl and tp):
            # Previously a silent no-op — a position could sit naked indefinitely with zero
            # trace of why. Real incident: after _reset() wiped self.stop_loss/take_profit
            # to None (a false-negative close read), this guard had nothing to re-attach
            # WITH and gave no signal that protection was missing.
            self.log_message(f"[{symbol}] ⚠️ _ensure_protection has no sl/tp to work with "
                             f"(self.stop_loss/take_profit is empty for this symbol) — "
                             f"cannot verify or re-attach protection this iteration.", color="red")
            return
        hdr = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_API_SECRET}
        try:
            import requests as _req
            # status=open EXCLUDES a bracket's stop leg while it sits in "held" status —
            # Alpaca keeps one OCO leg "held" until the other triggers. Querying only
            # "open" made the bot see a fully-protected position as NAKED, then try to
            # POST a duplicate OCO → 403 Forbidden (the 104 shares are already committed
            # to the live bracket). Query "all" and count a stop only when its order/leg
            # is in a LIVE (non-terminal) status.
            r = _req.get(f"{ALPACA_BASE_URL}/v2/orders",
                         params={"status": "all", "symbols": symbol, "nested": "true", "limit": 50},
                         headers=hdr, timeout=10)
            orders = r.json() if r.status_code == 200 else []
            _LIVE = {"new", "held", "accepted", "pending_new", "accepted_for_bidding",
                     "partially_filled", "calculated", "pending_replace"}
            def _has_stop(o):
                if o.get("stop_price") and o.get("status") in _LIVE:
                    return True
                return any(l.get("stop_price") and l.get("status") in _LIVE
                           for l in (o.get("legs") or []))
            if any(_has_stop(o) for o in orders):
                return  # already protected — nothing to do

            _is_crypto = self._make_asset(symbol).asset_type == Asset.AssetType.CRYPTO
            # int() truncated any crypto qty < 1 (e.g. 0.149675 BTC) to 0, silently
            # no-op'ing here with zero log — every fractional crypto position was
            # unprotectable through this path regardless of the order-type fix below.
            qty = abs(position.quantity) if _is_crypto else int(abs(position.quantity))
            if qty <= 0:
                return
            body = {"symbol": symbol, "qty": str(qty),
                    "side": "sell" if is_long else "buy", "type": "limit",
                    "time_in_force": "gtc", "order_class": "oco",
                    "take_profit": {"limit_price": str(round(tp, 2) if not _is_crypto else tp)},
                    "stop_loss":   indicators.build_protective_leg(
                                       round(sl, 2) if not _is_crypto else sl, _is_crypto, is_long)}
            resp = _req.post(f"{ALPACA_BASE_URL}/v2/orders", json=body, headers=hdr, timeout=10)
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            ids = [data["id"]] if data.get("id") else []
            ids += [l["id"] for l in (data.get("legs") or []) if l.get("id")]
            self.oco_ids[symbol]      = ids
            self.bracket_active[symbol] = False
            self.log_message(f"[{symbol}] 🛡 Re-attached protection — position was NAKED, "
                             f"posted GTC OCO (SL {sl:.2f} / TP {tp:.2f}).", color="yellow")
        except Exception as e:
            self.log_message(f"[{symbol}] ⚠️ Couldn't verify/re-attach protection: {e}", color="red")

    def _logged_close_ids(self):
        """Set of Alpaca order-ids we've already written to the ledger as broker-side
        closes. Persisted so a restart that re-detects the same already-closed
        position can't log it a second time."""
        try:
            with open(LOGGED_CLOSES_FILE) as f:
                return set(json.load(f))
        except Exception:
            return set()

    def _mark_close_logged(self, close_id):
        try:
            ids = self._logged_close_ids()
            ids.add(close_id)
            with open(LOGGED_CLOSES_FILE, "w") as f:
                json.dump(list(ids)[-300:], f)   # cap so the file can't grow forever
        except Exception:
            pass

    def _broker_entry_time(self, symbol, is_long):
        """Real opening-fill timestamp (ISO string) of the CURRENT open position, from
        Alpaca — so a restart recovers the TRUE entry time instead of stamping 'now'.
        Needed to detect a leftover/overnight hold (is_leftover_position). None on failure."""
        try:
            import requests as _req
            hdr = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_API_SECRET}
            r = _req.get(f"{ALPACA_BASE_URL}/v2/orders",
                         params={"status": "closed", "symbols": symbol,
                                 "limit": 50, "direction": "desc"},
                         headers=hdr, timeout=10)
            open_side = "buy" if is_long else "sell"
            for o in (r.json() if r.status_code == 200 else []):
                if o.get("status") == "filled" and o.get("side") == open_side and o.get("filled_at"):
                    return o.get("filled_at")   # most recent opening fill = this position's entry
        except Exception:
            pass
        return None

    def _real_round_trip(self, symbol, is_long):
        """Reconstruct the ACTUAL round trip from Alpaca's filled orders so the ledger
        reflects real fills — including gap slippage where a stop triggers at one price
        but fills far worse (e.g. META's overnight gap: 619.31 stop, 607.74 fill).

        `is_long` is used only as a SANITY CHECK, never to pick sides: entry/exit are
        matched purely from the order sequence (match_last_round_trip), immune to a
        stale self.bias. A real corrupted ledger row (SPY, 2026-07-23) came from the
        old is_long-driven matcher: leftover POSITION_OPEN state from a much earlier
        SPY position had bias="SHORT" sitting stale, which made it pick the sell-leg of
        one real trade as "entry" and the buy-leg of a LATER, unrelated real trade as
        "exit" — fabricating a -$916.90 round trip out of two real ones (-$47.94 and
        -$185.42). If the matched exit's side disagrees with is_long, our tracked state
        has drifted from the broker — log it and refuse rather than fabricate a row.
        Returns {entry_price, entry_time, exit_price, exit_time, qty, close_id} or None."""
        try:
            import requests as _req
            hdr = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_API_SECRET}
            r = _req.get(f"{ALPACA_BASE_URL}/v2/orders",
                         params={"status": "closed", "symbols": symbol,
                                 "limit": 50, "direction": "desc"},
                         headers=hdr, timeout=10)
            orders = r.json() if r.status_code == 200 else []
            entry_o, exit_o = indicators.match_last_round_trip(orders)
            if not exit_o:
                return None
            exit_is_long_close = exit_o.get("side") == "sell"   # a LONG closes by selling
            if exit_is_long_close != is_long:
                self.log_message(
                    f"[{symbol}] ⚠️ Tracked bias ({'LONG' if is_long else 'SHORT'}) disagrees "
                    f"with the actual closing fill side ({exit_o.get('side')}) — tracked "
                    f"state has drifted from the broker. Not logging a possibly-fabricated "
                    f"round trip.", color="red")
                return None
            return {
                "entry_price": float(entry_o["filled_avg_price"]) if entry_o else None,
                "entry_time":  entry_o.get("filled_at") if entry_o else None,
                "exit_price":  float(exit_o["filled_avg_price"]),
                "exit_time":   exit_o.get("filled_at"),
                "qty":         abs(float(exit_o.get("filled_qty") or 0)) or None,
                "close_id":    exit_o.get("id"),
            }
        except Exception:
            return None

    def _log_broker_side_close(self, symbol, current_price):
        """Log a trade that Alpaca closed server-side via its bracket/OCO leg (the
        common case — our Python SL/TP poll rarely beats the resting broker order).

        Pulls the REAL closing fill from Alpaca (true price/time, so an overnight gap
        that fills past the stop shows the real loss) instead of assuming the bracket
        filled exactly at SL/TP. De-duplicated by the closing order id so a restart
        that re-detects the same closed position can't double-log it. Falls back to the
        old SL/TP snap only if Alpaca is unreachable. Fully fail-soft — any error here
        must NOT block _reset (a naked position still has to be cleaned up)."""
        try:
            is_long = self.bias.get(symbol) == "BULLISH"
            sl = self.stop_loss.get(symbol)
            tp = self.take_profit.get(symbol)

            # Pull the real round-trip FIRST, before checking local tracking — it's fully
            # self-sufficient (only uses `is_long` as a sanity check against Alpaca's own
            # order history, never derived from local entry_price/entry_qty), so it can
            # recover a trustworthy close even when local tracking is corrupted. REAL
            # INCIDENT 2026-07-30: a duplicate-process race zeroed self.entry_price for
            # NVDA/GOOGL; the OLD code checked local tracking FIRST and silently returned
            # before ever attempting this real lookup, permanently losing a real +$64.48
            # GOOGL take-profit close (had to be manually backfilled).
            rt = self._real_round_trip(symbol, is_long)

            entry_p = self.entry_price.get(symbol)
            qty     = self.entry_qty.get(symbol)

            # De-dup: never write the same broker-side close twice (survives restarts).
            close_id = rt.get("close_id") if rt else None
            if close_id and close_id in self._logged_close_ids():
                self.log_message(
                    f"[{symbol}] Broker close {close_id[:8]} already in ledger — skipping duplicate.",
                    color="cyan")
                return

            entry_iso = exit_iso = None
            if rt and rt.get("exit_price"):
                exit_p    = rt["exit_price"]
                exit_iso  = rt.get("exit_time")
                entry_iso = rt.get("entry_time")
                if rt.get("entry_price"):
                    entry_p = rt["entry_price"]   # true entry fill, not the intended price
                if rt.get("qty"):
                    qty = rt["qty"]
            elif entry_p and qty:
                # No real round-trip available (Alpaca unreachable) — fall back to local
                # tracking + SL/TP-snap estimate, the old behaviour: an ESTIMATE that
                # hides slippage but beats logging nothing.
                if sl and tp and current_price:
                    exit_p = sl if abs(current_price - sl) <= abs(current_price - tp) else tp
                else:
                    exit_p = current_price or entry_p
            else:
                # Neither a real round-trip NOR local tracking has anything trustworthy —
                # genuinely nothing to log (this is the only case that should bail silently).
                self.log_message(
                    f"[{symbol}] Broker-side close detected but no real fills found AND "
                    f"local tracking is empty — cannot log, needs manual reconcile.",
                    color="red")
                return

            pnl = ((exit_p - entry_p) * qty if is_long else (entry_p - exit_p) * qty)
            self._log_trade_close_to_sheet(symbol, is_long, entry_p, exit_p, qty, pnl,
                                           entry_iso=entry_iso, exit_iso=exit_iso)
            if close_id:
                self._mark_close_logged(close_id)
        except Exception as e:
            self.log_message(f"[{symbol}] Broker-close logging skipped: {e}", color="red")

    def _log_trade_close_to_sheet(self, symbol, is_long, entry_price, exit_price, qty, pnl,
                                  entry_iso=None, exit_iso=None, exit_reason=None):
        """Fire-and-forget: append a completed round-trip row to the Stock Ledger tab,
        including a candlestick chart of the trade hosted on GitHub (Drive hosting
        isn't viable — service accounts have no storage quota and can't accept
        ownership transfers) with a local charts/ save as a fallback if the GitHub
        push fails. MUST be called before _reset(symbol) — that wipes
        entry_price/amd_phase/stop_loss/take_profit/entry_time, which this reads.
        sheets_logger's own functions are already fail-soft; the chart
        render/upload is separately wrapped so a bad bars fetch, GitHub hiccup, or
        disk error can never block the ledger row itself from being written."""
        reason = f"{self.amd_phase[symbol] or 'BOS'} {'LONG' if is_long else 'SHORT'}"
        now = datetime.now(timezone.utc)
        entry_time = self.entry_time[symbol] or now
        sl = self.stop_loss[symbol]
        tp = self.take_profit[symbol]

        # Timestamps written to the sheet: prefer the REAL Alpaca fill times (passed in
        # for broker-side closes) so a position held overnight shows its true entry/exit
        # times — not "now". Fall back to tracked entry_time / now for manual exits.
        def _sheet_ts(iso, fallback_dt):
            if iso:
                try:
                    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().isoformat()
                except Exception:
                    pass
            return fallback_dt.astimezone().isoformat()
        entry_time_str = _sheet_ts(entry_iso, entry_time)
        exit_time_str  = _sheet_ts(exit_iso, now)

        chart_ref = None
        try:
            side = "LONG" if is_long else "SHORT"
            # Preferred: screenshot of a REAL TradingView chart (their data/candles/
            # axes) with entry/SL/TP overlaid — stock bot only, per user preference.
            chart_path = render_stock_tradingview(symbol, side, entry_price, sl, tp,
                                                    PAPER_LEVERAGE,
                                                    self.entry_time[symbol], now)
            if not chart_path:
                # Fallback: self-rendered lightweight-charts image from Alpaca bars
                asset = self._make_asset(symbol)
                bars = self.get_historical_prices(asset, 200, self.timeframe_ltf)
                df = bars.pandas_df if bars is not None else None
                chart_path = render_trade_chart(df, symbol, side,
                                                  entry_price, sl, tp, PAPER_LEVERAGE,
                                                  str(self.timeframe_ltf), self.entry_time[symbol])
            filename = f"{symbol}_{now:%Y%m%dT%H%M%S}.png"
            chart_ref = upload_chart_to_github(chart_path, filename)
            if chart_ref:
                os.remove(chart_path)
            else:
                chart_ref = save_chart_locally(chart_path, filename)
        except Exception as e:
            self.log_message(f"[{symbol}] Chart generation failed: {e}", color="red")

        margin   = qty * entry_price / PAPER_LEVERAGE
        notional = qty * entry_price
        log_trade(get_sheet_client(), GOOGLE_SHEET_URL, "Stock Ledger",
                  entry_time_str, exit_time_str, symbol,
                  "LONG" if is_long else "SHORT", entry_price, sl, tp, exit_price, qty,
                  margin, notional, PAPER_LEVERAGE, pnl, reason, chart_ref,
                  # Exits here fire at the BROKER (bracket OCO legs), so there is no label
                  # to read — infer from which leg the fill landed on. No breakeven_moved
                  # argument: unlike the crypto bot, this strategy has no break-even trail,
                  # so that bucket cannot occur. Callers that DO know (the stale exit, the
                  # EOD flatten) pass exit_reason explicitly and win over the inference.
                  exit_reason=exit_reason or indicators.infer_exit_reason(exit_price, sl, tp))

    def _reset(self, symbol):
        # Cancel the broker-side OCO/bracket legs (TP/SL) so they don't linger as orphans
        self._cancel_oco(symbol)
        self.bracket_active[symbol] = False

        # Legacy Lumibot-tracked SL/TP orders (vestigial — OCO is posted via REST now)
        for order_attr in ("sl_order", "tp_order"):
            order = getattr(self, order_attr).get(symbol)
            if order is not None:
                try:
                    self.cancel_order(order)
                except Exception:
                    pass
                getattr(self, order_attr)[symbol] = None

        self.entry_order[symbol]     = None
        self.state[symbol]           = "IDLE"
        self.bias[symbol]            = None
        self.sweep_low[symbol]       = None
        self.sweep_high[symbol]      = None
        self.sweep_hunt_iter[symbol] = 0
        self.fvg_low[symbol]         = None
        self.fvg_high[symbol]        = None
        self.fvg_set_iter[symbol]    = 0
        self.ote_zone[symbol]        = None
        self.entry_price[symbol]     = None
        self.stop_loss[symbol]       = None
        self.take_profit[symbol]     = None
        self.entry_time[symbol]      = None
        self.ranging_mode[symbol]    = False
        self.amd_phase[symbol]       = None
        self.amd_zone_type[symbol]   = None
        self.zone_set_price[symbol]  = None
        self._save_state()

    def _make_asset(self, symbol):
        """Override in subclasses to change asset type (e.g. CRYPTO)."""
        return Asset(symbol, asset_type=Asset.AssetType.STOCK)

    def _minutes_to_close(self):
        """Minutes until the 4:00 PM ET regular-session close, or None when it's not a
        normal weekday session (weekend / already closed). Half-days aren't special-cased
        — those are rare and before_closing_bell still catches the real bell. Crypto
        subclasses trade 24/7 and override this to always return None (never cut off)."""
        now = datetime.now(_ET)
        if now.weekday() >= 5:                      # Sat / Sun
            return None
        close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if now >= close:                            # after the bell — session's done
            return None
        mins = (close - now).total_seconds() / 60.0
        return mins if mins <= 390 else None        # only meaningful within the 6.5h RTH

    def position_sizing(self, symbol, sl_price=None):
        """
        Risk-based sizing: risk exactly cash_at_risk_per_symbol of account on this trade.
        quantity = risk_dollars / sl_distance
        Caps at 20% of cash so one trade can never blow the account.
        Falls back to cash-allocation if no SL is available yet.
        """
        cash = self.get_cash()
        last_price = self.get_last_price(symbol)
        if not last_price:
            return cash, last_price, 0

        if sl_price and abs(last_price - sl_price) > 0:
            risk_dollars = cash * self.cash_at_risk_per_symbol
            sl_distance  = abs(last_price - sl_price)
            quantity     = risk_dollars / sl_distance
        else:
            quantity = (cash * self.cash_at_risk_per_symbol) / last_price

        # Leverage: multiply controlled qty while margin posted = position/leverage.
        # Max-qty cap applies to margin, not to the full leveraged position, so the
        # cap stays sane regardless of leverage level.
        max_qty  = (cash * 0.20) / last_price   # 20% of cash as margin per trade
        quantity = max(0, round(min(quantity, max_qty), 0))
        quantity = int(quantity * PAPER_LEVERAGE)

        # TEMPORARY throttle: cap the LEVERAGED qty so the real loss at the stop
        # (qty × sl_distance) can't exceed MAX_STOCK_RISK_DOLLARS. Applied last, after
        # leverage, so it governs the actual dollar hit. A setup whose stop is so wide
        # that even 1 share exceeds the cap is skipped (qty → 0) — fine while testing.
        if MAX_STOCK_RISK_DOLLARS and sl_price and abs(last_price - sl_price) > 0:
            _max_risk_qty = int(MAX_STOCK_RISK_DOLLARS / abs(last_price - sl_price))
            if quantity > _max_risk_qty:
                self.log_message(
                    f"[{symbol}] 🔒 Risk throttle: {quantity}→{_max_risk_qty} sh "
                    f"(cap stop loss at ${MAX_STOCK_RISK_DOLLARS:.0f} while testing crypto).",
                    color="yellow")
                quantity = _max_risk_qty

        if PAPER_LEVERAGE > 1 and quantity > 0:
            margin    = quantity * last_price / PAPER_LEVERAGE
            self.log_message(
                f"[leverage] {PAPER_LEVERAGE}x — controlling {quantity} shares "
                f"(${quantity*last_price:,.0f}) on ${margin:,.0f} margin. "
                f"Liq if price moves {1/PAPER_LEVERAGE:.0%} against.",
                color="yellow")
        return cash, last_price, quantity

    def get_daily_trend(self, symbol):
        """Daily EMA20 vs EMA50 alignment — must agree with the 4H BOS or we skip."""
        asset = self._make_asset(symbol)
        try:
            bars = self.get_historical_prices(asset, 100, "1 day")
            if bars is None:
                return None
            return indicators.get_daily_trend(bars.pandas_df)
        except Exception as e:
            self.log_message(f"[{symbol}] Daily trend error: {e}")
            return None

    def _ema_trend_4h(self, htf):
        """4H EMA-stack trend read: 'bullish' / 'bearish' / 'neutral'. Reuses the 4H df
        already fetched for SMC (no extra API call). Neutral when there aren't enough
        4H bars for a stable EMA50 — so on thin data it simply doesn't veto."""
        try:
            df = (htf or {}).get("df")
            if df is None or len(df) < TREND_EMA_SLOW + 2:
                return "neutral"
            close = df["close"]
            ef = close.ewm(span=TREND_EMA_FAST, adjust=False).mean()
            es = close.ewm(span=TREND_EMA_SLOW, adjust=False).mean()
            c, efl, esl = float(close.iloc[-1]), float(ef.iloc[-1]), float(es.iloc[-1])
            es_prev = float(es.iloc[-6])
            if c > esl and efl > esl and esl >= es_prev:
                return "bullish"
            if c < esl and efl < esl and esl <= es_prev:
                return "bearish"
            return "neutral"
        except Exception:
            return "neutral"

    def _htf_trend(self, symbol, htf=None, daily_trend=None):
        """Combined higher-timeframe trend gate (Lever #1). Returns the direction a NEW
        trade is allowed to take — 'bullish', 'bearish', or 'neutral' (block both sides).

        The reliable daily EMA20/50 is the anchor (plentiful daily data): if it can't
        confirm a trend, we stand down — no more trading a BOS on a choppy tape. The 4H
        EMA stack can VETO (downgrade to neutral) if it actively opposes the daily trend,
        but never overrides it. This trades WITH the trend and never against it."""
        daily = daily_trend if daily_trend in ("bullish", "bearish") else None
        if daily is None:
            return "neutral"                       # no confirmed daily trend → don't trade
        h4 = self._ema_trend_4h(htf)
        if h4 != "neutral" and h4 != daily:
            return "neutral"                       # 4H opposes the daily trend → stand down
        return daily

    def get_htf_bias(self, symbol):
        asset = self._make_asset(symbol)
        try:
            bars    = self.get_historical_prices(asset, 200, self.timeframe_htf)
            bars_1h = self.get_historical_prices(asset, 200, "1 hour")
            if bars is None:
                return None
            df    = bars.pandas_df
            df_1h = bars_1h.pandas_df if bars_1h is not None else None

            is_consolidating = indicators.detect_consolidation(df, lookback=20)

            # Dual HTF BOS: 4H = full conviction, 1H = faster signal at half size
            is_bos_4h, direction_4h, bos_lvl_4h = indicators.detect_displacement_bos(df,    lookback=15)
            is_bos_1h, direction_1h, bos_lvl_1h = (
                indicators.detect_displacement_bos(df_1h, lookback=15)
                if df_1h is not None else (False, None, None)
            )
            is_bos    = is_bos_4h or is_bos_1h
            direction = direction_4h if is_bos_4h else direction_1h
            bos_tf    = "4H" if is_bos_4h else ("1H" if is_bos_1h else "—")
            bos_level = bos_lvl_4h  if is_bos_4h else bos_lvl_1h
            is_1h_only = is_bos_1h and not is_bos_4h

            # Multi-touch S/R clustering — only levels price has revisited ≥2×
            resistance, support = indicators.find_support_resistance(df, lookback=50)
            all_sr = resistance + support

            # Is the BOS displacement candle cutting through a known S/R level?
            bos_near_sr, bos_sr_level = (
                indicators.is_near_sr_level(bos_level, all_sr)
                if bos_level else (False, None)
            )

            is_sweep_htf, _, sweep_wick_htf = indicators.check_liquidity_sweep(df, sweep_window=5)

            # Full AMD cycle: accumulation box → manipulation spike → distribution
            amd_phase, amd_info = indicators.detect_amd_phase(df)

            # Flag and channel structure on the HTF chart
            bull_flag, *_bfd = indicators.detect_bull_flag(df)
            bear_flag, *_brd = indicators.detect_bear_flag(df)
            channel, channel_slope = indicators.detect_channel(df)

            # ATR for consolidation guard (stocks: 0.4% threshold vs crypto's 2.5%)
            atr_14  = (df['high'] - df['low']).rolling(14).mean().iloc[-1]
            atr_pct = float(atr_14 / df['close'].iloc[-1]) if df['close'].iloc[-1] else 0.0

            # 1H ATR — the noise floor for structural stops (a stop tighter than this sits
            # inside intraday noise on a liquid stock and gets tagged by a random wick).
            atr_1h = None
            if df_1h is not None and len(df_1h) >= 15:
                atr_1h = float((df_1h['high'] - df_1h['low']).rolling(14).mean().iloc[-1])

            return {
                "consolidating":  is_consolidating,
                "bos":            is_bos,
                "bos_4h":         is_bos_4h,
                "bos_1h":         is_bos_1h,
                "is_1h_only":     is_1h_only,
                "bos_tf":         bos_tf,
                "direction":      direction,
                "bos_level":      bos_level,
                "bos_near_sr":    bos_near_sr,
                "bos_sr_level":   bos_sr_level,
                "resistance":     resistance,
                "support":        support,
                "sweep_htf":      is_sweep_htf,
                "sweep_wick_htf": sweep_wick_htf,
                "amd_phase":      amd_phase,
                "amd_info":       amd_info,
                "bull_flag":      bull_flag,
                "bear_flag":      bear_flag,
                "channel":        channel,
                "channel_slope":  channel_slope,
                "atr_pct":        atr_pct,
                "atr_1h":         atr_1h,
                "df":             df,
            }
        except Exception as e:
            self.log_message(f"[{symbol}] HTF error: {e}")
            return None

    def get_ltf_technicals(self, symbol):
        asset = self._make_asset(symbol)
        try:
            bars = self.get_historical_prices(asset, 50, self.timeframe_ltf)
            if bars is None:
                return None
            df = bars.pandas_df
            is_sweep, support_level, sweep_wick_low = indicators.check_liquidity_sweep(df)
            is_mss, swing_high_broken = indicators.check_market_structure_shift(df)
            is_fvg_bull, fvg_bottom, fvg_top = indicators.find_bullish_fvg(df)
            is_fvg_bear, fvg_bear_bottom, fvg_bear_top = indicators.find_bearish_fvg(df)
            is_choch, choch_direction = indicators.detect_choch(df, lookback=5)
            return {
                "sweep": is_sweep, "sweep_wick_low": sweep_wick_low,
                "support_level": support_level,
                "mss": is_mss, "swing_high": swing_high_broken,
                "fvg_bull": is_fvg_bull, "fvg_bottom": fvg_bottom, "fvg_top": fvg_top,
                "fvg_bear": is_fvg_bear, "fvg_bear_bottom": fvg_bear_bottom,
                "fvg_bear_top": fvg_bear_top,
                "choch": is_choch, "choch_direction": choch_direction,
                "df": df,
            }
        except Exception as e:
            self.log_message(f"[{symbol}] LTF error: {e}")
            return None

    def _get_sentiment(self, symbol):
        """
        Returns (confirm: bool, label: str, prob: float).

        `confirm` is now ALWAYS True — news no longer vetoes anything here. It used to
        return `not (sentiment == "negative" and probability >= 0.60)`, which was
        direction-blind: strongly negative news blocked SHORTS as readily as longs, when
        bad news is an argument FOR a short. Per the 2026-08-13 policy decision, news is
        context handed to the AI (see get_ai_confirmation's news_block), and the AI —
        which already holds veto power over every entry — decides. The label/probability
        are still returned for logging so the read stays visible in the log.
        """
        try:
            news_items = self.get_news(symbol)
        except Exception:
            news_items = None

        if not news_items:
            return True, "no_news", 0.0

        try:
            probability, sentiment = estimate_sentiment(news_items)
        except Exception:
            return True, "no_news", 0.0
        return True, sentiment, probability

    def _reconcile_position(self, symbol, position, current_price=None):
        """Broker truth wins. If Alpaca shows a real position but our internal state
        doesn't (a close order that was submitted but never actually filled, a lost
        restart, or any other desync), _ensure_protection's safety net never runs
        again for this symbol — self.state gates it, and nothing else ever sets
        state back to POSITION_OPEN. That's exactly how the MSFT short sat naked
        for 6 days: the stale-exit path submitted a close order and reset state
        immediately without confirming the close filled. Adopt the orphaned
        position here so protection resumes within one iteration, not silently
        forever."""
        if self.state[symbol] == "POSITION_OPEN" or position is None or abs(position.quantity) < 1e-9:
            return
        is_long = position.quantity > 0
        # Lumibot's Position exposes avg_fill_price (mapped from Alpaca's raw
        # avg_entry_price field) and it can be None on a failed parse — fall back
        # to the live price so an orphan still gets adopted with SOME risk plan
        # rather than crashing the whole iteration and never being managed at all.
        raw_entry = (getattr(position, "avg_fill_price", None)
                     or getattr(position, "avg_entry_price", None)
                     or current_price)
        if not raw_entry:
            self.log_message(f"[{symbol}] ⚠️ Orphaned position found but no entry/live price "
                             f"available — retrying adoption next iteration.", color="red")
            return
        entry = float(raw_entry)
        self.bias[symbol]        = "BULLISH" if is_long else "BEARISH"
        self.entry_price[symbol] = entry
        self.entry_qty[symbol]   = abs(position.quantity)
        self.entry_time[symbol]  = datetime.now(timezone.utc)
        if not self.stop_loss[symbol] or not self.take_profit[symbol]:
            # No known risk plan for an adopted position. Previously fell back to a
            # blind 1%-of-price stop — completely disconnected from real volatility or
            # structure (the exact "338.31/328.26 looks arbitrary" complaint: GOOGL's
            # real ATR-based risk was ~$8, this fallback silently replaced it with a
            # flat $3.35 box). Compute a REAL structural stop the same way a fresh
            # entry does — floored at 1.5×1H-ATR — instead of an arbitrary percentage.
            # No known zone to anchor to (that's exactly what got lost), so the ATR
            # floor alone sets the distance; still far better than a blind guess.
            try:
                htf = self.get_htf_bias(symbol)
                _atr_1h = max(htf.get("atr_1h") or entry * 0.01, 0.01)
                sl = indicators.structural_stop_price(
                    entry, None, _atr_1h, is_long, MIN_STOP_ATR_MULT, None)
                risk_amt = abs(entry - sl)
                bias_str = "bullish" if is_long else "bearish"
                _min_tp_lvl = entry + MIN_TP_RR * risk_amt if is_long else entry - MIN_TP_RR * risk_amt
                pool_tp = indicators.find_next_liquidity_target(htf["df"], _min_tp_lvl, bias_str)
                tp = indicators.structural_take_profit(
                    entry, risk_amt, pool_tp, is_long, MIN_TP_RR, MAX_TP_RR)
                self.stop_loss[symbol]   = round(sl, 2)
                self.take_profit[symbol] = round(tp, 2)
            except Exception as e:
                # Real market data unavailable — fall back to the old blind 1% box
                # rather than leaving the position with no SL/TP at all.
                self.log_message(f"[{symbol}] ⚠️ Structural fallback failed ({e}) — "
                                 f"using blind 1% box as a last resort.", color="red")
                risk = entry * 0.01
                self.stop_loss[symbol]   = entry - risk if is_long else entry + risk
                self.take_profit[symbol] = entry + risk * MIN_AI_RR if is_long else entry - risk * MIN_AI_RR
        self.state[symbol] = "POSITION_OPEN"
        self.log_message(
            f"[{symbol}] ⚠️ Orphaned position detected — broker shows "
            f"{position.quantity} shares but internal state was out of sync. Adopting as "
            f"POSITION_OPEN (SL {self.stop_loss[symbol]:.2f} / TP {self.take_profit[symbol]:.2f}) "
            f"so protection resumes.",
            color="red"
        )

    def _process_symbol(self, symbol):
        current_price = self.get_last_price(symbol)
        if not current_price:
            return
        asset = self._make_asset(symbol)
        position = self.get_position(asset)
        self._reconcile_position(symbol, position, current_price)

        # ── STATE 4: POSITION_OPEN ─────────────────────────────────────────────
        # Check this first so we don't re-enter while a trade is live.
        if self.state[symbol] == "POSITION_OPEN":
            if position is None:
                # Re-verify with a FRESH read before trusting this — a single get_position()
                # miss (transient broker-side lag/hiccup) previously went straight to _reset(),
                # which unconditionally cancels the OCO/bracket legs. REAL INCIDENT 2026-07-28:
                # NVDA + GOOGL were still genuinely open (confirmed via Alpaca's real order
                # history — no close order was ever submitted in that window) but got stripped
                # of all protection anyway and sat naked for ~6 hours because this branch never
                # double-checked before tearing it down.
                position = self.get_position(asset)
                if position is not None:
                    self.log_message(
                        f"[{symbol}] Position read returned None once but is confirmed still "
                        f"OPEN on re-check — treating as a transient miss, NOT resetting.",
                        color="yellow")
                else:
                    # The position went flat without our Python SL/TP check firing — i.e.
                    # Alpaca's resting bracket/OCO leg filled server-side (the NORMAL way a
                    # stock trade closes). This path used to just reset, so those closes
                    # never reached the Stock Ledger — that's why it looked empty. Log the
                    # round trip here before cleaning up.
                    self._log_broker_side_close(symbol, current_price)
                    self.log_message(f"[{symbol}] Position closed (broker fill). Resetting.", color="cyan")
                    self._reset(symbol)
                    return

            is_long = self.bias[symbol] == "BULLISH"
            sl = self.stop_loss[symbol]
            tp = self.take_profit[symbol]

            # SAFETY NET: a live position must ALWAYS have a broker-side stop. If the protective
            # orders vanished (legs expired at the close, cancelled, or lost across a restart),
            # re-attach a fresh GTC OCO. Without this, a held position can sit naked overnight.
            self._ensure_protection(symbol, position, is_long, sl, tp)

            # Manual SL/TP exit — Python-side check so we don't rely on Alpaca bracket parsing
            hit = None
            if sl and tp:
                if is_long and current_price <= sl:
                    hit = "SL"
                elif is_long and current_price >= tp:
                    hit = "TP"
                elif not is_long and current_price >= sl:
                    hit = "SL"
                elif not is_long and current_price <= tp:
                    hit = "TP"

            if hit:
                close_qty  = abs(position.quantity)
                close_side = Order.OrderSide.SELL if is_long else Order.OrderSide.BUY
                submitted = self.submit_order(self.create_order(asset, close_qty, close_side))
                entry_p = self.entry_price[symbol] or current_price
                pnl = ((current_price - entry_p) * close_qty if is_long
                       else (entry_p - current_price) * close_qty)
                lev_note = ""
                if PAPER_LEVERAGE > 1:
                    margin = close_qty * entry_p / PAPER_LEVERAGE
                    if pnl < 0 and abs(pnl) >= margin:
                        lev_note = f"  ⚡ LIQUIDATED at {PAPER_LEVERAGE}x (loss > margin ${margin:,.0f})"
                    else:
                        lev_note = f"  [{PAPER_LEVERAGE}x leveraged — unlevered P&L: ${pnl/PAPER_LEVERAGE:+.2f}]"
                self.log_message(
                    f"[{symbol}] {'🟢' if pnl >= 0 else '🔴'} {hit} hit "
                    f"@ {current_price:.4f} | P&L: {pnl:+.2f}{lev_note}",
                    color="green" if pnl >= 0 else "red"
                )
                # Only wipe our tracking if the close order was actually accepted. If
                # submit_order returned nothing, the position is still open on the
                # broker — resetting here would strip its protective legs (_reset
                # cancels the OCO) while leaving it live, exactly how MSFT went naked
                # for 6 days. Leave state as POSITION_OPEN; _reconcile_position and
                # _ensure_protection keep it protected until this actually resolves.
                if submitted is not None:
                    self._log_trade_close_to_sheet(symbol, is_long, entry_p, current_price, close_qty, pnl)
                    self._reset(symbol)  # also cancels broker SL/TP orders + saves state
                else:
                    self.log_message(
                        f"[{symbol}] ⚠️ Close order submission failed — position still "
                        f"open, NOT resetting state (would strip its protection).",
                        color="red"
                    )
                return

            # Time-based exit: close stale trades after STALE_TRADE_HOURS
            if self.entry_time[symbol]:
                now = datetime.now(timezone.utc)
                entry = self.entry_time[symbol]
                if entry.tzinfo is None:
                    entry = entry.replace(tzinfo=timezone.utc)
                elapsed_hours = (now - entry).total_seconds() / 3600
                if elapsed_hours >= STALE_TRADE_HOURS:
                    close_qty  = abs(position.quantity)
                    close_side = Order.OrderSide.SELL if is_long else Order.OrderSide.BUY
                    submitted = self.submit_order(self.create_order(asset, close_qty, close_side))
                    entry_p = self.entry_price[symbol] or current_price
                    pnl = ((current_price - entry_p) * close_qty if is_long
                           else (entry_p - current_price) * close_qty)
                    self.log_message(
                        f"[{symbol}] ⏰ Stale exit after {elapsed_hours:.1f}h — closing position. "
                        f"P&L: {pnl:+.2f}",
                        color="red"
                    )
                    # Same guard as the manual SL/TP exit above — see comment there.
                    if submitted is not None:
                        # Explicit: a stale exit fills mid-range, so infer_exit_reason would
                        # correctly refuse to guess and return OTHER. We know better here.
                        self._log_trade_close_to_sheet(symbol, is_long, entry_p, current_price,
                                                        close_qty, pnl, exit_reason="STALE")
                        self._reset(symbol)
                    else:
                        self.log_message(
                            f"[{symbol}] ⚠️ Stale-exit close order submission failed — "
                            f"position still open, NOT resetting state.",
                            color="red"
                        )
            return

        # ── FETCH DATA ─────────────────────────────────────────────────────────
        htf = self.get_htf_bias(symbol)
        if htf is None:
            return
        ltf = self.get_ltf_technicals(symbol)
        if ltf is None:
            return
        daily_trend = self.get_daily_trend(symbol)

        zone_tag = ""
        if self.state[symbol] == "ENTRY_WAIT" and self.fvg_low[symbol] and self.fvg_high[symbol]:
            tag = "AMD" if self.amd_phase[symbol] else "FVG"
            in_zone = self.fvg_low[symbol] <= current_price <= self.fvg_high[symbol]
            zone_tag = (f"  [{tag} {self.fvg_low[symbol]:.2f}–{self.fvg_high[symbol]:.2f} "
                        f"{'✅IN' if in_zone else '⏳waiting'}]")
        candle_type = indicators.classify_candle(ltf["df"].iloc[-1], ltf["df"].iloc[-2])
        ch = htf.get("channel")
        struct_tag = (f"▲ch({ch})" if ch == "ascending"
                      else f"▼ch({ch})" if ch == "descending"
                      else ("🚩bull_flag" if htf.get("bull_flag")
                            else ("🚩bear_flag" if htf.get("bear_flag") else "—")))
        amd_tag = htf.get("amd_phase", "—")
        sr_tag  = (f"SR✅{htf['bos_sr_level']:.2f}" if htf.get("bos_near_sr")
                   else f"res={htf['resistance'][0]:.2f}" if htf.get("resistance") else "—")
        self.log_message(
            f"[{symbol}] state={self.state[symbol]} price={current_price:.4f} "
            f"daily={daily_trend} bos={htf['bos']}({htf['direction']}/{htf['bos_tf']}) "
            f"amd={amd_tag}  struct={struct_tag}  sr={sr_tag}  "
            f"sweep={ltf['sweep']} fvg_bull={ltf['fvg_bull']} fvg_bear={ltf['fvg_bear']}  "
            f"candle={candle_type}"
            f"{zone_tag}"
        )

        # ── STATE 1: IDLE — daily trend + 4H BOS must agree ──────────────────────
        AMD_ENTRY_WAIT_ITERS = 96  # AMD zones can take many iterations to reach
        MIN_ATR_PCT = 0.004       # 0.4% 4H ATR floor for stocks — filters flat/dead days
        if self.state[symbol] == "IDLE" and position is None:
            atr_pct = htf.get("atr_pct", 1.0)

            # ── FIX #2: Accumulation guard runs FIRST, blocking every path below ──────
            # A tight HTF range = chop / institutions accumulating. Don't trade either
            # side until it breaks. Without this first, a BOS printed inside the range
            # whipsaws us. Log the range and bail for this iteration.
            if htf.get("amd_phase") == "accumulation":
                ai = htf.get("amd_info", {})
                self.log_message(
                    f"[{symbol}] 📦 Accumulation: {ai.get('range_low', 0):.2f}–"
                    f"{ai.get('range_high', 0):.2f} ({ai.get('range_pct', 0):.1%} wide) "
                    f"— chop, no entry until breakout/sweep", color="cyan"
                )
                return

            # ── FIX #1: BOS handling — trade the displacement retest, don't demand a sweep ──
            # When a BOS leaves a displacement FVG, lock that gap and go straight to
            # ENTRY_WAIT for the retest. This is how clean breakdowns/breakouts get traded:
            # an impulsive move has no opposing sweep, so requiring one made the bot blind
            # to its best continuation setups. Fall back to SWEEP_HUNT only when no gap.
            bos_aligned = htf["bos"] and htf["direction"] and (
                daily_trend == htf["direction"] or daily_trend is None
            )
            bos_counter = (htf["bos"] and htf["direction"] and daily_trend
                           and daily_trend != htf["direction"])

            if bos_aligned and atr_pct < MIN_ATR_PCT:
                self.log_message(
                    f"[{symbol}] BOS skip — flat market (ATR={atr_pct:.2%} < {MIN_ATR_PCT:.2%})",
                    color="yellow"
                )
            elif bos_aligned:
                direction = htf["direction"]
                self.bias[symbol]         = direction.upper()
                self.ranging_mode[symbol] = (daily_trend is None) or htf["is_1h_only"]
                size_tag = "HALF" if self.ranging_mode[symbol] else "FULL"
                sr_conf  = (f"  ✅ cuts S/R @ {htf['bos_sr_level']:.2f}"
                            if htf.get("bos_near_sr") else "")
                ch_tag   = f"  [{htf['channel']} channel]" if htf.get("channel") else ""
                color    = "green" if direction == "bullish" else "red"
                d_found, d_dir, d_lo, d_hi, _ = indicators.detect_displacement_fvg(
                    htf["df"],
                    **indicators.displacement_gates(htf["df"], DISPLACEMENT_ATR_MULT,
                                                    DISPLACEMENT_MIN_PCT, MIN_FVG_PCT))
                if d_found and d_dir == direction:
                    self.fvg_low[symbol]       = d_lo
                    self.fvg_high[symbol]      = d_hi
                    self.amd_zone_type[symbol] = "choch_fvg"     # displacement gap = the CHoCH zone
                    self.state[symbol]         = "ENTRY_WAIT"
                    self.fvg_set_iter[symbol]  = self._iter_count
                    self.log_message(
                        f"[{symbol}] STEP 1+2: {htf['bos_tf']} BOS ({direction}) + displacement FVG "
                        f"{d_lo:.2f}–{d_hi:.2f} → ENTRY_WAIT retest [{size_tag}]{sr_conf}{ch_tag}",
                        color=color
                    )
                else:
                    self.state[symbol]           = "SWEEP_HUNT"
                    self.sweep_hunt_iter[symbol] = self._iter_count
                    self.log_message(
                        f"[{symbol}] STEP 1: {htf['bos_tf']} BOS ({direction}) → "
                        f"hunting sweep [{size_tag}]{sr_conf}{ch_tag}", color=color
                    )
            elif bos_counter:
                self.log_message(
                    f"[{symbol}] {htf['bos_tf']} BOS {htf['direction']} skipped — "
                    f"daily trend is {daily_trend}.", color="yellow"
                )

            # ── AMD cycle: use detect_amd_phase() for full A→M→D awareness ────────
            # (Accumulation already filtered at the top of IDLE — returns early — so any
            #  symbol reaching here is NOT in a tight range.)
            if self.state[symbol] == "IDLE":
                amd_phase = htf.get("amd_phase", "unknown")
                amd_info  = htf.get("amd_info", {})

                if atr_pct >= MIN_ATR_PCT:
                    # manipulation_up = stop hunt of lows completed, price bleeding up
                    # → real move will be DOWN — SHORT from supply zone above
                    if amd_phase == "manipulation_up" and daily_trend == "bearish":
                        found, sup_lo, sup_hi, sup_type = indicators.find_supply_zone(
                            htf["df"], current_price
                        )
                        if found:
                            self.bias[symbol]          = "BEARISH"
                            self.sweep_low[symbol]     = amd_info.get("sweep_wick", htf.get("sweep_wick_htf"))
                            self.amd_phase[symbol]     = "manipulation_up"
                            self.amd_zone_type[symbol] = sup_type
                            self.fvg_low[symbol]       = sup_lo
                            self.fvg_high[symbol]      = sup_hi
                            self.ranging_mode[symbol]  = False
                            self.state[symbol]         = "ENTRY_WAIT"
                            self.fvg_set_iter[symbol]  = self._iter_count
                            self.log_message(
                                f"[{symbol}] 🎯 AMD manipulation_up: swept ${amd_info.get('swept_level', 0):.2f} → "
                                f"SHORT from [{sup_type}] {sup_lo:.2f}–{sup_hi:.2f}  "
                                f"target≈${amd_info.get('manipulation_target', 0):.2f}",
                                color="red"
                            )

                    # manipulation_down = stop hunt of highs completed, price pushed down
                    # → real move will be UP — LONG from demand zone below
                    elif amd_phase == "manipulation_down" and daily_trend == "bullish":
                        found, dem_lo, dem_hi, dem_type = indicators.find_demand_zone(
                            htf["df"], current_price
                        )
                        if found:
                            self.bias[symbol]          = "BULLISH"
                            self.sweep_low[symbol]     = amd_info.get("sweep_wick", htf.get("sweep_wick_htf"))
                            self.amd_phase[symbol]     = "manipulation_down"
                            self.amd_zone_type[symbol] = dem_type
                            self.fvg_low[symbol]       = dem_lo
                            self.fvg_high[symbol]      = dem_hi
                            self.ranging_mode[symbol]  = False
                            self.state[symbol]         = "ENTRY_WAIT"
                            self.fvg_set_iter[symbol]  = self._iter_count
                            self.log_message(
                                f"[{symbol}] 🎯 AMD manipulation_down: swept ${amd_info.get('swept_level', 0):.2f} → "
                                f"LONG from [{dem_type}] {dem_lo:.2f}–{dem_hi:.2f}  "
                                f"target≈${amd_info.get('manipulation_target', 0):.2f}",
                                color="green"
                            )

            # ── Trend-zone fallback: daily trend clear but no fresh BOS/sweep ──
            if self.state[symbol] == "IDLE":
                if atr_pct >= MIN_ATR_PCT:
                    if daily_trend == "bullish":
                        found, dem_lo, dem_hi, dem_type = indicators.find_demand_zone(
                            htf["df"], current_price, max_distance_pct=0.08
                        )
                        if found:
                            self.bias[symbol]            = "BULLISH"
                            self.amd_phase[symbol]       = "trend_follow"
                            self.amd_zone_type[symbol]   = dem_type
                            self.fvg_low[symbol]         = dem_lo
                            self.fvg_high[symbol]        = dem_hi
                            self.ranging_mode[symbol]    = False
                            self.state[symbol]           = "ENTRY_WAIT"
                            self.fvg_set_iter[symbol]    = self._iter_count
                            self.zone_set_price[symbol]  = current_price
                            self.log_message(
                                f"[{symbol}] 📊 Trend zone: daily BULLISH → [{dem_type}] "
                                f"{dem_lo:.2f}–{dem_hi:.2f} → LONG on pullback", color="green"
                            )
                    elif daily_trend == "bearish":
                        found, sup_lo, sup_hi, sup_type = indicators.find_supply_zone(
                            htf["df"], current_price, max_distance_pct=0.08
                        )
                        if found:
                            self.bias[symbol]            = "BEARISH"
                            self.amd_phase[symbol]       = "trend_follow"
                            self.amd_zone_type[symbol]   = sup_type
                            self.fvg_low[symbol]         = sup_lo
                            self.fvg_high[symbol]        = sup_hi
                            self.ranging_mode[symbol]    = False
                            self.state[symbol]           = "ENTRY_WAIT"
                            self.fvg_set_iter[symbol]    = self._iter_count
                            self.zone_set_price[symbol]  = current_price
                            self.log_message(
                                f"[{symbol}] 📊 Trend zone: daily BEARISH → [{sup_type}] "
                                f"{sup_lo:.2f}–{sup_hi:.2f} → SHORT on rally", color="red"
                            )

        # ── STATE 2: SWEEP_HUNT ───────────────────────────────────────────────────
        # Real SMC discipline: require a liquidity sweep before looking for an FVG.
        # After 30 iterations (~30 min) without a sweep the BOS signal is stale — reset.
        SWEEP_PATIENCE = 30
        if self.state[symbol] == "SWEEP_HUNT" and position is None:
            iters_hunting = self._iter_count - self.sweep_hunt_iter[symbol]

            # Re-check chop WHILE hunting. These guards ran only from IDLE (line ~1391),
            # so once a symbol left IDLE it was never re-checked: it rode a days-old
            # directional read into a flat tape and took the retest late — the "only
            # enters after the big move" complaint. htf already carries both readings, so
            # this costs nothing. Bail for this iteration rather than resetting; the sweep
            # may still come, and sweep_hunt_expired() owns the age question.
            if htf.get("amd_phase") == "accumulation":
                if iters_hunting % 12 == 1:          # periodic, not every iteration
                    self.log_message(
                        f"[{symbol}] 📦 Still hunting but HTF is accumulating — "
                        f"not arming into chop.", color="yellow")
                return
            if htf.get("atr_pct", 1.0) < MIN_ATR_PCT:
                if iters_hunting % 12 == 1:
                    self.log_message(
                        f"[{symbol}] ⏸ Still hunting but 4H ATR "
                        f"{htf.get('atr_pct', 0):.2%} < {MIN_ATR_PCT:.2%} floor — "
                        f"market too dead to arm a zone.", color="yellow")
                return

            if ltf["sweep"] and ltf["sweep_wick_low"]:
                self.sweep_low[symbol] = ltf["sweep_wick_low"]
                self.log_message(
                    f"[{symbol}] Sweep detected @ {ltf['sweep_wick_low']:.4f} — now hunting FVG/OB.",
                    color="cyan"
                )

            # Expire the BOS signal if no sweep in SWEEP_PATIENCE iterations
            # `and not self.sweep_low[symbol]` made this unreachable: sweep_low is set
            # on the FIRST sweep and only cleared on reset, so one sweep disabled the
            # expiry forever. Same defect as binance_bot.py — on the crypto side BTC then
            # held SWEEP_HUNT for ~62h on a three-day-old BOS. Two limits now: no sweep
            # after SWEEP_PATIENCE, and a hard ceiling at 2x regardless.
            _expired, _why = indicators.sweep_hunt_expired(
                iters_hunting, SWEEP_PATIENCE, bool(self.sweep_low[symbol]))
            if _expired:
                self.log_message(f"[{symbol}] 💀 {_why}. Resetting to IDLE.", color="yellow")
                self._reset(symbol)
                return

            # Only proceed to ENTRY_WAIT once a sweep has been recorded
            if not self.sweep_low[symbol]:
                return

            # Prefer the post-sweep displacement FVG (the CHoCH gap) — that's the gap the
            # reversal left behind. Fall back to a generic FVG/OB only with no clean gap.
            want = "bullish" if self.bias[symbol] == "BULLISH" else "bearish"
            d_found, d_dir, d_lo, d_hi, _ = indicators.detect_displacement_fvg(
                ltf["df"],
                **indicators.displacement_gates(ltf["df"], DISPLACEMENT_ATR_MULT,
                                                DISPLACEMENT_MIN_PCT, MIN_FVG_PCT))
            if d_found and d_dir == want:
                self.fvg_low[symbol]       = d_lo
                self.fvg_high[symbol]      = d_hi
                self.amd_zone_type[symbol] = "choch_fvg"
                self.fvg_set_iter[symbol]  = self._iter_count
                self.state[symbol]         = "ENTRY_WAIT"
                self.log_message(
                    f"[{symbol}] STEP 2: 🎯 CHoCH FVG (displacement) locked | "
                    f"{d_lo:.4f}-{d_hi:.4f} | sweep={self.sweep_low[symbol]:.4f}",
                    color="green" if want == "bullish" else "red"
                )

            elif self.bias[symbol] == "BULLISH" and ltf["fvg_bull"]:
                self.fvg_low[symbol]      = ltf["fvg_bottom"]
                self.fvg_high[symbol]     = ltf["fvg_top"]
                self.fvg_set_iter[symbol] = self._iter_count
                self.state[symbol]        = "ENTRY_WAIT"
                self.log_message(
                    f"[{symbol}] STEP 2: Bullish OB/FVG locked | "
                    f"{self.fvg_low[symbol]:.4f}-{self.fvg_high[symbol]:.4f} | "
                    f"sweep={self.sweep_low[symbol]:.4f}  mss={ltf['mss']}",
                    color="green"
                )

            elif self.bias[symbol] == "BEARISH" and ltf["fvg_bear"]:
                self.fvg_low[symbol]      = ltf["fvg_bear_bottom"]
                self.fvg_high[symbol]     = ltf["fvg_bear_top"]
                self.fvg_set_iter[symbol] = self._iter_count
                self.state[symbol]        = "ENTRY_WAIT"
                self.log_message(
                    f"[{symbol}] STEP 2: Bearish OB/FVG locked | "
                    f"{self.fvg_low[symbol]:.4f}-{self.fvg_high[symbol]:.4f} | "
                    f"sweep={self.sweep_low[symbol]:.4f}  mss={ltf['mss']}",
                    color="red"
                )

        # ── STATE 3: ENTRY_WAIT ────────────────────────────────────────────────
        FVG_EXPIRY_ITERS = 20   # standard FVG: ~20 iterations
        AMD_ENTRY_WAIT_ITERS = 96  # AMD zone: up to 8h for price to reach supply/demand
        if self.state[symbol] == "ENTRY_WAIT" and position is None:
            expiry = AMD_ENTRY_WAIT_ITERS if self.amd_phase[symbol] else FVG_EXPIRY_ITERS
            if self._iter_count - self.fvg_set_iter[symbol] > expiry:
                tag = "AMD zone" if self.amd_phase[symbol] else "FVG"
                self.log_message(
                    f"[{symbol}] {tag} expired after {expiry} iters — resetting to IDLE.",
                    color="yellow"
                )
                self._reset(symbol)
                return

            # Stale trend-zone invalidation: a "trend_follow" zone is a wait-for-the-pullback
            # setup. Abandon it only if price RUNS AWAY ≥1.5% in our direction from where the
            # zone was armed (the move left without us). Measured vs the arm-price, NOT a
            # displacement — a trend always has a recent displacement, which would abandon the
            # zone the instant it's armed (an infinite set/abandon thrash).
            zsp = self.zone_set_price[symbol]
            if self.amd_phase[symbol] == "trend_follow" and zsp:
                bias = self.bias[symbol]
                ran_away = ((bias == "BEARISH" and current_price < zsp * 0.985) or
                            (bias == "BULLISH" and current_price > zsp * 1.015))
                if ran_away:
                    moved = abs(current_price - zsp) / zsp
                    self.log_message(
                        f"[{symbol}] ⚠️ Trend zone abandoned — price ran {moved:.1%} from setup "
                        f"({zsp:.2f}→{current_price:.2f}) without tapping; re-hunting.", color="yellow"
                    )
                    self._reset(symbol)
                    return

            in_fvg = (self.fvg_low[symbol] is not None and
                      self.fvg_high[symbol] is not None and
                      self.fvg_low[symbol] <= current_price <= self.fvg_high[symbol])

            if in_fvg:
                # ATR gate at execution: skip if market gone dead since zone was set
                entry_atr_pct = htf.get("atr_pct", 1.0)
                if entry_atr_pct < 0.004:
                    self.log_message(
                        f"[{symbol}] ⏸ Entry skipped — flat at tap "
                        f"(ATR={entry_atr_pct:.2%} < 0.4%)", color="yellow"
                    )
                    return

                # ── Reversal veto: never fade a fresh opposite-side reversal ──────────
                # Same Debbie-La rule as the crypto bot: a swept LOW that gets reclaimed hard
                # is a bullish reversal (don't short it); a swept HIGH that flushes is bearish.
                # The reclaim threshold scales with the instrument's ATR — stocks travel far
                # less than crypto, so a fixed % would never trip here. Stand aside if price
                # has V-recovered off the window extreme and now sits near the opposite end.
                _is_long = self.bias[symbol] == "BULLISH"
                _rev = ltf["df"].tail(48)
                _wlo, _whi = float(_rev["low"].min()), float(_rev["high"].max())
                _rng = _whi - _wlo
                _thresh = max(0.012, 2.5 * entry_atr_pct)   # ≥1.2% or 2.5×ATR
                if _rng > 0 and not _is_long:
                    _age = len(_rev) - 1 - int(_rev["low"].values.argmin())
                    _rec = (current_price - _wlo) / _wlo
                    if _rec >= _thresh and (current_price - _wlo) / _rng >= 0.55 and _age >= 6:
                        self.log_message(
                            f"[{symbol}] 🚫 SHORT vetoed — price V-recovered {_rec:+.1%} off swept "
                            f"low {_wlo:.2f} and sits near the highs = bullish reversal; standing aside.",
                            color="yellow")
                        return
                elif _rng > 0 and _is_long:
                    _age = len(_rev) - 1 - int(_rev["high"].values.argmax())
                    _drop = (_whi - current_price) / _whi
                    if _drop >= _thresh and (_whi - current_price) / _rng >= 0.55 and _age >= 6:
                        self.log_message(
                            f"[{symbol}] 🚫 LONG vetoed — price V-dropped {_drop:+.1%} off swept "
                            f"high {_whi:.2f} and sits near the lows = bearish reversal; standing aside.",
                            color="yellow")
                        return

                # Candle pattern confirmation (informational — AI still decides)
                tap_candle = indicators.classify_candle(ltf["df"].iloc[-1], ltf["df"].iloc[-2])
                confirms   = indicators.candle_confirms_bias(tap_candle, self.bias[symbol])
                self.log_message(
                    f"[{symbol}] 🕯 Zone tap — candle: "
                    f"{'✅' if confirms else '⚠️'} {tap_candle}",
                    color="cyan"
                )

                # ── Fresh-momentum gate at the tap (added 2026-08-22) ─────────────────
                # binance_bot has demanded this since the 'armed long ago, entered on
                # nothing' incidents; the stock bot never had it. Nothing here rechecked
                # that the bar actually filling the zone is decisive, so a zone armed on
                # thin structure could fill into chop with no opposition. Same thresholds
                # as the crypto path: body >= 50% of range AND >= 0.6x ATR of this
                # timeframe, in the trade direction, within the last 3 bars.
                try:
                    _ltf_atr  = indicators.range_atr(ltf["df"])
                    _ltf_px   = float(ltf["df"]["close"].iloc[-1])
                    _fresh    = indicators.has_displacement(
                        ltf["df"].tail(3)[["open", "high", "low", "close"]].values.tolist(),
                        is_long=_is_long,
                        min_body_frac=DISPLACEMENT_BODY_FRAC,
                        min_body_abs=indicators.displacement_min_body(
                            _ltf_atr, _ltf_px, DISPLACEMENT_ATR_MULT, DISPLACEMENT_MIN_PCT))
                except Exception as _e:
                    # Fail CLOSED, matching the crypto sniper: an unverifiable momentum
                    # read must not become a free pass to enter.
                    self.log_message(f"[{symbol}] ⚠️ Could not verify tap momentum ({_e}) "
                                     f"— standing aside.", color="red")
                    return
                if not _fresh:
                    self.log_message(
                        f"[{symbol}] 🚫 No fresh displacement at tap — needs a body ≥"
                        f"{DISPLACEMENT_BODY_FRAC:.0%} of range AND ≥{DISPLACEMENT_ATR_MULT}×ATR "
                        f"(${indicators.displacement_min_body(_ltf_atr, _ltf_px, DISPLACEMENT_ATR_MULT, DISPLACEMENT_MIN_PCT):.2f})"
                        f" in the last 3 bars. Standing aside.",
                        color="yellow")
                    return

                # S/R confluence at the entry zone
                sr_near, sr_level = indicators.is_near_sr_level(
                    current_price,
                    htf.get("resistance", []) + htf.get("support", [])
                )
                if sr_near:
                    self.log_message(
                        f"[{symbol}] 📊 S/R confluence at zone tap: {sr_level:.2f}", color="cyan"
                    )
                else:
                    self.log_message(
                        f"[{symbol}] ⚠️ No S/R confluence at tap (nearest S/R may be far)", color="yellow"
                    )

                # Informational only — this read no longer gates anything (see
                # _get_sentiment). The actual news weighing happens inside
                # get_ai_confirmation, which receives the headlines as context.
                confirm, sentiment, prob = self._get_sentiment(symbol)
                if prob > 0:
                    self.log_message(
                        f"[{symbol}] 📰 FinBERT: {sentiment.upper()} ({prob*100:.1f}%) "
                        f"— context for AI, not a gate",
                        color="magenta"
                    )
                else:
                    self.log_message(f"[{symbol}] 📰 No news — proceeding on technicals.", color="magenta")

                if confirm:
                    is_long = self.bias[symbol] == "BULLISH"
                    side    = Order.OrderSide.BUY if is_long else Order.OrderSide.SELL

                    # ── HTF TREND FILTER (Lever #1) — trade WITH the trend, never against ──
                    # Longs only in a confirmed uptrend, shorts only in a downtrend. This is
                    # the single gate every entry path funnels through (BOS, AMD, trend-zone),
                    # so it can't be bypassed. It closes the loophole that let a BOS long
                    # through on a no-trend tape (the QQQ entry). Placed before SL/AI work so
                    # a counter-trend setup is dropped cheaply.
                    _need  = "bullish" if is_long else "bearish"
                    _trend = self._htf_trend(symbol, htf, daily_trend)
                    if _trend != _need:
                        self.log_message(
                            f"[{symbol}] 🚫 Trend filter: {'LONG' if is_long else 'SHORT'} "
                            f"blocked — HTF trend is {_trend.upper()} (need {_need.upper()}). "
                            f"Not fighting the tape.", color="yellow")
                        return

                    # ── STRUCTURAL STOP ────────────────────────────────────────────
                    # Anchor to the FVG/OB invalidation, but keep the stop OUTSIDE 1H-ATR
                    # noise so a single 5m/15m wick can't tag it. (The OLD code *capped*
                    # the stop at 3×15m ATR, which crushed it onto liquid stocks — e.g.
                    # NVDA to ~$1 — right into the noise. structural_stop_price FLOORS it
                    # instead.) Still capped at 90% of the liquidation distance.
                    _atr_1h = htf.get("atr_1h") or (
                        2.0 * float((ltf["df"]["high"] - ltf["df"]["low"]).rolling(14).mean().iloc[-1]))
                    _atr_1h = max(_atr_1h, 0.01)
                    _zone = ((self.fvg_low[symbol] * 0.997) if (is_long and self.fvg_low[symbol])
                             else (self.fvg_high[symbol] * 1.003) if (not is_long and self.fvg_high[symbol])
                             else None)
                    _liq_cap = (current_price / PAPER_LEVERAGE) * 0.90 if PAPER_LEVERAGE > 1 else None
                    # Anchor to the ZONE EDGE, not the live price. structural_stop_price
                    # returns `entry - dist`, so passing current_price let the stop SLIDE
                    # ALONG WITH THE FILL — risk stayed pinned near the ATR floor however
                    # far past the zone price had run, and R:R became incapable of
                    # degrading with fill quality. Measured on the crypto side, same zone /
                    # same fill / same target: 1:1.69 refused by one path, 1:2.20 taken by
                    # the other. Risk is still measured from the ACTUAL fill below, so a
                    # worse fill now costs more risk instead of dragging the stop behind it.
                    _anchor = (self.fvg_low[symbol] if (is_long and self.fvg_low[symbol])
                               else self.fvg_high[symbol] if (not is_long and self.fvg_high[symbol])
                               else current_price)
                    sl = round(indicators.structural_stop_price(
                        _anchor, _zone, _atr_1h, is_long, MIN_STOP_ATR_MULT, _liq_cap), 2)
                    risk_amt = abs(current_price - sl)
                    bias_str = "bullish" if is_long else "bearish"

                    # ── STRUCTURAL TARGET ──────────────────────────────────────────
                    # Pin TP to the nearest 4H liquidity pool that offers at least
                    # MIN_TP_RR (a REAL breaker/pool, not a multiple of a tiny stop),
                    # capped at MAX_TP_RR so it stays reachable before the EOD flatten.
                    _min_tp_lvl = (current_price + MIN_TP_RR * risk_amt if is_long
                                   else current_price - MIN_TP_RR * risk_amt)
                    pool_tp = indicators.find_next_liquidity_target(htf["df"], _min_tp_lvl, bias_str)
                    tp_planned = round(indicators.structural_take_profit(
                        current_price, risk_amt, pool_tp, is_long, MIN_TP_RR, MAX_TP_RR), 2)
                    # Reachability: MAX_TP_RR caps the target in units of RISK, which says
                    # nothing about whether price can TRAVEL that far before the position is
                    # force-closed (STALE_TRADE_HOURS, or the EOD flatten, whichever lands
                    # first). A wide stop turns a "4R cap" into a distance no session can
                    # cover, and the trade can then only exit on the clock while showing a
                    # flattering R:R on entry. Caught on the crypto side (POL/USD 2026-09-04,
                    # target 13.3% away vs a 2.755% HTF ATR); same exposure existed here.
                    _htf_atr_reach = indicators.range_atr(htf["df"])
                    tp_planned, _rr_reach, _reach_ok = indicators.reachable_target(
                        current_price, sl, tp_planned, _htf_atr_reach,
                        MAX_TARGET_ATR_MULT, MIN_TP_RR)
                    tp_planned = round(tp_planned, 2)
                    if not _reach_ok:
                        self.log_message(
                            f"[{symbol}] 🚫 Reachability gate — only "
                            f"{MAX_TARGET_ATR_MULT:g}× the HTF ATR (${_htf_atr_reach:.2f}) is "
                            f"reachable before the close, paying 1:{_rr_reach:.1f} "
                            f"< 1:{MIN_TP_RR:g}. Standing aside.", color="yellow")
                        return
                    rr_actual = abs(tp_planned - current_price) / risk_amt if risk_amt else 0.0
                    _tp_src = "@4H pool" if (pool_tp and abs(tp_planned - pool_tp) < 0.01) else f"{rr_actual:.1f}R cap"
                    self.log_message(
                        f"[{symbol}] 🎯 Structural: SL ${sl:.2f} (risk {risk_amt:.2f} = "
                        f"{risk_amt / _atr_1h:.1f}×1hATR) → TP ${tp_planned:.2f} [{_tp_src}] "
                        f"| R:R 1:{rr_actual:.1f}", color="cyan")

                    ai_confirm, _ai_rr, ai_reason = get_ai_confirmation(
                        symbol, current_price, daily_trend, bias_str,
                        self.fvg_low[symbol], self.fvg_high[symbol],
                        self.sweep_low[symbol] or 0, sl, risk_amt, pool_tp,
                        ltf["df"], htf["df"],
                        amd_phase=self.amd_phase[symbol],
                        zone_type=self.amd_zone_type[symbol],
                    )
                    self.log_message(
                        f"[{symbol}] 🤖 AI Bot Approval: {'✅ YES' if ai_confirm else '❌ NO'}  "
                        f"R:R=1:{rr_actual:.1f}  {ai_reason}",
                        color="magenta"
                    )

                    if ai_confirm:
                        # ── Close-of-session cutoff ────────────────────────────────
                        # Don't open a NEW position in the last ENTRY_CUTOFF_MIN minutes.
                        # before_closing_bell can't reliably flatten a 3:59 PM fill, and a
                        # position carried into the close rides straight into the overnight
                        # gap (how QQQ was held 7/22→7/23). Managing/exiting live positions
                        # is unaffected — this only blocks fresh entries.
                        _mtc = self._minutes_to_close()
                        if _mtc is not None and _mtc <= ENTRY_CUTOFF_MIN:
                            self.log_message(
                                f"[{symbol}] ⏰ {_mtc:.0f}m to close (≤{ENTRY_CUTOFF_MIN}m) — "
                                f"skipping new entry to avoid an overnight-gap hold.",
                                color="yellow")
                            return

                        cash, last_price, quantity = self.position_sizing(symbol, sl_price=sl)
                        if self.ranging_mode[symbol]:
                            quantity = max(1, round(quantity * 0.5))

                        if quantity > 0 and cash > last_price:
                            # Final guard: never stack onto an existing position. Covers a
                            # stale/None get_position from the top of the iteration, or a
                            # prior fill Alpaca hasn't reflected during the ~15s AI call.
                            # If we can't verify the position, we do NOT enter.
                            try:
                                live = self.get_position(asset)
                            except Exception as e:
                                self.log_message(
                                    f"[{symbol}] ⛔ Entry aborted — couldn't verify position ({e}).",
                                    color="red"
                                )
                                return
                            if live is not None and abs(live.quantity) > 0:
                                self.log_message(
                                    f"[{symbol}] ⛔ Entry aborted — position already open "
                                    f"({live.quantity}). Syncing to POSITION_OPEN.", color="red"
                                )
                                self.state[symbol] = "POSITION_OPEN"
                                self._save_state()
                                return

                            self.stop_loss[symbol]   = sl
                            self.take_profit[symbol] = tp_planned   # structural target (nearest 4H pool, ≤MAX_TP_RR)
                            tp = self.take_profit[symbol]

                            # Try a BRACKET order first — Alpaca/TradingView render its legs
                            # as a green TP / red SL pair with proper labels. If Alpaca rejects
                            # it (wash-trade, etc.), fall back to a plain market entry whose OCO
                            # is attached in on_filled_order. Either way the trade gets in.
                            # TIF=day is fine: the bot flattens at the close (before_closing_bell),
                            # so the protective legs only need to last the session.
                            self.bracket_active[symbol] = False
                            _is_crypto = self._make_asset(symbol).asset_type == Asset.AssetType.CRYPTO
                            try:
                                import requests as _req
                                _resp = _req.post(
                                    f"{ALPACA_BASE_URL}/v2/orders",
                                    json={
                                        "symbol":        symbol,
                                        "qty":           str(int(quantity)) if not _is_crypto else str(quantity),
                                        "side":          "buy" if is_long else "sell",
                                        "type":          "market",
                                        "time_in_force": "gtc",   # GTC so the SL/TP legs DON'T expire at the close (a held position must stay protected overnight)
                                        "order_class":   "bracket",
                                        "take_profit":   {"limit_price": str(round(tp, 2) if not _is_crypto else tp)},
                                        # Crypto rejects a bare stop_price leg (422 "invalid order
                                        # type for crypto order") — real incident: BTCUSD never had
                                        # a stop-loss since its first fill because of exactly this,
                                        # silently caught below and falling back to a naked entry.
                                        "stop_loss":     indicators.build_protective_leg(
                                                              round(sl, 2) if not _is_crypto else sl,
                                                              _is_crypto, is_long),
                                    },
                                    headers={
                                        "APCA-API-KEY-ID":     ALPACA_API_KEY,
                                        "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
                                    },
                                    timeout=10,
                                )
                                _resp.raise_for_status()
                                data = _resp.json() if _resp.content else {}
                                ids  = []
                                if isinstance(data, dict):
                                    if data.get("id"):
                                        ids.append(data["id"])
                                    for leg in (data.get("legs") or []):
                                        if isinstance(leg, dict) and leg.get("id"):
                                            ids.append(leg["id"])
                                self.oco_ids[symbol]        = ids
                                self.bracket_active[symbol] = True
                                _bkt_val = int(quantity) * current_price
                                _bkt_mgn = _bkt_val / PAPER_LEVERAGE
                                self.log_message(
                                    f"[{symbol}] 🎯 BRACKET entry ({int(quantity)} sh) | "
                                    f"margin=${_bkt_mgn:,.2f} → controls ${_bkt_val:,.2f} | "
                                    f"TP {tp:.2f} / SL {sl:.2f} | R:R 1:{rr_actual:.1f}", color="green"
                                )
                            except Exception as e:
                                # The POST may have raised AFTER Alpaca accepted it (read
                                # timeout / reset / bad body). Blindly re-sending here is how
                                # two MSFT entries landed at the same price on 2026-08-14.
                                if self._order_reached_broker(symbol):
                                    self.log_message(
                                        f"[{symbol}] ⚠️ Bracket POST raised ({e}) but the order "
                                        f"IS live at Alpaca — NOT sending a duplicate. Syncing to "
                                        f"POSITION_OPEN; _ensure_protection will attach the stop.",
                                        color="red"
                                    )
                                else:
                                    # Broker is provably clean — safe to place the entry.
                                    self.log_message(
                                        f"[{symbol}] Bracket rejected ({e}) — falling back to market + OCO",
                                        color="yellow"
                                    )
                                    order = self.create_order(asset, quantity, side)
                                    self.entry_order[symbol] = order
                                    self.submit_order(order)

                            self.state[symbol]       = "POSITION_OPEN"
                            self.entry_price[symbol] = current_price
                            self.entry_qty[symbol]   = quantity
                            self.entry_time[symbol]  = datetime.now(timezone.utc)
                            self._save_state()

                            pos_value = quantity * current_price
                            margin    = pos_value / PAPER_LEVERAGE
                            if PAPER_LEVERAGE > 1:
                                liq_price = (current_price * (1 - 1/PAPER_LEVERAGE) if is_long
                                             else current_price * (1 + 1/PAPER_LEVERAGE))
                                lev_line = (f"\n           💹 {PAPER_LEVERAGE}x LEVERAGE  "
                                            f"margin=${margin:,.2f} controls ${pos_value:,.2f}  "
                                            f"({quantity} shares)  "
                                            f"liq if price hits ${liq_price:,.2f}")
                            else:
                                lev_line = ""

                            self.log_message(
                                f"[{symbol}] ✅ {'LONG' if is_long else 'SHORT'} ENTRY | "
                                f"Price: {current_price:.4f} | "
                                f"margin: ${margin:,.2f} → controls ${pos_value:,.2f} | "
                                f"SL: {self.stop_loss[symbol]:.4f} | TP: {self.take_profit[symbol]:.4f} | "
                                f"Risk: ${risk_amt*quantity:.2f} | Reward: ${risk_amt*quantity*rr_actual:.2f}"
                                f"{lev_line}",
                                color="blue"
                            )

    def on_filled_order(self, position, order, price, quantity, multiplier):
        """Fires when entry fills. Posts a proper OCO directly to Alpaca REST API
        so TradingView shows SL/TP lines. Python-side monitoring in _process_symbol
        acts as the real exit trigger regardless of broker-side order state."""
        asset  = order.asset
        symbol = getattr(asset, "symbol", str(asset))
        if symbol not in self.symbols:
            return
        if order is not self.entry_order.get(symbol):
            return
        self.entry_order[symbol] = None

        # If the entry went in as a bracket, TP/SL are already attached — don't double up.
        if self.bracket_active.get(symbol):
            return

        is_long = self.bias.get(symbol) == "BULLISH"
        sl      = self.stop_loss.get(symbol)
        tp      = self.take_profit.get(symbol)

        # Match the OCO size to the ACTUAL live position (covers partial fills), falling
        # back to the fill quantity if the position isn't readable yet.
        try:
            live    = self.get_position(asset)
            oco_qty = int(abs(live.quantity)) if live is not None else int(abs(quantity))
        except Exception:
            oco_qty = int(abs(quantity))

        if sl and tp and oco_qty > 0:
            try:
                import requests as _req
                _resp = _req.post(
                    f"{ALPACA_BASE_URL}/v2/orders",
                    json={
                        "symbol":        symbol,
                        "qty":           str(oco_qty),
                        "side":          "buy" if not is_long else "sell",
                        "type":          "limit",
                        "time_in_force": "gtc",
                        "order_class":   "oco",
                        "take_profit":   {"limit_price": str(round(tp, 2))},
                        "stop_loss":     {"stop_price":  str(round(sl, 2))},
                    },
                    headers={
                        "APCA-API-KEY-ID":     ALPACA_API_KEY,
                        "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
                    },
                    timeout=10,
                )
                _resp.raise_for_status()

                # Capture the OCO order IDs (parent + legs) so _reset can cancel them
                # later instead of leaving orphans on the book.
                data = _resp.json() if _resp.content else {}
                ids  = []
                if isinstance(data, dict):
                    if data.get("id"):
                        ids.append(data["id"])
                    for leg in (data.get("legs") or []):
                        if isinstance(leg, dict) and leg.get("id"):
                            ids.append(leg["id"])
                self.oco_ids[symbol] = ids
                self._save_state()  # persist IDs so a restart can still cancel them

                self.log_message(
                    f"[{symbol}] 🛑🎯 OCO posted ({oco_qty} sh, {len(ids)} legs) — "
                    f"SL @ {sl:.4f}  TP @ {tp:.4f}  (lines on TradingView)",
                    color="yellow"
                )
            except Exception as e:
                self.log_message(
                    f"[{symbol}] OCO API failed: {e} — Python-side SL/TP monitoring still active",
                    color="red"
                )

    def on_trading_iteration(self):
        self._iter_count += 1

        # ── Leftover-overnight guard (stocks only) ──────────────────────────────
        # A position entered on a PRIOR session that somehow survived (bot was down at
        # the close, then the market was shut over the weekend so the flatten couldn't
        # fill) must be closed at the NEXT open, not held another whole day. The startup
        # is_regular_session check + the EOD window can't catch this specific Fri→Mon
        # case; this per-iteration check does, using the real entry time recovered at
        # startup. Crypto trades 24/7 (_minutes_to_close is None) → skip entirely.
        now_et = datetime.now(_ET)   # computed once, reused by the catch-up check below too
        if self._minutes_to_close() is not None:   # None only for crypto/24-7 subclass
            for symbol in self.symbols:
                if self.state.get(symbol) != "POSITION_OPEN":
                    continue
                et = self.entry_time.get(symbol)
                if et and indicators.is_leftover_position(et.isoformat(), now_et):
                    self.log_message(
                        f"[{symbol}] 🌙 Leftover from a prior session (entered "
                        f"{et:%Y-%m-%d}) — flattening at the open, not holding another day.",
                        color="red")
                    self._flatten_one(symbol)

        # EOD guard (stocks only — crypto overrides _minutes_to_close to None): inside the
        # flatten window, go flat and skip trading. Runs from the loop we KNOW executes, so
        # the end-of-day flatten no longer hinges on the before_closing_bell hook alone.
        _mtc = self._minutes_to_close()
        if _mtc is not None and _mtc <= EOD_FLATTEN_MIN:
            self._flatten_all(reason=f"EOD {_mtc:.0f}m-to-close")
            self._eod_flattened_date = now_et.date()
            self._log_daily_snapshot_if_new_day()
            return

        # Catch-up safety net: if a single iteration overran past the ENTIRE pre-close
        # window (EOD_FLATTEN_MIN sits exactly at the loop's execution interval — real
        # incident 2026-07-28, NVDA+GOOGL held 5.5h past close because _minutes_to_close
        # jumped straight from "not yet" to None with zero catch-up), this fires on the
        # next iteration regardless of exactly when it lands.
        if indicators.needs_eod_catchup_flatten(
                now_et, self._eod_flattened_date == now_et.date()):
            self.log_message(
                "⚠️ EOD catch-up flatten — the normal pre-close window was missed "
                "(iteration ran long); flattening now instead of holding overnight.",
                color="red")
            self._flatten_all(reason="EOD catch-up (missed pre-close window)")
            self._eod_flattened_date = now_et.date()
            self._log_daily_snapshot_if_new_day()
            return

        for symbol in self.symbols:
            self._process_symbol(symbol)
        self._log_daily_snapshot_if_new_day()

    def _log_daily_snapshot_if_new_day(self):
        """Once-per-UTC-day portfolio snapshot to the Stock Macro tab. Uses
        get_portfolio_value() (cash + open positions), not get_cash() — a large open
        position deducting from cash must not read as a loss, same fix as test_bot.py's
        equity display. SPY is already in the watchlist so this costs no extra fetch."""
        today = datetime.now().astimezone().date()   # LOCAL day — Macro rows dated by your date
        if self._sheet_log_date == today:
            return
        balance = self.get_portfolio_value()
        if self._daily_open_balance is None:
            self._daily_open_balance = balance   # first iteration ever — nothing to compare yet
        spy_price = self.get_last_price("SPY")
        if self._daily_open_spy is None:
            self._daily_open_spy = spy_price
        daily_return_pct = ((balance - self._daily_open_balance) / self._daily_open_balance * 100
                             if self._daily_open_balance else 0.0)
        spy_return_pct = (((spy_price - self._daily_open_spy) / self._daily_open_spy * 100)
                           if spy_price and self._daily_open_spy else 0.0)
        # Backfill days the bot was down across midnight — this rollover check otherwise
        # writes ONLY the current day and silently swallows the gap, leaving holes in the
        # Quant Desk equity curve (hit for real on the crypto side: Aug 13 -> Aug 15).
        # Missed days carry forward the last known balance at 0% — the bot wasn't running,
        # so nothing could have traded, and a flat carry-forward beats inventing a value.
        # This also fills weekends/holidays — CONFIRMED WANTED by the user 2026-08-16.
        # Correct for an equity CURVE: the account genuinely held that value on those
        # days, so a flat Sat/Sun point is the truth, not noise. Do not "optimise" this
        # into skipping non-trading days.
        for _gap_day in missing_snapshot_dates(self._sheet_log_date, today):
            self.log_message(f"[SHEETS] Backfilling missed Stock Macro row for {_gap_day} "
                             f"@ ${self._daily_open_balance:,.2f}", color="yellow")
            log_daily_snapshot(get_sheet_client(), GOOGLE_SHEET_URL, "Stock Macro",
                                _gap_day.isoformat(), self._daily_open_balance, 0.0,
                                spy_price or 0.0, 0.0)
        log_daily_snapshot(get_sheet_client(), GOOGLE_SHEET_URL, "Stock Macro",
                            today.isoformat(), balance, daily_return_pct,
                            spy_price or 0.0, spy_return_pct)
        self._sheet_log_date     = today
        self._daily_open_balance = balance
        self._daily_open_spy     = spy_price
        self._save_daily_snapshot_flag()   # persist immediately — a restart before the
        # next position-state save must still see "today already logged"

    def _flatten_one(self, symbol, reason="flatten"):
        """Cancel protection + market-close a SINGLE symbol's position (if any)."""
        asset = self._make_asset(symbol)
        position = self.get_position(asset)
        if position is not None and abs(position.quantity) > 0:
            # Cancel OCO FIRST so the TP/SL legs don't race the closing market order
            # and cause a wash-trade or an orphan fill on the wrong side.
            self._cancel_oco(symbol)
            close_qty  = abs(position.quantity)
            close_side = (Order.OrderSide.BUY if position.quantity < 0
                          else Order.OrderSide.SELL)
            order = self.create_order(asset, close_qty, close_side)
            self.submit_order(order)
            self.log_message(
                f"[{symbol}] {reason} close: cancelled OCO + submitted market close "
                f"({close_qty} shares).", color="cyan")
            self._reset(symbol)

    def _flatten_all(self, reason="EOD"):
        """Cancel protection and market-close every open position. Shared by the
        before_closing_bell hook and the main-loop EOD guard so a flatten fires
        whichever path runs first."""
        for symbol in self.symbols:
            self._flatten_one(symbol, reason=reason)

    def before_closing_bell(self):
        self._flatten_all(reason="EOD bell")

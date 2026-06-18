from lumibot.strategies import Strategy
from lumibot.entities import Asset, Order
from bot import indicators
from finbert_utils import estimate_sentiment
from config import NVIDIA_API_KEY, API_KEY as ALPACA_API_KEY, API_SECRET as ALPACA_API_SECRET, BASE_URL as ALPACA_BASE_URL
import json
import logging
import os
import re
from datetime import datetime, timezone

STRATEGY_STATE_FILE = "strategy_state.json"

MIN_AI_RR = 3.5   # hard floor — enforced in code regardless of what the model says
MAX_AI_RR = 15.0  # sanity ceiling — guards against a hallucinated target


def get_ai_confirmation(symbol, price, daily_trend, bos_dir,
                        fvg_low, fvg_high, sweep_level,
                        sl, risk_amt, pool_tp, ltf_df, htf_df=None,
                        amd_phase=None, zone_type=None):
    """
    Asks Llama 3.3 70B (via NVIDIA API) whether this SMC setup is worth taking,
    and lets it pick the R:R target itself (we only enforce a 1:{MIN_AI_RR} floor).
    Returns (confirm: bool, rr: float, reason: str).
    Defaults to (True, MIN_AI_RR, ...) on any failure — an API hiccup should
    never block a trade, it just falls back to the minimum acceptable R:R.
    """
    if not NVIDIA_API_KEY:
        return True, MIN_AI_RR, "no_nvidia_key — proceeding at minimum 1:3.5 R:R"

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

Using the full 4H chart above, analyze this SMC setup:
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
            model="meta/llama-3.3-70b-instruct",
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
            reason = f"(unparsed AI response, defaulting approve) {text}"
            confirm = True

        return confirm, rr, reason
    except Exception as e:
        return True, MIN_AI_RR, f"API error ({e}) — proceeding at minimum 1:4 R:R"

if not os.path.exists("./logs"):
    os.makedirs("./logs")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("./logs/bot_activity.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

STALE_TRADE_HOURS = 72  # 1:6 targets need up to 3 days to develop


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
        self.stop_loss          = {s: None   for s in self.symbols}
        self.take_profit        = {s: None   for s in self.symbols}
        self.entry_time         = {s: None   for s in self.symbols}
        self.sl_order           = {s: None   for s in self.symbols}
        self.tp_order           = {s: None   for s in self.symbols}
        self.entry_order        = {s: None   for s in self.symbols}
        self.ranging_mode       = {s: False  for s in self.symbols}
        self.amd_phase          = {s: None   for s in self.symbols}
        self.amd_zone_type      = {s: None   for s in self.symbols}
        self._iter_count        = 0

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
        """Persist SL/TP for every open position so restarts don't lose protection."""
        try:
            data = {}
            for s in self.symbols:
                if self.state[s] == "POSITION_OPEN":
                    data[s] = {
                        "state":       self.state[s],
                        "bias":        self.bias[s],
                        "entry_price": self.entry_price[s],
                        "stop_loss":   self.stop_loss[s],
                        "take_profit": self.take_profit[s],
                        "entry_time":  (self.entry_time[s].isoformat()
                                        if self.entry_time[s] else None),
                    }
            with open(STRATEGY_STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.log_message(f"[STATE] save failed: {e}", color="red")

    def _load_state(self):
        """Restore SL/TP from last save so Python-side monitoring resumes correctly."""
        if not os.path.exists(STRATEGY_STATE_FILE):
            return
        try:
            with open(STRATEGY_STATE_FILE) as f:
                data = json.load(f)
            for s, saved in data.items():
                if s not in self.symbols:
                    continue
                self.state[s]       = saved.get("state", "IDLE")
                self.bias[s]        = saved.get("bias")
                self.entry_price[s] = saved.get("entry_price")
                self.stop_loss[s]   = saved.get("stop_loss")
                self.take_profit[s] = saved.get("take_profit")
                raw_time            = saved.get("entry_time")
                self.entry_time[s]  = (datetime.fromisoformat(raw_time)
                                       if raw_time else None)
                if self.state[s] == "POSITION_OPEN":
                    self.log_message(
                        f"[{s}] Restored POSITION_OPEN  SL={self.stop_loss[s]}  TP={self.take_profit[s]}",
                        color="yellow"
                    )
        except Exception as e:
            self.log_message(f"[STATE] load failed: {e}", color="red")

    def before_starting_trading(self):
        """Re-sync state from live positions and saved SL/TP on startup."""
        self._load_state()
        try:
            for symbol in self.symbols:
                asset    = self._make_asset(symbol)
                position = self.get_position(asset)
                if position is not None and self.state[symbol] != "POSITION_OPEN":
                    self.state[symbol]      = "POSITION_OPEN"
                    self.entry_time[symbol] = datetime.now(timezone.utc)
                    self.log_message(
                        f"[{symbol}] Startup sync: live position found → POSITION_OPEN "
                        f"SL={self.stop_loss[symbol]}  TP={self.take_profit[symbol]}",
                        color="yellow"
                    )
                elif position is None and self.state[symbol] == "POSITION_OPEN":
                    # Position closed while bot was offline — clean up
                    self._reset(symbol)
        except Exception as e:
            self.log_message(f"Startup sync skipped (broker not ready): {e}", color="red")

    def _reset(self, symbol):
        # Cancel any resting SL/TP orders so they don't fire after we've moved on
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
        self._save_state()

    def _make_asset(self, symbol):
        """Override in subclasses to change asset type (e.g. CRYPTO)."""
        return Asset(symbol, asset_type=Asset.AssetType.STOCK)

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
            # fallback: dollar allocation (used only when SL isn't calculated yet)
            quantity = (cash * self.cash_at_risk_per_symbol) / last_price

        max_qty  = (cash * 0.20) / last_price  # hard cap: 20% of cash per trade
        quantity = max(0, round(min(quantity, max_qty), 0))
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

    def get_htf_bias(self, symbol):
        asset = self._make_asset(symbol)
        try:
            bars = self.get_historical_prices(asset, 200, self.timeframe_htf)
            if bars is None:
                return None
            df = bars.pandas_df
            is_consolidating = indicators.detect_consolidation(df, lookback=20)
            is_bos, direction = indicators.detect_displacement_bos(df, lookback=15)
            resistance, support = indicators.find_support_resistance(df, lookback=50)
            is_sweep_htf, _, sweep_wick_htf = indicators.check_liquidity_sweep(df, sweep_window=5)
            return {
                "consolidating": is_consolidating,
                "bos": is_bos,
                "direction": direction,
                "resistance": resistance[0] if resistance else None,
                "support": support[0] if support else None,
                "sweep_htf": is_sweep_htf,
                "sweep_wick_htf": sweep_wick_htf,
                "df": df,
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
        Always returns True if no news is available — avoids the "no news = no trade" trap.
        """
        try:
            news_items = self.get_news(symbol)
        except Exception:
            news_items = None

        if not news_items:
            return True, "no_news", 0.0

        probability, sentiment = estimate_sentiment(news_items)
        confirm = not (sentiment == "negative" and probability >= 0.60)
        return confirm, sentiment, probability

    def _process_symbol(self, symbol):
        current_price = self.get_last_price(symbol)
        if not current_price:
            return
        asset = self._make_asset(symbol)
        position = self.get_position(asset)

        # ── STATE 4: POSITION_OPEN ─────────────────────────────────────────────
        # Check this first so we don't re-enter while a trade is live.
        if self.state[symbol] == "POSITION_OPEN":
            if position is None:
                self.log_message(f"[{symbol}] Position closed. Resetting.", color="cyan")
                self._reset(symbol)
                return

            is_long = self.bias[symbol] == "BULLISH"
            sl = self.stop_loss[symbol]
            tp = self.take_profit[symbol]

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
                self.submit_order(self.create_order(asset, close_qty, close_side))
                pnl = ((current_price - self.entry_price[symbol]) * close_qty
                       if is_long else
                       (self.entry_price[symbol] - current_price) * close_qty)
                self.log_message(
                    f"[{symbol}] {'🟢' if pnl >= 0 else '🔴'} {hit} hit "
                    f"@ {current_price:.4f} | P&L: {pnl:+.2f}",
                    color="green" if pnl >= 0 else "red"
                )
                self._reset(symbol)  # also cancels broker SL/TP orders + saves state
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
                    self.submit_order(self.create_order(asset, close_qty, close_side))
                    self.log_message(
                        f"[{symbol}] ⏰ Stale exit after {elapsed_hours:.1f}h — closing position.",
                        color="red"
                    )
                    self._reset(symbol)
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
        self.log_message(
            f"[{symbol}] state={self.state[symbol]} price={current_price:.4f} "
            f"daily={daily_trend} bos={htf['bos']}({htf['direction']}) "
            f"htf_sw={htf.get('sweep_htf', False)} sweep={ltf['sweep']} "
            f"fvg_bull={ltf['fvg_bull']} fvg_bear={ltf['fvg_bear']}"
            f"{zone_tag}"
        )

        # ── STATE 1: IDLE — daily trend + 4H BOS must agree ──────────────────────
        AMD_ENTRY_WAIT_ITERS = 96  # AMD zones can take many iterations to reach
        if self.state[symbol] == "IDLE" and position is None:
            if htf["bos"] and htf["direction"] == "bullish" and daily_trend == "bullish":
                self.bias[symbol]               = "BULLISH"
                self.ranging_mode[symbol]       = False
                self.state[symbol]              = "SWEEP_HUNT"
                self.sweep_hunt_iter[symbol]    = self._iter_count
                self.log_message(
                    f"[{symbol}] STEP 1: Daily BULLISH + 4H BOS → hunting sweep then FVG.",
                    color="yellow"
                )
            elif htf["bos"] and htf["direction"] == "bearish" and daily_trend == "bearish":
                self.bias[symbol]               = "BEARISH"
                self.ranging_mode[symbol]       = False
                self.state[symbol]              = "SWEEP_HUNT"
                self.sweep_hunt_iter[symbol]    = self._iter_count
                self.log_message(
                    f"[{symbol}] STEP 1: Daily BEARISH + 4H BOS → hunting sweep then FVG.",
                    color="red"
                )
            elif htf["bos"] and htf["direction"] and daily_trend is None:
                self.bias[symbol]               = htf["direction"].upper()
                self.ranging_mode[symbol]       = True
                self.state[symbol]              = "SWEEP_HUNT"
                self.sweep_hunt_iter[symbol]    = self._iter_count
                self.log_message(
                    f"[{symbol}] STEP 1 (ranging): 4H BOS {htf['direction'].upper()}, "
                    f"daily=unclear → hunting sweep at HALF size.",
                    color="yellow"
                )
            elif htf["bos"] and htf["direction"] and daily_trend and htf["direction"] != daily_trend:
                self.log_message(
                    f"[{symbol}] 4H BOS {htf['direction']} skipped — daily trend is {daily_trend}.",
                    color="yellow"
                )

            # ── AMD counter-setup: 4H sweep already happened → wait for zone ──
            # Sweep of lows + bearish daily = stop hunt (manipulation) → SHORT from supply.
            if self.state[symbol] == "IDLE":
                if htf.get("sweep_htf") and daily_trend == "bearish":
                    found, sup_lo, sup_hi, sup_type = indicators.find_supply_zone(
                        htf["df"], current_price
                    )
                    if found:
                        self.bias[symbol]            = "BEARISH"
                        self.sweep_low[symbol]       = htf.get("sweep_wick_htf")
                        self.amd_phase[symbol]       = "manipulation_up"
                        self.amd_zone_type[symbol]   = sup_type
                        self.fvg_low[symbol]         = sup_lo
                        self.fvg_high[symbol]        = sup_hi
                        self.ranging_mode[symbol]    = False
                        self.state[symbol]           = "ENTRY_WAIT"
                        self.fvg_set_iter[symbol]    = self._iter_count
                        self.log_message(
                            f"[{symbol}] 🎯 AMD: 4H sweep + daily BEARISH → "
                            f"[{sup_type}] {sup_lo:.2f}–{sup_hi:.2f} → SHORT",
                            color="red"
                        )
                elif htf.get("sweep_htf") and daily_trend == "bullish":
                    found, dem_lo, dem_hi, dem_type = indicators.find_demand_zone(
                        htf["df"], current_price
                    )
                    if found:
                        self.bias[symbol]            = "BULLISH"
                        self.sweep_low[symbol]       = htf.get("sweep_wick_htf")
                        self.amd_phase[symbol]       = "manipulation_down"
                        self.amd_zone_type[symbol]   = dem_type
                        self.fvg_low[symbol]         = dem_lo
                        self.fvg_high[symbol]        = dem_hi
                        self.ranging_mode[symbol]    = False
                        self.state[symbol]           = "ENTRY_WAIT"
                        self.fvg_set_iter[symbol]    = self._iter_count
                        self.log_message(
                            f"[{symbol}] 🎯 AMD: 4H sweep + daily BULLISH → "
                            f"[{dem_type}] {dem_lo:.2f}–{dem_hi:.2f} → LONG",
                            color="green"
                        )

            # ── Trend-zone fallback: daily trend clear but no fresh BOS/sweep ──
            # Trending markets grind without daily BOS signals; watch for pullbacks
            # to unmitigated 4H demand/supply zones instead.
            if self.state[symbol] == "IDLE":
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
                        self.log_message(
                            f"[{symbol}] 📊 Trend zone: daily BULLISH → [{dem_type}] "
                            f"{dem_lo:.2f}–{dem_hi:.2f} → LONG on pullback",
                            color="green"
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
                        self.log_message(
                            f"[{symbol}] 📊 Trend zone: daily BEARISH → [{sup_type}] "
                            f"{sup_lo:.2f}–{sup_hi:.2f} → SHORT on rally",
                            color="red"
                        )

        # ── STATE 2: SWEEP_HUNT ───────────────────────────────────────────────────
        # Real SMC discipline: require a liquidity sweep before looking for an FVG.
        # After 30 iterations (~30 min) without a sweep the BOS signal is stale — reset.
        SWEEP_PATIENCE = 30
        if self.state[symbol] == "SWEEP_HUNT" and position is None:
            iters_hunting = self._iter_count - self.sweep_hunt_iter[symbol]

            if ltf["sweep"] and ltf["sweep_wick_low"]:
                self.sweep_low[symbol] = ltf["sweep_wick_low"]
                self.log_message(
                    f"[{symbol}] Sweep detected @ {ltf['sweep_wick_low']:.4f} — now hunting FVG/OB.",
                    color="cyan"
                )

            # Expire the BOS signal if no sweep in SWEEP_PATIENCE iterations
            if iters_hunting > SWEEP_PATIENCE and not self.sweep_low[symbol]:
                self.log_message(
                    f"[{symbol}] No sweep after {SWEEP_PATIENCE} iters — BOS stale. Resetting.",
                    color="yellow"
                )
                self._reset(symbol)
                return

            # Only proceed to ENTRY_WAIT once a sweep has been recorded
            if not self.sweep_low[symbol]:
                return

            if self.bias[symbol] == "BULLISH" and ltf["fvg_bull"]:
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

            in_fvg = (self.fvg_low[symbol] is not None and
                      self.fvg_high[symbol] is not None and
                      self.fvg_low[symbol] <= current_price <= self.fvg_high[symbol])

            if in_fvg:
                confirm, sentiment, prob = self._get_sentiment(symbol)
                if prob > 0:
                    self.log_message(
                        f"[{symbol}] AI: {sentiment.upper()} ({prob*100:.1f}%) → {'✅' if confirm else '❌'}",
                        color="purple"
                    )
                else:
                    self.log_message(f"[{symbol}] No news — proceeding on technicals.", color="purple")

                if confirm:
                    is_long = self.bias[symbol] == "BULLISH"
                    side    = Order.OrderSide.BUY if is_long else Order.OrderSide.SELL

                    # SL at the structural invalidation point (OB/FVG boundary)
                    # If price closes back through the zone it entered, the thesis is wrong.
                    if is_long:
                        sl = float(f"{self.fvg_low[symbol] * 0.997:.2f}")
                    else:
                        sl = float(f"{self.fvg_high[symbol] * 1.003:.2f}")

                    risk_amt = abs(current_price - sl)
                    bias_str = "bullish" if is_long else "bearish"
                    pool_tp  = indicators.find_next_liquidity_target(htf["df"], current_price, bias_str)

                    ai_confirm, rr_actual, ai_reason = get_ai_confirmation(
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
                        color="purple"
                    )

                    if ai_confirm:
                        cash, last_price, quantity = self.position_sizing(symbol, sl_price=sl)
                        if self.ranging_mode[symbol]:
                            quantity = max(1, round(quantity * 0.5))

                        if quantity > 0 and cash > last_price:
                            self.stop_loss[symbol]   = sl
                            self.take_profit[symbol] = round(
                                current_price + risk_amt * rr_actual if is_long
                                else current_price - risk_amt * rr_actual, 2
                            )

                            # Plain market entry — SL/TP orders are submitted in
                            # on_filled_order once Alpaca confirms the position is open.
                            order = self.create_order(asset, quantity, side)
                            self.entry_order[symbol] = order
                            self.submit_order(order)
                            self.state[symbol]       = "POSITION_OPEN"
                            self.entry_price[symbol] = current_price
                            self.entry_time[symbol]  = datetime.now(timezone.utc)
                            self._save_state()
                            self.log_message(
                                f"[{symbol}] ✅ {'LONG' if is_long else 'SHORT'} ENTRY | "
                                f"Price: {current_price:.4f} | "
                                f"SL: {self.stop_loss[symbol]:.4f} | TP: {self.take_profit[symbol]:.4f} | "
                                f"Qty: {quantity} | Risk: ${risk_amt * quantity:.2f}",
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

        is_long = self.bias.get(symbol) == "BULLISH"
        sl      = self.stop_loss.get(symbol)
        tp      = self.take_profit.get(symbol)
        qty     = abs(quantity)

        if sl and tp:
            try:
                import requests as _req
                _resp = _req.post(
                    f"{ALPACA_BASE_URL}/v2/orders",
                    json={
                        "symbol":        symbol,
                        "qty":           str(int(abs(qty))),
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
                self.log_message(
                    f"[{symbol}] 🛑🎯 OCO posted — SL @ {sl:.4f}  TP @ {tp:.4f}  (lines on TradingView)",
                    color="yellow"
                )
            except Exception as e:
                self.log_message(
                    f"[{symbol}] OCO API failed: {e} — Python-side SL/TP monitoring still active",
                    color="red"
                )

    def on_trading_iteration(self):
        self._iter_count += 1
        for symbol in self.symbols:
            self._process_symbol(symbol)

    def before_closing_bell(self):
        for symbol in self.symbols:
            asset = self._make_asset(symbol)
            position = self.get_position(asset)
            if position is not None:
                close_qty  = abs(position.quantity)
                close_side = (Order.OrderSide.BUY if position.quantity < 0
                              else Order.OrderSide.SELL)
                order = self.create_order(asset, close_qty, close_side)
                self.submit_order(order)
                self.log_message(f"[{symbol}] EOD: closed position.", color="cyan")
                self._reset(symbol)

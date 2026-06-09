from lumibot.strategies import Strategy
from lumibot.entities import Asset, Order
from bot import indicators
from finbert_utils import estimate_sentiment
import logging
import os
from datetime import datetime, timezone

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

STALE_TRADE_HOURS = 4  # Close any position open longer than this


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

        self.state       = {s: "IDLE" for s in self.symbols}
        self.bias        = {s: None   for s in self.symbols}
        self.sweep_low   = {s: None   for s in self.symbols}
        self.sweep_high  = {s: None   for s in self.symbols}
        self.fvg_low     = {s: None   for s in self.symbols}
        self.fvg_high    = {s: None   for s in self.symbols}
        self.ote_zone    = {s: None   for s in self.symbols}
        self.entry_price = {s: None   for s in self.symbols}
        self.stop_loss   = {s: None   for s in self.symbols}
        self.take_profit = {s: None   for s in self.symbols}
        self.entry_time  = {s: None   for s in self.symbols}  # for stale-trade exit

    def before_starting_trading(self):
        """
        Re-sync state from live positions on startup.
        Prevents the bot getting stuck in POSITION_OPEN after a crash or reconnect.
        """
        for symbol in self.symbols:
            asset = self._make_asset(symbol)
            position = self.get_position(asset)
            if position is not None:
                self.state[symbol] = "POSITION_OPEN"
                self.entry_time[symbol] = datetime.now(timezone.utc)
                self.log_message(
                    f"[{symbol}] Startup sync: live position found → resuming POSITION_OPEN",
                    color="yellow"
                )

    def _reset(self, symbol):
        self.state[symbol]       = "IDLE"
        self.bias[symbol]        = None
        self.sweep_low[symbol]   = None
        self.sweep_high[symbol]  = None
        self.fvg_low[symbol]     = None
        self.fvg_high[symbol]    = None
        self.ote_zone[symbol]    = None
        self.entry_price[symbol] = None
        self.stop_loss[symbol]   = None
        self.take_profit[symbol] = None
        self.entry_time[symbol]  = None

    def _make_asset(self, symbol):
        """Override in subclasses to change asset type (e.g. CRYPTO)."""
        return Asset(symbol, asset_type=Asset.AssetType.STOCK)

    def position_sizing(self, symbol):
        cash = self.get_cash()
        last_price = self.get_last_price(symbol)
        if not last_price:
            return cash, last_price, 0
        quantity = round((cash * self.cash_at_risk_per_symbol) / last_price, 0)
        return cash, last_price, quantity

    def get_htf_bias(self, symbol):
        asset = self._make_asset(symbol)
        try:
            bars = self.get_historical_prices(asset, 100, self.timeframe_htf)
            if bars is None:
                return None
            df = bars.pandas_df
            is_consolidating = indicators.detect_consolidation(df, lookback=20)
            is_bos, direction = indicators.detect_displacement_bos(df, lookback=15)
            resistance, support = indicators.find_support_resistance(df, lookback=50)
            return {
                "consolidating": is_consolidating,
                "bos": is_bos,
                "direction": direction,
                "resistance": resistance[0] if resistance else None,
                "support": support[0] if support else None,
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
                # TP or SL was hit
                self.log_message(f"[{symbol}] Position closed (TP/SL). Resetting.", color="cyan")
                self._reset(symbol)
                return

            # Time-based exit: close stale trades after STALE_TRADE_HOURS
            if self.entry_time[symbol]:
                now = datetime.now(timezone.utc)
                entry = self.entry_time[symbol]
                if entry.tzinfo is None:
                    entry = entry.replace(tzinfo=timezone.utc)
                elapsed_hours = (now - entry).total_seconds() / 3600
                if elapsed_hours >= STALE_TRADE_HOURS:
                    order = self.create_order(asset, position.quantity, Order.OrderSide.SELL)
                    self.submit_order(order)
                    self.log_message(
                        f"[{symbol}] ⏰ Stale exit after {elapsed_hours:.1f}h — closing position.",
                        color="orange"
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

        # ── STATE 1: IDLE ──────────────────────────────────────────────────────
        if self.state[symbol] == "IDLE" and position is None:
            if htf["bos"] and ltf["sweep"]:
                if ltf["fvg_bull"]:
                    self.bias[symbol] = "BULLISH"
                    self.sweep_low[symbol] = ltf["sweep_wick_low"]
                    self.state[symbol] = "SWEEP_HUNT"
                    self.log_message(
                        f"[{symbol}] STEP 1-2: BLEED+SWEEP → BULLISH | Wick: {self.sweep_low[symbol]:.2f}",
                        color="yellow"
                    )
                elif ltf["fvg_bear"]:
                    self.bias[symbol] = "BEARISH"
                    self.sweep_high[symbol] = ltf["sweep_wick_low"]
                    self.state[symbol] = "SWEEP_HUNT"
                    self.log_message(
                        f"[{symbol}] STEP 1-2: BLEED+SWEEP → BEARISH | Wick: {self.sweep_high[symbol]:.2f}",
                        color="orange"
                    )

        # ── STATE 2: SWEEP_HUNT ────────────────────────────────────────────────
        if self.state[symbol] == "SWEEP_HUNT" and position is None:
            if ltf["mss"] and ltf["choch"]:
                if self.bias[symbol] == "BULLISH" and ltf["choch_direction"] == "bullish":
                    self.fvg_low[symbol] = ltf["fvg_bottom"]
                    self.fvg_high[symbol] = ltf["fvg_top"]
                    _, self.ote_zone[symbol] = indicators.calculate_fib_levels(
                        self.sweep_low[symbol], ltf["swing_high"]
                    )
                    self.state[symbol] = "ENTRY_WAIT"
                    self.log_message(
                        f"[{symbol}] STEP 3: MSS+FVG | "
                        f"FVG: {self.fvg_low[symbol]:.2f}-{self.fvg_high[symbol]:.2f} | "
                        f"OTE: {self.ote_zone[symbol]['lower']:.2f}-{self.ote_zone[symbol]['upper']:.2f}",
                        color="green"
                    )

        # ── STATE 3: ENTRY_WAIT ────────────────────────────────────────────────
        if self.state[symbol] == "ENTRY_WAIT" and position is None:
            in_fvg = self.fvg_low[symbol] <= current_price <= self.fvg_high[symbol]
            in_ote = indicators.is_price_in_fib_ote(current_price, self.ote_zone[symbol])

            if in_fvg and in_ote:
                confirm, sentiment, prob = self._get_sentiment(symbol)
                if prob > 0:
                    self.log_message(
                        f"[{symbol}] AI: {sentiment.upper()} ({prob*100:.1f}%) → {'✅' if confirm else '❌'}",
                        color="purple"
                    )
                else:
                    self.log_message(f"[{symbol}] No news — proceeding on technicals.", color="purple")

                if confirm:
                    cash, last_price, quantity = self.position_sizing(symbol)
                    if quantity > 0 and cash > last_price:
                        self.stop_loss[symbol] = self.sweep_low[symbol] * 0.98
                        risk = current_price - self.stop_loss[symbol]
                        self.take_profit[symbol] = current_price + (risk * 2)
                        order = self.create_order(
                            asset, quantity, Order.OrderSide.BUY,
                            order_type="bracket",
                            take_profit_price=self.take_profit[symbol],
                            stop_loss_price=self.stop_loss[symbol],
                        )
                        self.submit_order(order)
                        self.state[symbol] = "POSITION_OPEN"
                        self.entry_price[symbol] = current_price
                        self.entry_time[symbol] = datetime.now(timezone.utc)
                        self.log_message(
                            f"[{symbol}] ✅ ENTRY | Price: {current_price:.2f} | "
                            f"SL: {self.stop_loss[symbol]:.2f} | TP: {self.take_profit[symbol]:.2f} | "
                            f"Qty: {quantity}",
                            color="blue"
                        )

    def on_trading_iteration(self):
        for symbol in self.symbols:
            self._process_symbol(symbol)

    def before_closing_bell(self):
        for symbol in self.symbols:
            asset = self._make_asset(symbol)
            if self.get_position(asset) is not None:
                order = self.create_order(asset, self.get_position(asset).quantity, Order.OrderSide.SELL)
                self.submit_order(order)
                self.log_message(f"[{symbol}] EOD: closed position.", color="cyan")
                self._reset(symbol)

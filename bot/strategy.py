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

STALE_TRADE_HOURS = 12  # Close any position open longer than this


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
        Wrapped in try/except so a slow broker connection can't block strategy startup.
        """
        try:
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
        except Exception as e:
            self.log_message(f"Startup sync skipped (broker not ready): {e}", color="red")

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
                    close_qty = abs(position.quantity)
                    close_side = (Order.OrderSide.BUY if position.quantity < 0
                                  else Order.OrderSide.SELL)
                    order = self.create_order(asset, close_qty, close_side)
                    self.submit_order(order)
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

        self.log_message(
            f"[{symbol}] state={self.state[symbol]} price={current_price:.4f} "
            f"bos={htf['bos']} sweep={ltf['sweep']} mss={ltf['mss']} "
            f"fvg_bull={ltf['fvg_bull']} choch={ltf['choch']}({ltf['choch_direction']})"
        )

        # ── STATE 1: IDLE — HTF bias sets direction, no LTF required yet ──────
        # BOS on 4H establishes the macro bias. LTF sweep is hunted in STATE 2.
        if self.state[symbol] == "IDLE" and position is None:
            if htf["bos"] and htf["direction"] == "bullish":
                self.bias[symbol] = "BULLISH"
                self.state[symbol] = "SWEEP_HUNT"
                self.log_message(
                    f"[{symbol}] STEP 1: HTF BOS → BULLISH bias set. Hunting LTF sweep.",
                    color="yellow"
                )
            elif htf["bos"] and htf["direction"] == "bearish":
                self.bias[symbol] = "BEARISH"
                self.state[symbol] = "SWEEP_HUNT"
                self.log_message(
                    f"[{symbol}] STEP 1: HTF BOS → BEARISH bias set. Hunting LTF sweep.",
                    color="red"
                )

        # ── STATE 2: SWEEP_HUNT — wait for a FVG in the bias direction ──────────
        # sweep/MSS are logged as context but not required — the MSS function only
        # detects bullish breaks, so gating on it blocks every bearish setup.
        if self.state[symbol] == "SWEEP_HUNT" and position is None:
            if ltf["sweep"] and ltf["sweep_wick_low"]:
                self.sweep_low[symbol] = ltf["sweep_wick_low"]

            if self.bias[symbol] == "BULLISH" and ltf["fvg_bull"]:
                self.fvg_low[symbol]  = ltf["fvg_bottom"]
                self.fvg_high[symbol] = ltf["fvg_top"]
                self.state[symbol]    = "ENTRY_WAIT"
                self.log_message(
                    f"[{symbol}] STEP 2: Bullish FVG locked | "
                    f"{self.fvg_low[symbol]:.4f}-{self.fvg_high[symbol]:.4f} | "
                    f"sweep={'yes' if self.sweep_low[symbol] else 'no'} "
                    f"mss={ltf['mss']}",
                    color="green"
                )

            elif self.bias[symbol] == "BEARISH" and ltf["fvg_bear"]:
                self.fvg_low[symbol]  = ltf["fvg_bear_bottom"]
                self.fvg_high[symbol] = ltf["fvg_bear_top"]
                self.state[symbol]    = "ENTRY_WAIT"
                self.log_message(
                    f"[{symbol}] STEP 2: Bearish FVG locked | "
                    f"{self.fvg_low[symbol]:.4f}-{self.fvg_high[symbol]:.4f} | "
                    f"sweep={'yes' if self.sweep_low[symbol] else 'no'} "
                    f"mss={ltf['mss']}",
                    color="red"
                )

        # ── STATE 3: ENTRY_WAIT ────────────────────────────────────────────────
        if self.state[symbol] == "ENTRY_WAIT" and position is None:
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
                    cash, last_price, quantity = self.position_sizing(symbol)
                    if quantity > 0 and cash > last_price:
                        is_long = self.bias[symbol] == "BULLISH"
                        side = Order.OrderSide.BUY if is_long else Order.OrderSide.SELL
                        sweep_ref = self.sweep_low[symbol] or current_price * 0.98
                        if is_long:
                            self.stop_loss[symbol] = sweep_ref * 0.98
                            risk = current_price - self.stop_loss[symbol]
                            self.take_profit[symbol] = current_price + (risk * 3)
                        else:
                            self.stop_loss[symbol] = current_price * 1.02
                            risk = self.stop_loss[symbol] - current_price
                            self.take_profit[symbol] = current_price - (risk * 3)
                        order = self.create_order(
                            asset, quantity, side,
                            order_type="bracket",
                            take_profit_price=self.take_profit[symbol],
                            stop_loss_price=self.stop_loss[symbol],
                        )
                        self.submit_order(order)
                        self.state[symbol] = "POSITION_OPEN"
                        self.entry_price[symbol] = current_price
                        self.entry_time[symbol] = datetime.now(timezone.utc)
                        self.log_message(
                            f"[{symbol}] ✅ {'LONG' if is_long else 'SHORT'} ENTRY | "
                            f"Price: {current_price:.4f} | "
                            f"SL: {self.stop_loss[symbol]:.4f} | TP: {self.take_profit[symbol]:.4f} | "
                            f"Qty: {quantity}",
                            color="blue"
                        )

    def on_trading_iteration(self):
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

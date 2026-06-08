from lumibot.strategies import Strategy
from lumibot.entities import Asset, Order
from bot import indicators
from finbert_utils import estimate_sentiment
import logging
import os
from datetime import datetime

# Setup logging to file
if not os.path.exists("./logs"):
    os.makedirs("./logs")

log_file = f"./logs/bot_activity.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class DebbieLaSMC(Strategy):
    """
    The "Debbie-La" Institutional Setup Bot
    
    Multi-timeframe trading using:
    - 4H (HTF): Macro bias, consolidation zones, displacement, support/resistance
    - 15m (LTF): Execution, sweep detection, FVG entry, CHoCH confirmation
    - Fibonacci: Optimal Trade Entry (OTE) zone refinement
    - AI Sentiment: Confluence confirmation filter
    
    The 4-Step Setup:
    Step 1: The Bleed - Identify downtrend with lower highs/lows
    Step 2: The Wick (Sweep) - Price dumps below support, sweeps liquidity
    Step 3: The Rocket (Shift) - Aggressive institutional reversal, breaks structure
    Step 4: The Discount (Entry) - Price retraces into FVG + Fib OTE zone
    """
    
    def initialize(self, symbol: str = "SPY", cash_at_risk: float = 0.03, 
                   timeframe_htf: str = "4H", timeframe_ltf: str = "15m"):
        self.symbol = symbol
        self.sleeptime = timeframe_ltf
        self.cash_at_risk = cash_at_risk
        self.timeframe_htf = timeframe_htf
        self.timeframe_ltf = timeframe_ltf
        
        # Setup state machine
        self.state = "IDLE"  # IDLE -> SWEEP_HUNT -> MSS_CONFIRM -> ENTRY_WAIT -> POSITION_OPEN
        self.bias = None  # "BULLISH" or "BEARISH" from HTF analysis
        
        # Tracked levels and zones
        self.sweep_low = None
        self.sweep_high = None
        self.fvg_low = None
        self.fvg_high = None
        self.ote_zone = None
        self.entry_price = None
        self.stop_loss = None
        self.take_profit = None
        
        # Risk management
        self.max_position_size = 1
        self.is_stop_loss_triggered = False
        
    def position_sizing(self):
        cash = self.get_cash()
        last_price = self.get_last_price(self.symbol)
        if last_price == 0:
            return cash, last_price, 0
        quantity = round((cash * self.cash_at_risk) / last_price, 0)
        return cash, last_price, quantity
    
    def get_htf_bias(self):
        """
        STEP 1 (HTF): Analyze 4H chart for macro narrative.
        Determines if we're in a consolidation, trending, or reversal phase.
        """
        asset = Asset(self.symbol, asset_type=Asset.AssetType.STOCK)
        
        try:
            bars_4h = self.get_historical_prices(asset, 100, self.timeframe_htf)
            if bars_4h is None:
                return None
            
            df_4h = bars_4h.pandas_df
            
            # Check consolidation (range-bound market)
            is_consolidating = indicators.detect_consolidation(df_4h, lookback=20)
            
            # Check displacement and break of structure
            is_bos, direction = indicators.detect_displacement_bos(df_4h, lookback=15)
            
            # Get support and resistance
            resistance, support = indicators.find_support_resistance(df_4h, lookback=50)
            
            # Determine bias based on structure
            bias_info = {
                "consolidating": is_consolidating,
                "bos": is_bos,
                "direction": direction,
                "resistance": resistance[0] if resistance else None,
                "support": support[0] if support else None,
                "df": df_4h
            }
            
            return bias_info
            
        except Exception as e:
            self.log_message(f"⚠️ HTF Bias Error: {e}")
            return None
    
    def get_ltf_technicals(self):
        """
        STEP 2-3 (LTF): Analyze 15m/5m chart for the institutional setup.
        Returns sweep, MSS, and FVG data for state progression.
        """
        asset = Asset(self.symbol, asset_type=Asset.AssetType.STOCK)
        
        try:
            bars_ltf = self.get_historical_prices(asset, 50, self.timeframe_ltf)
            if bars_ltf is None:
                return None
            
            df_ltf = bars_ltf.pandas_df
            
            # STEP 2: Check for the liquidity sweep (The Wick)
            is_sweep, support_level, sweep_wick_low = indicators.check_liquidity_sweep(df_ltf)
            
            # STEP 3: Check for market structure shift (The Rocket)
            is_mss, swing_high_broken = indicators.check_market_structure_shift(df_ltf)
            
            # STEP 3: Check for Fair Value Gap (The Imbalance)
            is_fvg_bull, fvg_bottom, fvg_top = indicators.find_bullish_fvg(df_ltf)
            is_fvg_bear, fvg_bear_bottom, fvg_bear_top = indicators.find_bearish_fvg(df_ltf)
            
            # CHoCH confirmation
            is_choch, choch_direction = indicators.detect_choch(df_ltf, lookback=5)
            
            ltf_data = {
                "sweep": is_sweep,
                "sweep_wick_low": sweep_wick_low,
                "support_level": support_level,
                "mss": is_mss,
                "swing_high": swing_high_broken,
                "fvg_bull": is_fvg_bull,
                "fvg_bottom": fvg_bottom,
                "fvg_top": fvg_top,
                "fvg_bear": is_fvg_bear,
                "fvg_bear_bottom": fvg_bear_bottom,
                "fvg_bear_top": fvg_bear_top,
                "choch": is_choch,
                "choch_direction": choch_direction,
                "df": df_ltf
            }
            
            return ltf_data
            
        except Exception as e:
            self.log_message(f"⚠️ LTF Technicals Error: {e}")
            return None
    
    def on_trading_iteration(self):
        """
        Main execution loop - steps through the 4-stage institutional setup.
        """
        current_price = self.get_last_price(self.symbol)
        asset = Asset(self.symbol, asset_type=Asset.AssetType.STOCK)
        position = self.get_position(asset)
        
        # ========== HTF BIAS SCAN (4H) ==========
        htf = self.get_htf_bias()
        if htf is None:
            return
        
        # ========== LTF EXECUTION SCAN (15m) ==========
        ltf = self.get_ltf_technicals()
        if ltf is None:
            return
        
        # ========== STATE MACHINE ==========
        
        # STATE 1: IDLE - Wait for HTF setup confirmation
        if self.state == "IDLE" and position is None:
            # Check if we have a clear bias and consolidation is breaking
            if htf["bos"] and ltf["sweep"]:
                if ltf["fvg_bull"]:
                    self.bias = "BULLISH"
                    self.sweep_low = ltf["sweep_wick_low"]
                    self.state = "SWEEP_HUNT"
                    self.log_message(
                        f"📍 [STEP 1-2] BLEED + SWEEP confirmed. Bias: BULLISH | "
                        f"Sweep Wick: {self.sweep_low:.2f}", 
                        color="yellow"
                    )
                elif ltf["fvg_bear"]:
                    self.bias = "BEARISH"
                    self.sweep_high = ltf["sweep_wick_low"]
                    self.state = "SWEEP_HUNT"
                    self.log_message(
                        f"📍 [STEP 1-2] BLEED + SWEEP confirmed. Bias: BEARISH | "
                        f"Sweep Wick: {self.sweep_high:.2f}", 
                        color="orange"
                    )
                    return
        
        # STATE 2: SWEEP_HUNT - Confirm MSS + FVG after sweep
        if self.state == "SWEEP_HUNT" and position is None:
            if ltf["mss"] and ltf["choch"]:
                if self.bias == "BULLISH" and ltf["choch_direction"] == "bullish":
                    # STEP 3: The Rocket - Set FVG zone
                    self.fvg_low = ltf["fvg_bottom"]
                    self.fvg_high = ltf["fvg_top"]
                    
                    # Calculate Fibonacci from sweep to MSS peak
                    swing_low = self.sweep_low
                    swing_high = ltf["swing_high"]
                    fib_levels, self.ote_zone = indicators.calculate_fib_levels(swing_low, swing_high)
                    
                    self.state = "ENTRY_WAIT"
                    self.log_message(
                        f"🚀 [STEP 3] MSS + FVG confirmed! "
                        f"FVG Zone: {self.fvg_low:.2f} - {self.fvg_high:.2f} | "
                        f"OTE Zone: {self.ote_zone['lower']:.2f} - {self.ote_zone['upper']:.2f}",
                        color="green"
                    )
        
        # STATE 3: ENTRY_WAIT - Wait for price to retrace into OTE zone
        if self.state == "ENTRY_WAIT" and position is None:
            # STEP 4: The Discount - Wait for retracement into FVG + OTE
            in_fvg = self.fvg_low <= current_price <= self.fvg_high
            in_ote = indicators.is_price_in_fib_ote(current_price, self.ote_zone)
            
            if in_fvg and in_ote:
                # Get AI sentiment confirmation
                news_items = self.get_news()
                sentiment_confirm = True
                
                if news_items:
                    probability, sentiment = estimate_sentiment(news_items)
                    self.log_message(
                        f"🧠 AI Sentiment: {sentiment.upper()} ({probability*100:.1f}%)",
                        color="purple"
                    )
                    sentiment_confirm = (sentiment == "positive" and probability >= 0.60)
                
                if sentiment_confirm:
                    # EXECUTE ENTRY
                    cash, last_price, quantity = self.position_sizing()
                    
                    if quantity > 0 and cash > last_price:
                        # Stop loss: Behind the sweep wick (manipulation zone)
                        self.stop_loss = self.sweep_low * 0.98  # 2% buffer below wick
                        
                        # Take profit: Next liquidity pool (use 2x risk:reward)
                        risk = current_price - self.stop_loss
                        self.take_profit = current_price + (risk * 2)
                        
                        order = self.create_order(
                            asset, quantity, Order.OrderSide.BUY,
                            order_type="bracket",
                            take_profit_price=self.take_profit,
                            stop_loss_price=self.stop_loss
                        )
                        self.submit_order(order)
                        
                        self.state = "POSITION_OPEN"
                        self.entry_price = current_price
                        
                        self.log_message(
                            f"✅ [STEP 4] ENTRY EXECUTION!"
                            f"\n   Entry: {self.entry_price:.2f} | SL: {self.stop_loss:.2f} | TP: {self.take_profit:.2f}"
                            f"\n   Risk:Reward = 1:{(self.take_profit - current_price) / risk:.1f}",
                            color="blue"
                        )
        
        # STATE 4: POSITION_OPEN - Manage trade
        if self.state == "POSITION_OPEN":
            if position is None:
                # Position was closed (either hit TP or SL)
                self.log_message("📊 Position closed. Resetting to IDLE.", color="cyan")
                self.state = "IDLE"
                self.bias = None
                self.sweep_low = None
                self.sweep_high = None
                self.fvg_low = None
                self.fvg_high = None
                self.ote_zone = None
                self.entry_price = None
                self.stop_loss = None
                self.take_profit = None
    
    def before_closing_bell(self):
        """
        End-of-day cleanup: Close any open positions before market close.
        """
        asset = Asset(self.symbol, asset_type=Asset.AssetType.STOCK)
        position = self.get_position(asset)
        
        if position is not None:
            self.sell_all()
            self.log_message("📌 End of day: Closed all positions.", color="cyan")
            self.state = "IDLE"

from datetime import datetime, timedelta
from lumibot.brokers import Alpaca
from lumibot.strategies import Strategy
from lumibot.traders import Trader
from lumibot.entities import Asset, Order
from lumibot.backtesting import YahooDataBacktesting
from alpaca_trade_api import REST
from finbert_utils import estimate_sentiment

from bot.strategy import DebbieLaSMC

API_KEY = "PKUUMTTFXCJD6LVDLXIVSHZOP4"
API_SECRET = "GnNnkoXtxRkWTp9fNe4VfEWsXtQ2nSpzV1edCVGtkXei"
BASE_URL = "https://paper-api.alpaca.markets"

# ============================================================================
# BACKTEST CONFIGURATION
# ============================================================================

def run_backtest():
    """
    Backtest the Debbie-La Institutional Setup Strategy over historical data.
    Tests the multi-timeframe logic (4H bias + 15m execution) on recent market data.
    """
    # Define backtest window
    start_date = datetime(2026, 6, 1)
    end_date = datetime(2026, 6, 7)
    
    print("=" * 80)
    print("🤖 DEBBIE-LA INSTITUTIONAL SMC STRATEGY - BACKTEST")
    print("=" * 80)
    print(f"📊 Backtest Period: {start_date.date()} to {end_date.date()}")
    print(f"📈 Symbol: SPY")
    print(f"⏱️  HTF: 4H | LTF: 15m")
    print(f"💰 Risk per trade: 3%")
    print("=" * 80)
    
    DebbieLaSMC.backtest(
        YahooDataBacktesting,
        start_date,
        end_date,
        parameters={
            "symbol": "SPY",
            "cash_at_risk": 0.03,
            "timeframe_htf": "4H",
            "timeframe_ltf": "15m"
        }
    )

def run_live_trading():
    """
    Run the strategy live on paper trading account (Alpaca).
    Connects to Alpaca API and executes real-time trades with institutional setup logic.
    """
    print("=" * 80)
    print("🔴 DEBBIE-LA INSTITUTIONAL SMC - LIVE PAPER TRADING")
    print("=" * 80)
    print(f"🔗 Connected to: {BASE_URL}")
    print(f"📈 Symbol: SPY")
    print(f"⏱️  HTF: 4H | LTF: 15m | Execution Interval: 15 minutes")
    print(f"💰 Risk per trade: 3%")
    print("=" * 80)
    
    # Connect to broker with paper trading config
    ALPACA_CREDS = {
    "API_KEY": "PKUUMTTFXCJD6LVDLXIVSHZOP4",
    "API_SECRET": "GnNnkoXtxRkWTp9fNe4VfEWsXtQ2nSpzV1edCVGtkXei",
    "PAPER": True
}
    broker = Alpaca(ALPACA_CREDS)
    
    # Initialize strategy
    strategy = DebbieLaSMC(
        broker=broker,
        parameters={
            "symbol": "SPY",
            "cash_at_risk": 0.03,
            "timeframe_htf": "4H",
            "timeframe_ltf": "15m"
        }
    )
    
    # Create trader
    trader = Trader()
    trader.add_strategy(strategy)
    
    # Start trading
    trader.run_all()

if __name__ == "__main__":
    import sys
    
    # Check command line argument
    if len(sys.argv) > 1:
        mode = sys.argv[1].lower()
        
        if mode == "live":
            run_live_trading()
        elif mode == "backtest":
            run_backtest()
        else:
            print("Usage: python tradingbot.py [live|backtest]")
            print("\n  live     - Run strategy on paper trading")
            print("  backtest - Run strategy backtest on historical data")
    else:
        # Default to backtest
        print("No mode specified. Running backtest by default...")
        print("Usage: python tradingbot.py [live|backtest]\n")
        run_backtest()

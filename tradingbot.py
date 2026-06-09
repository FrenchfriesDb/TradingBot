from datetime import datetime, timedelta
from lumibot.brokers import Alpaca
from lumibot.strategies import Strategy
from lumibot.traders import Trader
from lumibot.entities import Asset, Order
from lumibot.backtesting import YahooDataBacktesting
from finbert_utils import estimate_sentiment
from config import API_KEY, API_SECRET, BASE_URL, PAPER_TRADING

from bot.strategy import DebbieLaSMC
from bot.crypto_strategy import DebbieLaCrypto

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
            "symbols": ["SPY"],
            "cash_at_risk": 0.03,
            "timeframe_htf": "4 hours",
            "timeframe_ltf": "15 minutes"
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
    watchlist = ["AAPL", "QQQ", "SPY", "NVDA", "TSLA", "GOOGL"]
    print(f"📈 Watchlist: {', '.join(watchlist)}")
    print(f"⏱️  HTF: 4H | LTF: 15m | Execution Interval: 15 minutes")
    print(f"💰 Risk per trade: 3% total (~0.5% per symbol)")
    print("=" * 80)

    ALPACA_CREDS = {
        "API_KEY": API_KEY,
        "API_SECRET": API_SECRET,
        "PAPER": PAPER_TRADING
    }
    broker = Alpaca(ALPACA_CREDS)

    strategy = DebbieLaSMC(
        broker=broker,
        parameters={
            "symbols": watchlist,
            "cash_at_risk": 0.03,
            "timeframe_htf": "4 hours",
            "timeframe_ltf": "15 minutes"
        }
    )
    
    # Create trader
    trader = Trader()
    trader.add_strategy(strategy)
    
    # Start trading
    trader.run_all()

def run_crypto_live():
    """Run the Debbie-La strategy on Alpaca crypto (BTC, ETH, SOL, etc.) 24/7."""
    crypto_watchlist = ["BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK"]
    print("=" * 80)
    print("🟡 DEBBIE-LA CRYPTO — ALPACA PAPER TRADING (24/7)")
    print("=" * 80)
    print(f"🔗 Connected to: {BASE_URL}")
    print(f"₿  Watchlist: {', '.join(crypto_watchlist)}")
    print(f"⏱️  HTF: 4H | LTF: 15m | Risk: 2% total (~0.33% per symbol)")
    print("=" * 80)

    ALPACA_CREDS = {
        "API_KEY": API_KEY,
        "API_SECRET": API_SECRET,
        "PAPER": PAPER_TRADING
    }
    broker = Alpaca(ALPACA_CREDS)

    strategy = DebbieLaCrypto(
        broker=broker,
        parameters={
            "symbols": crypto_watchlist,
            "cash_at_risk": 0.02,
            "timeframe_htf": "4 hours",
            "timeframe_ltf": "15 minutes"
        }
    )

    trader = Trader()
    trader.add_strategy(strategy)
    trader.run_all()


if __name__ == "__main__":
    import sys

    modes = {
        "live":    run_live_trading,
        "crypto":  run_crypto_live,
        "backtest": run_backtest,
    }

    if len(sys.argv) > 1 and sys.argv[1].lower() in modes:
        modes[sys.argv[1].lower()]()
    else:
        print("Usage: python tradingbot.py [live|crypto|backtest]")
        print("  live      — Stocks on Alpaca paper (AAPL, QQQ, SPY, NVDA, TSLA, GOOGL)")
        print("  crypto    — Crypto on Alpaca paper (BTC, ETH, SOL, DOGE, AVAX, LINK)")
        print("  backtest  — Backtest on historical data")
        print("\nBinance: python binance_bot.py")

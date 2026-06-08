# ============================================================================
# TRADING BOT CONFIGURATION
# ============================================================================

# ALPACA BROKER CREDENTIALS (Paper Trading)
API_KEY = "PKUUMTTFXCJD6LVDLXIVSHZOP4"
API_SECRET = "GnNnkoXtxRkWTp9fNe4VfEWsXtQ2nSpzV1edCVGtkXei"
BASE_URL = "https://paper-api.alpaca.markets"
PAPER_TRADING = True  # Set to False for REAL MONEY (DANGEROUS!)

# TRADING PARAMETERS
SYMBOL = "SPY"  # Stock to trade
CASH_AT_RISK = 0.03  # 3% per trade (conservative for testing)
TIMEFRAME_HTF = "4H"  # Higher timeframe for macro bias
TIMEFRAME_LTF = "15m"  # Lower timeframe for execution

# BOT EXECUTION
SLEEP_TIME = "15m"  # Wake up every 15 minutes to check setup
MAX_OPEN_POSITIONS = 1  # Only 1 trade at a time

# LOGGING
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR
LOG_TO_FILE = True
LOG_DIR = "./logs"

# STRATEGY PARAMETERS
MIN_SENTIMENT_CONFIDENCE = 0.60  # AI needs 60% confidence minimum
MIN_RISK_REWARD_RATIO = 1.5  # Minimum 1:1.5 R:R before entry
STOP_LOSS_BUFFER = 0.98  # 2% below sweep wick
TAKE_PROFIT_MULTIPLIER = 2.0  # 2x risk:reward

# MARKET HOURS (Eastern Time)
MARKET_OPEN = "09:30"  # 9:30 AM ET
MARKET_CLOSE = "15:45"  # 3:45 PM ET (exit all 15 min before close)
TRADING_ENABLED = True  # Disable for maintenance/testing

# BACKTEST PARAMETERS
BACKTEST_START = "2026-06-01"
BACKTEST_END = "2026-06-07"
BACKTEST_CASH = 100000  # Starting capital for backtest

# ============================================================================
# BINANCE (CCXT) CONFIG - for crypto paper trading on testnet
# ============================================================================
# Replace with your Binance testnet keys if you want to use testnet
BINANCE_API_KEY = ""  # your binance testnet api key
BINANCE_SECRET = ""  # your binance testnet secret
BINANCE_TESTNET = True  # Use Binance testnet sandbox when True
BINANCE_SYMBOL = "BTC/USDT"
BINANCE_CASH_AT_RISK = 0.02  # 2% per trade (crypto volatile)


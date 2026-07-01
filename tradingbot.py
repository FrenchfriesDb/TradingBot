import logging

# ── Quiet the self-healing network churn so the console stays readable ──────────
# Lumibot auto-reconnects the Alpaca order-stream websocket; the giant tracebacks and
# "restarting connection" lines are noise, not failures. NOTE: a filter on the root
# *logger* does NOT catch records propagated up from child loggers (which is why the
# old telemetry filter never actually worked) — filters must go on the *handlers*.
_NOISE = (
    "LUMIBOT_TELEMETRY",
    "LUMIWEALTH_API_KEY not set",
    "trading stream websocket error",
    "starting trading websocket connection",
    "connected to: BaseURL.TRADING_STREAM",
    "keepalive ping timeout",
    "ConnectionClosedError",
    "Error getting broker balances",
)

class _QuietFilter(logging.Filter):
    def filter(self, record):
        try:
            return not any(s in record.getMessage() for s in _NOISE)
        except Exception:
            return True

_QUIET = _QuietFilter()

def quiet_logging():
    """(Re)apply noise suppression. Safe to call repeatedly — call again after Lumibot
    has set up its own log handlers so the handler-level filter actually takes effect."""
    root = logging.getLogger()
    if _QUIET not in root.filters:
        root.addFilter(_QUIET)
    for h in root.handlers:
        if _QUIET not in h.filters:
            h.addFilter(_QUIET)
    # The big tracebacks come from these two loggers — mute them; auto-reconnect handles it.
    logging.getLogger("websockets").setLevel(logging.CRITICAL)
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)

quiet_logging()

from datetime import datetime
from lumibot.brokers import Alpaca
from lumibot.traders import Trader
from lumibot.backtesting import YahooDataBacktesting
from config import API_KEY, API_SECRET, BASE_URL, PAPER_TRADING

from bot.strategy import DebbieLaSMC
from bot.crypto_strategy import DebbieLaCrypto

# ── Lumibot compatibility patch ───────────────────────────────────────────────
# Alpaca keeps old bracket/OTO order history that lumibot can't parse (they have
# no child order prices). Instead of crashing every sync cycle, skip them silently.
try:
    from lumibot.brokers.alpaca import Alpaca as _AlpacaBroker
    _orig_parse = _AlpacaBroker._parse_broker_order

    def _safe_parse_broker_order(self, response, strategy_name, strategy_object=None):
        try:
            return _orig_parse(self, response, strategy_name, strategy_object)
        except (ValueError, TypeError) as e:
            order_id = (response.get("id", "?") if isinstance(response, dict)
                        else getattr(response, "id", "?"))
            print(f"[patch] Skipping malformed order {order_id}: {e}")
            return None

    _AlpacaBroker._parse_broker_order = _safe_parse_broker_order

    # Lumibot v4.5.x calls process_pending_orders during crash recovery but the
    # method was removed from the Alpaca broker in the current SDK version.
    # Add a no-op so a transient network drop doesn't permanently kill the bot.
    if not hasattr(_AlpacaBroker, "process_pending_orders"):
        _AlpacaBroker.process_pending_orders = lambda self, *a, **kw: None

except Exception as _patch_err:
    print(f"[patch] Warning: Could not apply order parser patch: {_patch_err}")
# ─────────────────────────────────────────────────────────────────────────────

# ============================================================================
# BACKTEST CONFIGURATION
# ============================================================================

def run_backtest():
    """
    Backtest the Debbie-La Institutional Setup Strategy over historical data.
    Tests the multi-timeframe logic (4H bias + 15m execution) on recent market data.
    """
    # Define backtest window
    start_date = datetime(2025, 1, 1)
    end_date = datetime(2025, 12, 31)
    
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
    watchlist = ["AAPL", "QQQ", "SPY", "NVDA", "TSLA", "GOOGL", "META", "MSFT"]
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
            "cash_at_risk": 0.02,
            "timeframe_htf": "4 hours",
            "timeframe_ltf": "15 minutes"
        }
    )
    
    # Create trader
    trader = Trader()
    trader.add_strategy(strategy)
    
    # Start trading
    quiet_logging()   # re-apply now that Lumibot has configured its log handlers
    trader.run_all()

def run_crypto_live():
    """Run the Debbie-La strategy on Alpaca crypto (BTC, ETH, SOL, etc.) 24/7."""
    crypto_watchlist = ["BTC", "ETH", "SOL", "LINK", "LTC", "BCH"]
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
    quiet_logging()   # re-apply now that Lumibot has configured its log handlers
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

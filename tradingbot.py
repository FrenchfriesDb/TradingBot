import os

# ── Kill LumiBot noise at the source (must be set BEFORE lumibot imports) ──────
# LUMIBOT_TELEMETRY=false disables the telemetry emitter thread entirely (the
# 5-min JSON spam). LUMIBOT_LOG_LEVEL=ERROR sets lumibot's own console handler to
# ERROR-only — lumibot re-applies this level on every internal reconfigure, so
# calling setLevel() from our side after import gets clobbered; the env var wins.
os.environ.setdefault("LUMIBOT_TELEMETRY", "false")
os.environ.setdefault("LUMIBOT_LOG_LEVEL", "ERROR")

import logging
import warnings

# alpaca-py's trading-stream module still calls asyncio.iscoroutinefunction, which
# Python 3.12+ deprecates (removal slated for 3.16). It's alpaca's code, not ours,
# fixed in their newer releases — purely cosmetic here, so silence just that one.
warnings.filterwarnings("ignore", message=".*iscoroutinefunction.*",
                        category=DeprecationWarning)

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

# ── Dedupe "Skipping malformed order X" spam ──────────────────────────────────
# Alpaca keeps old bracket/OCO order history with no child order prices. Lumibot's
# own _parse_broker_order (alpaca.py) catches this INTERNALLY and calls
# logger.warning(...) itself before returning None — it never raises, so the
# try/except patch below (which assumes it raises) never actually engages for this
# case. Every broker sync re-parses the same ~30 dead orders and re-warns on all of
# them, forever, on whatever schedule lumibot syncs on (independent of market hours
# or on_trading_iteration). Dedupe by order ID at the log-record level instead: each
# ID's warning gets through once, then stays silent for the rest of the run.
import re as _re
_MALFORMED_ORDER_RE = _re.compile(r"Skipping malformed order (\S+)")
_seen_malformed_order_ids: set = set()

class _DedupMalformedOrderFilter(logging.Filter):
    # Dead legacy bracket/OCO orders in the account history each warn once per run
    # under pure per-ID dedup — with ~40 of them that's still a 40-line wall at every
    # startup sync. Show the first few as a heads-up, then suppress the rest.
    _MAX_SHOWN = 3

    def filter(self, record):
        try:
            m = _MALFORMED_ORDER_RE.search(record.getMessage())
        except Exception:
            return True
        if not m:
            return True
        order_id = m.group(1)
        if order_id in _seen_malformed_order_ids:
            return False
        _seen_malformed_order_ids.add(order_id)
        if len(_seen_malformed_order_ids) == self._MAX_SHOWN + 1:
            print("[LOG] More malformed legacy orders skipped — suppressing the rest "
                  "(dead bracket/OCO orders from old test runs; lumibot ignores them safely).",
                  flush=True)
        return len(_seen_malformed_order_ids) <= self._MAX_SHOWN

_DEDUP_MALFORMED = _DedupMalformedOrderFilter()

def _add_filters(logger_obj):
    for f in (_QUIET, _DEDUP_MALFORMED):
        if f not in logger_obj.filters:
            logger_obj.addFilter(f)
    for h in logger_obj.handlers:
        for f in (_QUIET, _DEDUP_MALFORMED):
            if f not in h.filters:
                h.addFilter(f)

def quiet_logging():
    """(Re)apply noise suppression. Safe to call repeatedly — call again after Lumibot
    has set up its own log handlers so the handler-level filter actually takes effect."""
    # Suppress all lumibot INFO/WARNING at the logger level — telemetry, balance
    # polling errors, and websocket churn are all INFO; real failures are ERROR+.
    lumibot_logger = logging.getLogger("lumibot")
    lumibot_logger.setLevel(logging.ERROR)
    # Lumibot attaches its OWN console/file handlers directly to the "lumibot" logger
    # (not the true root) — a filter only on root's handlers never sees those records
    # at all, since they're already printed by lumibot's own handler before propagating.
    _add_filters(lumibot_logger)
    _add_filters(logging.getLogger())
    # Third-party libraries lumibot depends on (e.g. alpaca-py's own trading-stream
    # websocket reconnect logging) use their OWN logger namespace ("alpaca.trading.*",
    # not "lumibot"), which propagates straight to the true root. With no handler left
    # on root, Python's own logging.lastResort fallback prints it raw — no formatting,
    # and it never passes through either filter above. Filter that fallback directly.
    for f in (_QUIET, _DEDUP_MALFORMED):
        if f not in logging.lastResort.filters:
            logging.lastResort.addFilter(f)
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
    _warned_order_ids: set = set()   # suppress repeat prints for same stale orders

    def _safe_parse_broker_order(self, response, strategy_name, strategy_object=None):
        try:
            return _orig_parse(self, response, strategy_name, strategy_object)
        except (ValueError, TypeError) as e:
            order_id = (response.get("id", "?") if isinstance(response, dict)
                        else getattr(response, "id", "?"))
            if order_id not in _warned_order_ids:
                _warned_order_ids.add(order_id)
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

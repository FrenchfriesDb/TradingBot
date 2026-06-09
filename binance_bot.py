"""
Binance CCXT bot — continuous loop with Debbie-La SMC state machine.
Runs on Binance testnet (paper) or live depending on BINANCE_TESTNET in .env.
"""

import time
import math
import sys
import os
import pandas as pd
from datetime import datetime

# ── Patch ccxt _version.py bug before importing ccxt ──────────────────────────
# ccxt 4.5.x has a bug where git_pieces_from_vcs returns None for count_out.
def _patch_ccxt():
    try:
        import ccxt
        import importlib.util
        vpath = os.path.join(
            os.path.dirname(ccxt.__file__),
            "static_dependencies", "toolz", "_version.py"
        )
        if os.path.exists(vpath):
            txt = open(vpath).read()
            patched = txt.replace(
                'pieces["distance"] = int(count_out)',
                'pieces["distance"] = int(count_out) if count_out is not None else 0'
            )
            if patched != txt:
                open(vpath, "w").write(patched)
    except Exception:
        pass

_patch_ccxt()
import ccxt  # noqa: E402 (import after patch)

from config import (
    BINANCE_API_KEY,
    BINANCE_SECRET,
    BINANCE_TESTNET,
    BINANCE_SYMBOL,
    BINANCE_CASH_AT_RISK,
)
from bot import indicators
from finbert_utils import estimate_sentiment

SLEEP_SECONDS = 15 * 60      # 15 minutes between iterations
STALE_TRADE_HOURS = 4        # Force-close after 4 hours

# ── Exchange connection ────────────────────────────────────────────────────────

def connect_binance():
    exchange = ccxt.binance({
        "apiKey": BINANCE_API_KEY,
        "secret": BINANCE_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    if BINANCE_TESTNET:
        exchange.set_sandbox_mode(True)
    return exchange

# ── Helpers ────────────────────────────────────────────────────────────────────

def ohlcv_to_df(ohlcv):
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df

def fetch_balance(exchange, quote="USDT"):
    bal = exchange.fetch_balance()
    return float(bal.get("free", {}).get(quote, 0.0))

def position_size(balance_usdt, price, risk_fraction):
    if price <= 0:
        return 0.0
    return (balance_usdt * risk_fraction) / price

def get_sentiment(headlines: list):
    if not headlines:
        return True, "no_news", 0.0
    try:
        prob, sentiment = estimate_sentiment(headlines)
        return (sentiment == "positive" and prob >= 0.55), sentiment, prob
    except Exception:
        return True, "error", 0.0

# ── State machine ──────────────────────────────────────────────────────────────

class SymbolState:
    def __init__(self):
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
        self.entry_time = None
        self.order_id = None

    def reset(self):
        self.__init__()


def process_symbol(exchange, symbol: str, state: SymbolState, risk_fraction: float):
    base = symbol.split("/")[0]
    now = datetime.utcnow()

    # ── Fetch data ─────────────────────────────────────────────────────────────
    try:
        ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe="15m", limit=55)
        ohlcv_4h  = exchange.fetch_ohlcv(symbol, timeframe="4h",  limit=105)
    except Exception as e:
        print(f"[{base}] Data fetch error: {e}")
        return

    df_ltf = ohlcv_to_df(ohlcv_15m)
    df_htf = ohlcv_to_df(ohlcv_4h)
    current_price = float(df_ltf["close"].iloc[-1])

    # ── Check live position ────────────────────────────────────────────────────
    try:
        positions = exchange.fetch_balance().get("total", {})
        held_qty = float(positions.get(base, 0.0))
    except Exception:
        held_qty = 0.0

    has_position = held_qty > 0.0001  # small dust threshold

    # ── POSITION_OPEN: manage or exit ──────────────────────────────────────────
    if state.state == "POSITION_OPEN":
        if not has_position:
            print(f"[{base}] Position closed externally. Resetting.")
            state.reset()
            return

        # Stale-trade exit
        if state.entry_time:
            elapsed = (now - state.entry_time).total_seconds() / 3600
            if elapsed >= STALE_TRADE_HOURS:
                try:
                    exchange.create_order(symbol, "market", "sell", held_qty)
                    print(f"[{base}] ⏰ Stale exit after {elapsed:.1f}h @ {current_price:.4f}")
                except Exception as e:
                    print(f"[{base}] Stale-exit order error: {e}")
                state.reset()
        return

    # ── Indicators ────────────────────────────────────────────────────────────
    is_bos, direction = indicators.detect_displacement_bos(df_htf, lookback=15)
    is_sweep, support, sweep_wick = indicators.check_liquidity_sweep(df_ltf)
    is_mss, swing_high = indicators.check_market_structure_shift(df_ltf)
    is_fvg_bull, fvg_bot, fvg_top = indicators.find_bullish_fvg(df_ltf)
    is_fvg_bear, _, _ = indicators.find_bearish_fvg(df_ltf)
    is_choch, choch_dir = indicators.detect_choch(df_ltf, lookback=5)

    print(f"[{base}] {now.strftime('%H:%M')} price={current_price:.4f} "
          f"state={state.state} bos={is_bos} sweep={is_sweep} mss={is_mss} fvg={is_fvg_bull}")

    # ── STATE 1: IDLE ──────────────────────────────────────────────────────────
    if state.state == "IDLE" and not has_position:
        if is_bos and is_sweep:
            if is_fvg_bull:
                state.bias = "BULLISH"
                state.sweep_low = sweep_wick
                state.state = "SWEEP_HUNT"
                print(f"[{base}] STEP 1-2 → BULLISH sweep @ {sweep_wick:.4f}")
            elif is_fvg_bear:
                state.bias = "BEARISH"
                state.sweep_high = sweep_wick
                state.state = "SWEEP_HUNT"
                print(f"[{base}] STEP 1-2 → BEARISH sweep @ {sweep_wick:.4f}")

    # ── STATE 2: SWEEP_HUNT ────────────────────────────────────────────────────
    if state.state == "SWEEP_HUNT" and not has_position:
        if is_mss and is_choch and state.bias == "BULLISH" and choch_dir == "bullish":
            state.fvg_low = fvg_bot
            state.fvg_high = fvg_top
            _, state.ote_zone = indicators.calculate_fib_levels(state.sweep_low, swing_high)
            state.state = "ENTRY_WAIT"
            print(f"[{base}] STEP 3: MSS+FVG confirmed | "
                  f"OTE {state.ote_zone['lower']:.4f}-{state.ote_zone['upper']:.4f}")

    # ── STATE 3: ENTRY_WAIT ────────────────────────────────────────────────────
    if state.state == "ENTRY_WAIT" and not has_position:
        in_fvg = state.fvg_low <= current_price <= state.fvg_high
        in_ote = indicators.is_price_in_fib_ote(current_price, state.ote_zone)

        if in_fvg and in_ote:
            confirm, sentiment, prob = get_sentiment([])  # pass news headlines if available
            if prob > 0:
                print(f"[{base}] AI: {sentiment} ({prob*100:.1f}%) → {'✅' if confirm else '❌'}")
            else:
                print(f"[{base}] No news — proceeding on technicals.")

            if confirm:
                balance = fetch_balance(exchange)
                qty = position_size(balance, current_price, risk_fraction)
                qty = math.floor(qty * 1e6) / 1e6  # floor to 6 decimal places

                if qty > 0:
                    state.stop_loss = state.sweep_low * 0.98
                    risk = current_price - state.stop_loss
                    state.take_profit = current_price + (risk * 2)

                    try:
                        order = exchange.create_order(symbol, "market", "buy", qty)
                        state.state = "POSITION_OPEN"
                        state.entry_price = current_price
                        state.entry_time = now
                        state.order_id = order.get("id")
                        print(f"[{base}] ✅ ENTRY {current_price:.4f} | qty={qty} | "
                              f"SL={state.stop_loss:.4f} | TP={state.take_profit:.4f}")
                    except Exception as e:
                        print(f"[{base}] Order error: {e}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def run():
    print("=" * 70)
    print("🔴 DEBBIE-LA BINANCE BOT — CONTINUOUS MODE")
    print(f"   Testnet: {BINANCE_TESTNET} | Symbol: {BINANCE_SYMBOL}")
    print(f"   Risk/symbol: {BINANCE_CASH_AT_RISK*100:.1f}% | Interval: 15m")
    print("=" * 70)

    exchange = connect_binance()
    symbols = BINANCE_SYMBOL if isinstance(BINANCE_SYMBOL, list) else [BINANCE_SYMBOL]
    states = {s: SymbolState() for s in symbols}

    while True:
        for symbol in symbols:
            try:
                process_symbol(exchange, symbol, states[symbol], BINANCE_CASH_AT_RISK)
            except Exception as e:
                print(f"[{symbol}] Unexpected error: {e}")

        print(f"  ↩ sleeping {SLEEP_SECONDS//60}m …")
        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    run()

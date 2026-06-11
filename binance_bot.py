"""
CCXT bot — Debbie-La SMC state machine with paper trading.
Uses Kraken public data feed by default (no API key needed).
Set BINANCE_API_KEY/SECRET in .env to switch to real Binance trading.
"""

import time
import math
import os
import pandas as pd
from datetime import datetime, timezone

# ── Patch ccxt _version.py bug BEFORE importing ccxt ──────────────────────────
# ccxt 4.5.x crashes on import due to int(None) in toolz/_version.py.
# We locate the file via importlib (no import needed) and patch it on disk first.
def _patch_ccxt():
    try:
        import importlib.util
        spec = importlib.util.find_spec("ccxt")
        if spec is None:
            return
        ccxt_dir = os.path.dirname(spec.origin)
        vpath = os.path.join(ccxt_dir, "static_dependencies", "toolz", "_version.py")
        if not os.path.exists(vpath):
            return
        txt = open(vpath).read()
        patched = txt.replace(
            'pieces["distance"] = int(count_out)',
            'pieces["distance"] = int(count_out) if count_out is not None else 0'
        )
        if patched != txt:
            open(vpath, "w").write(patched)
            print("ccxt patched.")
    except Exception as e:
        print(f"ccxt patch skipped: {e}")

_patch_ccxt()
import ccxt

from config import BINANCE_API_KEY, BINANCE_SECRET, BINANCE_TESTNET, BINANCE_CASH_AT_RISK
from bot import indicators
from finbert_utils import estimate_sentiment

SLEEP_SECONDS = 5 * 60
STALE_TRADE_HOURS = 12
FVG_EXPIRY_BARS = 12   # reset ENTRY_WAIT if price hasn't tapped FVG within this many iterations
PAPER_BALANCE = 10_000.0

DEFAULT_SYMBOLS  = ["BTC/USD", "ETH/USD", "SOL/USD", "LINK/USD", "LTC/USD"]
CRYPTO_STATE_FILE = "crypto_state.json"


# ── Paper trader ───────────────────────────────────────────────────────────────

class PaperTrader:
    """
    Simulates long AND short trades against real price data.
    Positions dict: positive qty = long, negative qty = short.
    Entry prices tracked separately for short P&L calculation.
    """

    def __init__(self, balance: float = PAPER_BALANCE):
        self.balance = balance
        self.positions: dict = {}      # symbol -> qty (neg = short)
        self.entry_prices: dict = {}   # symbol -> avg entry price
        self.trade_count = 0

    def get_position(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def buy(self, symbol: str, qty: float, price: float):
        """Open long, or cover an existing short."""
        held = self.positions.get(symbol, 0.0)
        if held < 0:
            # Cover short: P&L = (entry - exit) * qty
            cover = min(qty, abs(held))
            pnl = (self.entry_prices.get(symbol, price) - price) * cover
            self.balance += pnl
            new_held = held + cover
            if abs(new_held) < 1e-9:
                self.positions.pop(symbol, None)
                self.entry_prices.pop(symbol, None)
            else:
                self.positions[symbol] = new_held
        else:
            # Open long
            cost = qty * price
            if cost > self.balance:
                qty = math.floor((self.balance * 0.95 / price) * 1e6) / 1e6
                cost = qty * price
            if qty <= 0:
                return None
            self.balance -= cost
            self.positions[symbol] = held + qty
            self.entry_prices[symbol] = price
        self.trade_count += 1
        return {"id": self.trade_count, "qty": qty, "price": price}

    def sell(self, symbol: str, qty: float, price: float):
        """Close an existing long, or open a short."""
        held = self.positions.get(symbol, 0.0)
        if held > 0:
            # Close long
            qty = min(qty, held)
            if qty <= 0:
                return None
            self.balance += qty * price
            new_held = held - qty
            if new_held < 1e-9:
                self.positions.pop(symbol, None)
                self.entry_prices.pop(symbol, None)
            else:
                self.positions[symbol] = new_held
        else:
            # Open short (paper — no margin needed, P&L settled on cover)
            self.positions[symbol] = -qty
            self.entry_prices[symbol] = price
        self.trade_count += 1
        return {"id": self.trade_count, "qty": qty, "price": price}


# ── Exchange connection ────────────────────────────────────────────────────────

def connect_exchange():
    """
    Kraken public feed if no API key — gives real crypto prices, no account needed.
    Falls back to Binance (testnet or live) when BINANCE_API_KEY is set in .env.
    """
    if BINANCE_API_KEY:
        ex = ccxt.binance({
            "apiKey": BINANCE_API_KEY,
            "secret": BINANCE_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })
        if BINANCE_TESTNET:
            ex.set_sandbox_mode(True)
        mode = "Binance testnet" if BINANCE_TESTNET else "Binance live"
    else:
        ex = ccxt.kraken({"enableRateLimit": True})
        mode = "Kraken (public data — paper trades only)"
    print(f"Exchange: {mode}")
    return ex


# ── Helpers ────────────────────────────────────────────────────────────────────

def ohlcv_to_df(ohlcv):
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


def save_crypto_state(paper: "PaperTrader", states: dict, symbols: list, prices: dict):
    """Write paper trader snapshot to JSON so monitor.py can display it."""
    try:
        positions = {}
        for sym, qty in paper.positions.items():
            if abs(qty) < 1e-9:
                continue
            entry = paper.entry_prices.get(sym, 0.0)
            cur   = prices.get(sym, 0.0)
            upnl  = (cur - entry) * qty if qty > 0 else (entry - cur) * abs(qty)
            positions[sym] = {
                "qty":           qty,
                "side":          "LONG" if qty > 0 else "SHORT",
                "entry_price":   entry,
                "current_price": cur,
                "unrealized_pnl": upnl,
            }
        data = {
            "last_updated":  datetime.now(timezone.utc).isoformat(),
            "balance":       paper.balance,
            "start_balance": PAPER_BALANCE,
            "trade_count":   paper.trade_count,
            "positions":     positions,
            "state_machine": {s: states[s].state for s in symbols},
        }
        import json as _json
        with open(CRYPTO_STATE_FILE, "w") as fh:
            _json.dump(data, fh, indent=2)
    except Exception as e:
        print(f"[STATE] save failed: {e}")


def get_sentiment(headlines: list):
    if not headlines:
        return True, "no_news", 0.0
    try:
        prob, sentiment = estimate_sentiment(headlines)
        confirm = not (sentiment == "negative" and prob >= 0.60)
        return confirm, sentiment, prob
    except Exception:
        return True, "error", 0.0


# ── Per-symbol state ───────────────────────────────────────────────────────────

class SymbolState:
    def __init__(self):
        self.state = "IDLE"
        self.bias = None
        self.sweep_low = None
        self.fvg_low = None
        self.fvg_high = None
        self.entry_price = None
        self.stop_loss = None
        self.take_profit = None
        self.entry_time = None
        self.bars_in_entry_wait = 0  # expire stale FVG zones

    def reset(self):
        self.__init__()


# ── Core strategy loop per symbol ─────────────────────────────────────────────

def process_symbol(exchange, paper: PaperTrader, symbol: str,
                   state: SymbolState, risk_fraction: float):
    base = symbol.split("/")[0]
    now = datetime.now(timezone.utc)

    try:
        df_ltf = ohlcv_to_df(exchange.fetch_ohlcv(symbol, "5m",  limit=55))
        df_htf = ohlcv_to_df(exchange.fetch_ohlcv(symbol, "4h",  limit=105))
    except Exception as e:
        print(f"[{base}] Data error: {e}")
        return None

    price = float(df_ltf["close"].iloc[-1])
    held  = paper.get_position(symbol)
    has_position = abs(held) > 1e-6

    is_bos,  direction   = indicators.detect_displacement_bos(df_htf, lookback=15)
    is_sweep, support, sweep_wick = indicators.check_liquidity_sweep(df_ltf)
    is_mss,  swing_high  = indicators.check_market_structure_shift(df_ltf)
    is_fvg_bull, fvg_bot, fvg_top         = indicators.find_bullish_fvg(df_ltf)
    is_fvg_bear, fvg_bear_bot, fvg_bear_top = indicators.find_bearish_fvg(df_ltf)

    print(f"[{base}] {now.strftime('%H:%M')} ${price:,.2f}  "
          f"state={state.state}  bos={is_bos}({direction})  "
          f"sweep={is_sweep}  mss={is_mss}  "
          f"fvg_bull={is_fvg_bull}  fvg_bear={is_fvg_bear}")

    # ── POSITION_OPEN ──────────────────────────────────────────────────────────
    if state.state == "POSITION_OPEN":
        if not has_position:
            print(f"[{base}] Position gone. Resetting.")
            state.reset()
            return price

        is_long = held > 0
        close_qty = abs(held)

        def close_position(reason):
            if is_long:
                paper.sell(symbol, close_qty, price)
                pnl = (price - state.entry_price) * close_qty
            else:
                paper.buy(symbol, close_qty, price)
                pnl = (state.entry_price - price) * close_qty
            direction_label = "LONG" if is_long else "SHORT"
            print(f"[{base}] {reason} ${price:,.2f}  {direction_label}  "
                  f"P&L: ${pnl:+.2f}  Balance: ${paper.balance:,.2f}")
            state.reset()

        # SL: long exits below SL, short exits above SL
        sl_hit = (is_long and price <= state.stop_loss) or \
                 (not is_long and price >= state.stop_loss)
        tp_hit = (is_long and price >= state.take_profit) or \
                 (not is_long and price <= state.take_profit)

        if sl_hit:
            close_position("🔴 SL hit")
            return price
        if tp_hit:
            close_position("🟢 TP hit")
            return price
        if state.entry_time:
            elapsed = (now - state.entry_time).total_seconds() / 3600
            if elapsed >= STALE_TRADE_HOURS:
                close_position(f"⏰ Stale {elapsed:.1f}h")
        return price

    # ── IDLE: fire on HTF BOS alone ────────────────────────────────────────────
    if state.state == "IDLE" and not has_position:
        if is_bos and direction == "bullish":
            state.bias = "BULLISH"
            state.state = "SWEEP_HUNT"
            print(f"[{base}] STEP 1: HTF BOS → BULLISH. Hunting sweep.")
        elif is_bos and direction == "bearish":
            state.bias = "BEARISH"
            state.state = "SWEEP_HUNT"
            print(f"[{base}] STEP 1: HTF BOS → BEARISH. Hunting sweep.")

    # ── SWEEP_HUNT: advance on FVG in bias direction (no MSS gate) ───────────
    if state.state == "SWEEP_HUNT" and not has_position:
        if is_sweep and sweep_wick:
            state.sweep_low = sweep_wick

        if state.bias == "BULLISH" and is_fvg_bull:
            state.fvg_low  = fvg_bot
            state.fvg_high = fvg_top
            state.state    = "ENTRY_WAIT"
            print(f"[{base}] STEP 2: Bullish FVG locked  "
                  f"${state.fvg_low:,.4f}-${state.fvg_high:,.4f}  "
                  f"sweep={'yes' if state.sweep_low else 'no'}")

        elif state.bias == "BEARISH" and is_fvg_bear:
            state.fvg_low  = fvg_bear_bot
            state.fvg_high = fvg_bear_top
            state.state    = "ENTRY_WAIT"
            print(f"[{base}] STEP 2: Bearish FVG locked  "
                  f"${state.fvg_low:,.4f}-${state.fvg_high:,.4f}  "
                  f"sweep={'yes' if state.sweep_low else 'no'}")

    # ── ENTRY_WAIT: enter when price taps into FVG ────────────────────────────
    if state.state == "ENTRY_WAIT" and not has_position:
        state.bars_in_entry_wait += 1
        if state.bars_in_entry_wait > FVG_EXPIRY_BARS:
            print(f"[{base}] FVG expired after {FVG_EXPIRY_BARS} bars — resetting.")
            state.reset()
            return price

        in_fvg = (state.fvg_low is not None and
                  state.fvg_low <= price <= state.fvg_high)

        if in_fvg:
            confirm, sentiment, prob = get_sentiment([])
            if prob > 0:
                print(f"[{base}] AI: {sentiment} ({prob*100:.1f}%) → {'✅' if confirm else '❌'}")

            if confirm:
                is_long = state.bias == "BULLISH"
                qty = math.floor((paper.balance * risk_fraction / price) * 1e6) / 1e6
                if qty > 0:
                    if is_long:
                        sweep_ref         = state.sweep_low or price * 0.98
                        state.stop_loss   = sweep_ref * 0.98
                        risk_amt          = price - state.stop_loss
                        state.take_profit = price + risk_amt * 3
                        trade = paper.buy(symbol, qty, price)
                        label = "LONG"
                    else:
                        state.stop_loss   = price * 1.02
                        risk_amt          = state.stop_loss - price
                        state.take_profit = price - risk_amt * 3
                        trade = paper.sell(symbol, qty, price)
                        label = "SHORT"

                    if trade:
                        state.state       = "POSITION_OPEN"
                        state.entry_price = price
                        state.entry_time  = now
                        print(f"[{base}] ✅ PAPER {label}  ${price:,.2f}  qty={qty:.6f}  "
                              f"SL=${state.stop_loss:,.2f}  TP=${state.take_profit:,.2f}  "
                              f"Balance: ${paper.balance:,.2f}")

    return price


# ── Main ───────────────────────────────────────────────────────────────────────

def run():
    symbols_env = os.getenv("BINANCE_SYMBOL", "")
    symbols = ([s.strip() for s in symbols_env.split(",")]
               if "," in symbols_env else
               [symbols_env] if symbols_env else DEFAULT_SYMBOLS)

    print("=" * 70)
    print("DEBBIE-LA CCXT BOT — PAPER TRADING (real Kraken data)")
    print(f"  Symbols:  {', '.join(symbols)}")
    print(f"  Balance:  ${PAPER_BALANCE:,.0f} USDT (paper)")
    print(f"  Risk:     {BINANCE_CASH_AT_RISK*100:.1f}% per symbol | Interval: 5m | LTF: 5m | HTF: 4h")
    print("=" * 70)

    exchange = connect_exchange()
    paper  = PaperTrader(PAPER_BALANCE)
    states = {s: SymbolState() for s in symbols}

    prices = {}
    while True:
        print(f"\n{'─'*60}")
        print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"{'─'*60}")
        for symbol in symbols:
            try:
                result = process_symbol(exchange, paper, symbol, states[symbol], BINANCE_CASH_AT_RISK)
                if result is not None:
                    prices[symbol] = result
            except Exception as e:
                print(f"[{symbol}] Error: {e}")

        positions_str = (
            "  ".join(f"{k.split('/')[0]}={v:.4f}" for k, v in paper.positions.items())
            or "none"
        )
        print(f"\n  Balance: ${paper.balance:,.2f}  |  Positions: {positions_str}")
        save_crypto_state(paper, states, symbols, prices)
        print(f"  Sleeping {SLEEP_SECONDS // 60}m …\n")
        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    run()

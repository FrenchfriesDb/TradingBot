"""
EMA Crossover Test Bot — stocks + crypto.
Stocks : SPY via Alpaca paper (NYSE hours, EMA 9/21 on 1m)
Crypto : BTC/ETH/SOL via Kraken public (24/7, EMA 9/21 on 5m, paper)
Purpose: confirm execution works on both pipelines before trusting the SMC bot.
"""

import os
import math
import time
import threading
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

API_KEY    = os.getenv("ALPACA_API_KEY", "")
API_SECRET = os.getenv("ALPACA_API_SECRET", "")
PAPER      = os.getenv("ALPACA_PAPER", "True").lower() in ("1", "true", "yes")

FAST           = 9
SLOW           = 21
STOCK_SYMBOL   = "SPY"
CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]
CRYPTO_BALANCE = 5_000.0
CRYPTO_RISK    = 0.05    # 5% per trade — generous for frequent test signals
CRYPTO_SLEEP   = 5 * 60  # 5 minutes


# ── Paper trader (crypto side) ─────────────────────────────────────────────────

class PaperTrader:
    def __init__(self, balance: float):
        self.balance       = balance
        self.positions     = {}   # symbol -> qty (negative = short)
        self.entry_prices  = {}
        self.trade_count   = 0

    def get_position(self, symbol):
        return self.positions.get(symbol, 0.0)

    def buy(self, symbol, qty, price):
        held = self.positions.get(symbol, 0.0)
        if held < 0:
            cover = min(qty, abs(held))
            self.balance += (self.entry_prices.get(symbol, price) - price) * cover
            new_held = held + cover
            if abs(new_held) < 1e-9:
                self.positions.pop(symbol, None); self.entry_prices.pop(symbol, None)
            else:
                self.positions[symbol] = new_held
        else:
            cost = qty * price
            if cost > self.balance:
                qty  = math.floor((self.balance * 0.95 / price) * 1e6) / 1e6
                cost = qty * price
            if qty <= 0:
                return None
            self.balance -= cost
            self.positions[symbol]    = held + qty
            self.entry_prices[symbol] = price
        self.trade_count += 1
        return {"id": self.trade_count, "qty": qty, "price": price}

    def sell(self, symbol, qty, price):
        held = self.positions.get(symbol, 0.0)
        if held > 0:
            qty = min(qty, held)
            if qty <= 0:
                return None
            self.balance += qty * price
            new_held = held - qty
            if new_held < 1e-9:
                self.positions.pop(symbol, None); self.entry_prices.pop(symbol, None)
            else:
                self.positions[symbol] = new_held
        else:
            self.positions[symbol]    = -qty
            self.entry_prices[symbol] = price
        self.trade_count += 1
        return {"id": self.trade_count, "qty": qty, "price": price}


# ── ccxt patch ─────────────────────────────────────────────────────────────────

def _patch_ccxt():
    try:
        import importlib.util
        spec = importlib.util.find_spec("ccxt")
        if spec is None:
            return
        vpath = os.path.join(os.path.dirname(spec.origin),
                             "static_dependencies", "toolz", "_version.py")
        if not os.path.exists(vpath):
            return
        txt = open(vpath).read()
        patched = txt.replace(
            'pieces["distance"] = int(count_out)',
            'pieces["distance"] = int(count_out) if count_out is not None else 0'
        )
        if patched != txt:
            open(vpath, "w").write(patched)
    except Exception:
        pass


def ohlcv_to_df(ohlcv):
    df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.set_index("timestamp")


# ── Crypto EMA loop ────────────────────────────────────────────────────────────

def run_crypto_ema():
    _patch_ccxt()
    import ccxt

    exchange = ccxt.kraken({"enableRateLimit": True})
    paper    = PaperTrader(CRYPTO_BALANCE)

    print(f"[CRYPTO] EMA{FAST}/{SLOW} on "
          f"{', '.join(s.split('/')[0] for s in CRYPTO_SYMBOLS)} | 5m | "
          f"${CRYPTO_BALANCE:,.0f} paper balance")

    while True:
        ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
        for symbol in CRYPTO_SYMBOLS:
            base = symbol.split("/")[0]
            try:
                df    = ohlcv_to_df(exchange.fetch_ohlcv(symbol, "5m", limit=SLOW + 5))
                close = df["close"]
                price = float(close.iloc[-1])
                fast  = close.ewm(span=FAST, adjust=False).mean()
                slow  = close.ewm(span=SLOW, adjust=False).mean()

                bull_cross = fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]
                bear_cross = fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]
                held       = paper.get_position(symbol)
                side_label = "LONG" if held > 0 else "SHORT" if held < 0 else "flat"

                print(f"[{base}] {ts}  ${price:,.2f}  "
                      f"EMA{FAST}={fast.iloc[-1]:,.3f}  EMA{SLOW}={slow.iloc[-1]:,.3f}  "
                      f"pos={side_label}  bull={bull_cross}  bear={bear_cross}")

                if bull_cross:
                    if held < 0:
                        entry = paper.entry_prices.get(symbol, price)
                        paper.buy(symbol, abs(held), price)
                        pnl = (entry - price) * abs(held)
                        print(f"[{base}] ↩ Covered SHORT @ ${price:,.2f}  P&L: ${pnl:+.2f}")
                    if paper.get_position(symbol) <= 0:
                        qty = math.floor((paper.balance * CRYPTO_RISK / price) * 1e6) / 1e6
                        if qty > 0 and paper.buy(symbol, qty, price):
                            print(f"[{base}] ✅ PAPER LONG  ${price:,.2f}  "
                                  f"qty={qty:.6f}  Balance: ${paper.balance:,.2f}")

                elif bear_cross:
                    if held > 0:
                        entry = paper.entry_prices.get(symbol, price)
                        paper.sell(symbol, held, price)
                        pnl = (price - entry) * held
                        print(f"[{base}] ↩ Closed LONG @ ${price:,.2f}  P&L: ${pnl:+.2f}")
                    if paper.get_position(symbol) >= 0:
                        qty = math.floor((paper.balance * CRYPTO_RISK / price) * 1e6) / 1e6
                        if qty > 0 and paper.sell(symbol, qty, price):
                            print(f"[{base}] 🔴 PAPER SHORT ${price:,.2f}  "
                                  f"qty={qty:.6f}  Balance: ${paper.balance:,.2f}")

            except Exception as e:
                print(f"[{base}] Error: {e}")

        pos_str = "  ".join(
            f"{k.split('/')[0]}={'L' if v>0 else 'S'}{abs(v):.4f}"
            for k, v in paper.positions.items()
        ) or "flat"
        print(f"  [CRYPTO] Balance: ${paper.balance:,.2f}  |  {pos_str}\n")
        time.sleep(CRYPTO_SLEEP)


# ── Stock EMA bot (lumibot + Alpaca) ──────────────────────────────────────────

def run_stock_ema():
    try:
        from lumibot.strategies import Strategy
        from lumibot.entities import Asset, Order
        from lumibot.brokers import Alpaca
        from lumibot.traders import Trader

        class EMATestBot(Strategy):
            def initialize(self):
                self.sleeptime = "1M"

            def on_trading_iteration(self):
                asset    = Asset(STOCK_SYMBOL, asset_type=Asset.AssetType.STOCK)
                price    = self.get_last_price(STOCK_SYMBOL)
                position = self.get_position(asset)

                bars = self.get_historical_prices(asset, SLOW + 5, "1 minute")
                if bars is None:
                    return
                close = bars.pandas_df["close"]
                if len(close) < SLOW + 2:
                    return

                fast = close.ewm(span=FAST, adjust=False).mean()
                slow = close.ewm(span=SLOW, adjust=False).mean()

                bull_cross = fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]
                bear_cross = fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]

                self.log_message(
                    f"[{STOCK_SYMBOL}] ${price:.2f}  "
                    f"EMA{FAST}={fast.iloc[-1]:.3f}  EMA{SLOW}={slow.iloc[-1]:.3f}  "
                    f"bull={bull_cross}  bear={bear_cross}"
                )

                if bull_cross:
                    if position and position.quantity < 0:
                        self.submit_order(
                            self.create_order(asset, abs(position.quantity), Order.OrderSide.BUY))
                    if not position or position.quantity <= 0:
                        self.submit_order(self.create_order(asset, 1, Order.OrderSide.BUY))
                        self.log_message(f"[{STOCK_SYMBOL}] ✅ BUY @ ${price:.2f}", color="green")

                elif bear_cross:
                    if position and position.quantity > 0:
                        self.submit_order(
                            self.create_order(asset, position.quantity, Order.OrderSide.SELL))
                        self.log_message(f"[{STOCK_SYMBOL}] 🔴 SELL @ ${price:.2f}", color="red")

        print(f"[STOCKS] EMA{FAST}/{SLOW} on {STOCK_SYMBOL} | 1m | Alpaca paper "
              f"(waits for NYSE open 9:30 AM ET)")
        broker   = Alpaca({"API_KEY": API_KEY, "API_SECRET": API_SECRET, "PAPER": PAPER})
        strategy = EMATestBot(broker=broker)
        trader   = Trader()
        trader.add_strategy(strategy)
        trader.run_all()

    except Exception as e:
        print(f"[STOCKS] Failed to start: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 65)
    print("EMA CROSSOVER TEST BOT — STOCKS + CRYPTO")
    print(f"  Stocks : {STOCK_SYMBOL} via Alpaca paper  (fires at NYSE open)")
    print(f"  Crypto : {', '.join(s.split('/')[0] for s in CRYPTO_SYMBOLS)} via Kraken  (24/7)")
    print(f"  EMAs   : {FAST} / {SLOW}  |  Stock: 1m bars  |  Crypto: 5m bars")
    print("=" * 65)

    # Stock bot runs in a background thread (lumibot blocks internally)
    threading.Thread(target=run_stock_ema, daemon=True).start()

    # Crypto bot runs in the main thread
    run_crypto_ema()

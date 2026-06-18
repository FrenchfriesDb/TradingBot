"""
EMA Crossover Test Bot — stocks + crypto.
Stocks : IWM via Alpaca paper (NYSE hours, EMA 9/21 on 1m)
Crypto : BTC only via Kraken public (24/7, EMA 9/21 on 5m, paper)
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
STOCK_SYMBOL   = "IWM"  # kept off DebbieLaSMC's watchlist on purpose — avoids both bots trading the same ticker
STOCK_SL_PCT   = 0.005   # SL = 0.5% from entry
STOCK_RR       = 3       # 1:3 R:R → TP = 1.5% from entry
CRYPTO_SYMBOLS = ["BTC/USD"]
CRYPTO_BALANCE = 5_000.0
CRYPTO_RISK    = 0.05    # 5% of balance per trade
CRYPTO_SL_PCT  = 0.015   # SL = 1.5% from entry
CRYPTO_RR      = 6       # 1:6 R:R  →  TP = 9% from entry
CRYPTO_SLEEP   = 5 * 60  # 5 minutes
TEST_STATE_FILE = "test_state.json"


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


def save_test_state(paper, sl_levels, tp_levels, prices):
    import json
    try:
        positions = {}
        for sym, qty in paper.positions.items():
            if abs(qty) < 1e-9:
                continue
            entry = paper.entry_prices.get(sym, 0.0)
            cur   = prices.get(sym, 0.0)
            sl    = sl_levels.get(sym)
            tp    = tp_levels.get(sym)
            is_long = qty > 0
            upnl  = (cur - entry) * qty if is_long else (entry - cur) * abs(qty)
            risk  = abs(entry - sl) * abs(qty) if sl else None
            reward = abs(tp - entry) * abs(qty) if tp else None
            positions[sym] = {
                "qty": qty, "side": "LONG" if is_long else "SHORT",
                "entry_price": entry, "current_price": cur,
                "stop_loss": sl, "take_profit": tp,
                "unrealized_pnl": upnl,
                "risk_dollars": risk, "reward_dollars": reward,
            }
        data = {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "bot": "EMATestBot",
            "balance": paper.balance,
            "start_balance": CRYPTO_BALANCE,
            "trade_count": paper.trade_count,
            "positions": positions,
            "live_prices": {s: prices.get(s, 0) for s in CRYPTO_SYMBOLS},
        }
        with open(TEST_STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[STATE] save failed: {e}")


def ohlcv_to_df(ohlcv):
    df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.set_index("timestamp")


# ── Crypto EMA loop ────────────────────────────────────────────────────────────

def run_crypto_ema():
    _patch_ccxt()
    import ccxt

    exchange = ccxt.coinbase({"enableRateLimit": True})
    paper    = PaperTrader(CRYPTO_BALANCE)

    # SL/TP tracked per symbol — crossover opens the trade, price closes it
    sl_levels: dict  = {s: None for s in CRYPTO_SYMBOLS}
    tp_levels: dict  = {s: None for s in CRYPTO_SYMBOLS}
    live_prices: dict = {s: 0.0  for s in CRYPTO_SYMBOLS}

    print(f"[CRYPTO] EMA{FAST}/{SLOW} on "
          f"{', '.join(s.split('/')[0] for s in CRYPTO_SYMBOLS)} | 5m | "
          f"${CRYPTO_BALANCE:,.0f} paper  |  SL={CRYPTO_SL_PCT*100:.1f}%  "
          f"TP={CRYPTO_SL_PCT*CRYPTO_RR*100:.1f}%  (1:{CRYPTO_RR} R:R)")

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
                live_prices[symbol] = price
                side_label = "LONG" if held > 0 else "SHORT" if held < 0 else "flat"

                sl = sl_levels[symbol]
                tp = tp_levels[symbol]
                sl_str = f"  SL=${sl:,.2f}  TP=${tp:,.2f}" if sl else ""
                print(f"[{base}] {ts}  ${price:,.2f}  "
                      f"EMA{FAST}={fast.iloc[-1]:,.3f}  EMA{SLOW}={slow.iloc[-1]:,.3f}  "
                      f"pos={side_label}{sl_str}  bull={bull_cross}  bear={bear_cross}")

                # ── Check SL/TP first — price-based exits only ─────────────────
                if held != 0 and sl and tp:
                    is_long = held > 0
                    entry   = paper.entry_prices.get(symbol, price)
                    hit     = None
                    if is_long  and price <= sl: hit = ("SL", "red")
                    elif is_long  and price >= tp: hit = ("TP", "green")
                    elif not is_long and price >= sl: hit = ("SL", "red")
                    elif not is_long and price <= tp: hit = ("TP", "green")

                    if hit:
                        label, _ = hit
                        if is_long:
                            paper.sell(symbol, abs(held), price)
                            pnl = (price - entry) * abs(held)
                        else:
                            paper.buy(symbol, abs(held), price)
                            pnl = (entry - price) * abs(held)
                        icon = "🟢" if label == "TP" else "🔴"
                        print(f"[{base}] {icon} {label} hit @ ${price:,.2f}  "
                              f"P&L: ${pnl:+.2f}  Balance: ${paper.balance:,.2f}")
                        sl_levels[symbol] = None
                        tp_levels[symbol] = None
                    continue  # don't look for new entries mid-trade

                # ── EMA crossover opens new position (only when flat) ──────────
                if bull_cross and held <= 0:
                    qty = math.floor((paper.balance * CRYPTO_RISK / price) * 1e6) / 1e6
                    if qty > 0 and paper.buy(symbol, qty, price):
                        sl_levels[symbol] = round(price * (1 - CRYPTO_SL_PCT), 4)
                        tp_levels[symbol] = round(price * (1 + CRYPTO_SL_PCT * CRYPTO_RR), 4)
                        print(f"[{base}] ✅ PAPER LONG  ${price:,.2f}  qty={qty:.6f}  "
                              f"SL=${sl_levels[symbol]:,.2f}  TP=${tp_levels[symbol]:,.2f}  "
                              f"Balance: ${paper.balance:,.2f}")

                elif bear_cross and held >= 0:
                    qty = math.floor((paper.balance * CRYPTO_RISK / price) * 1e6) / 1e6
                    if qty > 0 and paper.sell(symbol, qty, price):
                        sl_levels[symbol] = round(price * (1 + CRYPTO_SL_PCT), 4)
                        tp_levels[symbol] = round(price * (1 - CRYPTO_SL_PCT * CRYPTO_RR), 4)
                        print(f"[{base}] 🔴 PAPER SHORT ${price:,.2f}  qty={qty:.6f}  "
                              f"SL=${sl_levels[symbol]:,.2f}  TP=${tp_levels[symbol]:,.2f}  "
                              f"Balance: ${paper.balance:,.2f}")

            except Exception as e:
                print(f"[{base}] Error: {e}")

        pos_str = "  ".join(
            f"{k.split('/')[0]}={'L' if v>0 else 'S'}{abs(v):.4f}"
            for k, v in paper.positions.items()
        ) or "flat"
        print(f"  [CRYPTO] Balance: ${paper.balance:,.2f}  |  {pos_str}\n")
        save_test_state(paper, sl_levels, tp_levels, live_prices)
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
                self.sleeptime    = "1M"
                self.entry_order  = None
                self.sl_order     = None
                self.tp_order     = None
                self.stop_loss    = None
                self.take_profit  = None

            def _cancel_resting_orders(self):
                for attr in ("sl_order", "tp_order"):
                    order = getattr(self, attr)
                    if order is not None:
                        try:
                            self.cancel_order(order)
                        except Exception:
                            pass
                        setattr(self, attr, None)
                self.stop_loss   = None
                self.take_profit = None

            def on_filled_order(self, position, order, price, quantity, multiplier):
                # Single OCO order, not two separate stop + limit orders — submitting
                # them separately makes Alpaca hold the full share count against the
                # first one, so the second always gets rejected with "insufficient
                # qty available" (fails async, after we've already logged success).
                if order is not self.entry_order:
                    return
                self.entry_order = None
                asset = order.asset

                sl = round(price * (1 - STOCK_SL_PCT), 2)
                tp = round(price * (1 + STOCK_SL_PCT * STOCK_RR), 2)
                self.stop_loss   = sl
                self.take_profit = tp

                try:
                    oco_order = self.create_order(
                        asset, abs(quantity), Order.OrderSide.SELL,
                        order_class=Order.OrderClass.OCO,
                        limit_price=tp,
                        stop_price=sl,
                    )
                    self.submit_order(oco_order)
                    self.sl_order = oco_order
                    self.tp_order = oco_order
                    self.log_message(
                        f"[{STOCK_SYMBOL}] 🛑🎯 OCO order placed — SL @ ${sl:.2f}  TP @ ${tp:.2f}",
                        color="yellow"
                    )
                except Exception as e:
                    self.log_message(f"[{STOCK_SYMBOL}] OCO order failed: {e}", color="red")

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

                sl_str = f"  SL=${self.stop_loss:.2f}  TP=${self.take_profit:.2f}" if self.stop_loss else ""
                self.log_message(
                    f"[{STOCK_SYMBOL}] ${price:.2f}  "
                    f"EMA{FAST}={fast.iloc[-1]:.3f}  EMA{SLOW}={slow.iloc[-1]:.3f}  "
                    f"bull={bull_cross}  bear={bear_cross}{sl_str}"
                )

                # Manual SL/TP check — closes the position if the resting broker order
                # hasn't filled yet by the time we poll (keeps this in sync either way).
                if position and position.quantity > 0 and self.stop_loss and self.take_profit:
                    if price <= self.stop_loss or price >= self.take_profit:
                        label = "SL" if price <= self.stop_loss else "TP"
                        self._cancel_resting_orders()
                        self.submit_order(
                            self.create_order(asset, position.quantity, Order.OrderSide.SELL))
                        self.log_message(f"[{STOCK_SYMBOL}] {'🔴' if label=='SL' else '🟢'} {label} hit @ ${price:.2f}",
                                         color="red" if label == "SL" else "green")
                        return

                if bull_cross:
                    if position and position.quantity < 0:
                        self.submit_order(
                            self.create_order(asset, abs(position.quantity), Order.OrderSide.BUY))
                    if not position or position.quantity <= 0:
                        order = self.create_order(asset, 1, Order.OrderSide.BUY)
                        self.entry_order = order
                        self.submit_order(order)
                        self.log_message(f"[{STOCK_SYMBOL}] ✅ BUY @ ${price:.2f}", color="green")

                elif bear_cross:
                    if position and position.quantity > 0:
                        self._cancel_resting_orders()
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
    print(f"  Crypto : {', '.join(s.split('/')[0] for s in CRYPTO_SYMBOLS)} via Coinbase  (24/7)")
    print(f"  EMAs   : {FAST} / {SLOW}  |  Stock: 1m bars  |  Crypto: 5m bars")
    print("=" * 65)

    # Stock bot runs in a background thread (lumibot blocks internally)
    threading.Thread(target=run_stock_ema, daemon=True).start()

    # Crypto bot runs in the main thread
    run_crypto_ema()

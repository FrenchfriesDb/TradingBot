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

POOL_LOOKBACK_1H     = 24        # 1H candles used to mark the swing high/low anchor
POOL_RECALC_SECONDS  = 60 * 60   # recompute the 1H pools once per hour
SL_BUFFER_PCT        = 0.0005    # stop sits this far beyond the spike wick
MIN_RR               = 3.0       # skip the trade if implied R:R is below this
RISK_PCT             = 0.01      # 1% of balance/cash risked per trade
STOCK_SYMBOL   = "IWM"  # kept off DebbieLaSMC's watchlist on purpose — avoids both bots trading the same ticker
CRYPTO_SYMBOLS = ["BTC/USD"]
CRYPTO_BALANCE = 5_000.0
POLL_SECONDS   = 60      # 1-minute poll — matches the 1M sniper timeframe
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


def save_test_state(paper, sl_levels, tp_levels, prices, pools, trade_states):
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
        pool_data = {
            sym: {
                "pool_high": pools[sym]["high"],
                "pool_low":  pools[sym]["low"],
                "state":     trade_states.get(sym),
            }
            for sym in pools
        }
        data = {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "bot": "SweepTestBot",
            "balance": paper.balance,
            "start_balance": CRYPTO_BALANCE,
            "trade_count": paper.trade_count,
            "positions": positions,
            "pools": pool_data,
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


def compute_pools(df_1h: pd.DataFrame, lookback: int = POOL_LOOKBACK_1H):
    """Returns (pool_high, pool_low) — the swing high/low over the last `lookback` 1H candles."""
    window = df_1h.tail(lookback)
    return float(window["high"].max()), float(window["low"].min())


def detect_sweep(candle_high: float, candle_low: float, candle_close: float,
                  pool_high: float, pool_low: float):
    """Returns 'SHORT', 'LONG', or None — a wick past the pool that closes back inside it."""
    if candle_high > pool_high and candle_close <= pool_high:
        return "SHORT"
    if candle_low < pool_low and candle_close >= pool_low:
        return "LONG"
    return None


def compute_stop_target(direction: str, candle_high: float, candle_low: float,
                         pool_high: float, pool_low: float,
                         sl_buffer_pct: float = SL_BUFFER_PCT):
    """Returns (stop_loss, take_profit) for the given sweep direction."""
    if direction == "SHORT":
        return candle_high * (1 + sl_buffer_pct), pool_low
    return candle_low * (1 - sl_buffer_pct), pool_high


def compute_rr(entry: float, sl: float, tp: float) -> float:
    """Risk:reward as a plain float (3.0 means 1:3). Returns 0.0 if risk <= 0."""
    risk   = abs(entry - sl)
    reward = abs(tp - entry)
    return reward / risk if risk > 0 else 0.0


def size_position(balance: float, risk_pct: float, entry: float, sl: float) -> float:
    """Qty sized so a stop-out loses exactly risk_pct of balance. Returns 0.0 if risk <= 0."""
    risk_per_unit = abs(entry - sl)
    if risk_per_unit <= 0:
        return 0.0
    qty = (balance * risk_pct) / risk_per_unit
    return math.floor(qty * 1e6) / 1e6


# ── Crypto sweep-reversal loop ────────────────────────────────────────────────

def run_crypto_sweep():
    _patch_ccxt()
    import ccxt

    exchange = ccxt.coinbase({"enableRateLimit": True})
    paper    = PaperTrader(CRYPTO_BALANCE)

    pools        = {s: {"high": None, "low": None} for s in CRYPTO_SYMBOLS}
    sl_levels    = {s: None for s in CRYPTO_SYMBOLS}
    tp_levels    = {s: None for s in CRYPTO_SYMBOLS}
    trade_states = {s: "RETIRED" for s in CRYPTO_SYMBOLS}   # forces a pool recalc on the first tick
    last_recalc  = {s: 0.0 for s in CRYPTO_SYMBOLS}
    live_prices  = {s: 0.0 for s in CRYPTO_SYMBOLS}

    print(f"[CRYPTO] 1H/1M Sweep-Reversal on "
          f"{', '.join(s.split('/')[0] for s in CRYPTO_SYMBOLS)} | "
          f"${CRYPTO_BALANCE:,.0f} paper  |  risk={RISK_PCT*100:.0f}%  "
          f"min R:R=1:{MIN_RR:.0f}")

    while True:
        ts  = datetime.now(timezone.utc).strftime("%H:%M UTC")
        now = time.time()
        for symbol in CRYPTO_SYMBOLS:
            base = symbol.split("/")[0]
            try:
                if trade_states[symbol] != "IN_TRADE" and now - last_recalc[symbol] >= POOL_RECALC_SECONDS:
                    df_1h = ohlcv_to_df(exchange.fetch_ohlcv(symbol, "1h", limit=POOL_LOOKBACK_1H))
                    pool_high, pool_low = compute_pools(df_1h, POOL_LOOKBACK_1H)
                    pools[symbol]["high"] = pool_high
                    pools[symbol]["low"]  = pool_low
                    last_recalc[symbol]   = now
                    trade_states[symbol]  = "WATCHING"
                    print(f"[{base}] \U0001F553 1H recalc — pool_high=${pool_high:,.2f}  pool_low=${pool_low:,.2f}")

                pool_high = pools[symbol]["high"]
                pool_low  = pools[symbol]["low"]
                df_1m  = ohlcv_to_df(exchange.fetch_ohlcv(symbol, "1m", limit=2))
                candle = df_1m.iloc[-1]
                price  = float(candle["close"])
                live_prices[symbol] = price
                held = paper.get_position(symbol)

                print(f"[{base}] {ts}  ${price:,.2f}  pool_high=${pool_high}  "
                      f"pool_low=${pool_low}  state={trade_states[symbol]}")

                # ── Manage an open trade ────────────────────────────────────────
                if held != 0:
                    sl = sl_levels[symbol]
                    tp = tp_levels[symbol]
                    is_long = held > 0
                    entry   = paper.entry_prices.get(symbol, price)
                    hit = None
                    if is_long and price <= sl: hit = "SL"
                    elif is_long and price >= tp: hit = "TP"
                    elif not is_long and price >= sl: hit = "SL"
                    elif not is_long and price <= tp: hit = "TP"

                    if hit:
                        if is_long:
                            paper.sell(symbol, abs(held), price)
                            pnl = (price - entry) * abs(held)
                        else:
                            paper.buy(symbol, abs(held), price)
                            pnl = (entry - price) * abs(held)
                        icon = "\U0001F7E2" if hit == "TP" else "\U0001F534"
                        print(f"[{base}] {icon} {hit} hit @ ${price:,.2f}  "
                              f"P&L: ${pnl:+.2f}  Balance: ${paper.balance:,.2f}")
                        sl_levels[symbol] = None
                        tp_levels[symbol] = None
                        trade_states[symbol] = "RETIRED"   # stays retired until the next 1H recalc
                    continue

                # ── Look for a new sweep+reversal entry ─────────────────────────
                if trade_states[symbol] != "WATCHING" or pool_high is None:
                    continue

                direction = detect_sweep(float(candle["high"]), float(candle["low"]),
                                          float(candle["close"]), pool_high, pool_low)
                if direction is None:
                    continue

                sl, tp = compute_stop_target(direction, float(candle["high"]), float(candle["low"]),
                                              pool_high, pool_low, SL_BUFFER_PCT)
                rr = compute_rr(price, sl, tp)
                if rr < MIN_RR:
                    print(f"[{base}] ⏭ Sweep {direction} skipped — R:R 1:{rr:.1f} < 1:{MIN_RR:.0f} min")
                    continue

                qty = size_position(paper.balance, RISK_PCT, price, sl)
                if qty <= 0:
                    print(f"[{base}] ⏭ Sweep {direction} skipped — position size rounds to zero")
                    continue

                if direction == "SHORT":
                    paper.sell(symbol, qty, price)
                else:
                    paper.buy(symbol, qty, price)
                sl_levels[symbol]    = sl
                tp_levels[symbol]    = tp
                trade_states[symbol] = "IN_TRADE"
                print(f"[{base}] ⚡ SWEEP {direction} @ ${price:,.2f}  SL=${sl:,.2f}  "
                      f"TP=${tp:,.2f}  R:R=1:{rr:.1f}  qty={qty:.6f}")

            except Exception as e:
                print(f"[{base}] Error: {e}")

        pos_str = "  ".join(
            f"{k.split('/')[0]}={'L' if v>0 else 'S'}{abs(v):.4f}"
            for k, v in paper.positions.items()
        ) or "flat"
        print(f"  [CRYPTO] Balance: ${paper.balance:,.2f}  |  {pos_str}\n")
        save_test_state(paper, sl_levels, tp_levels, live_prices, pools, trade_states)
        time.sleep(POLL_SECONDS)


# ── Stock sweep-reversal bot (lumibot + Alpaca) ───────────────────────────────

def run_stock_sweep():
    try:
        from lumibot.strategies import Strategy
        from lumibot.entities import Asset, Order
        from lumibot.brokers import Alpaca
        from lumibot.traders import Trader

        class SweepTestBot(Strategy):
            def initialize(self):
                self.sleeptime    = "1M"
                self.entry_order  = None
                self.sl_order     = None
                self.tp_order     = None
                self.stop_loss    = None
                self.take_profit  = None
                self.pool_high    = None
                self.pool_low     = None
                self.last_recalc  = None   # datetime of the last 1H pool recalculation
                self.armed        = False

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

                sl = self.stop_loss
                tp = self.take_profit
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
                        f"[{STOCK_SYMBOL}] \U0001F6D1\U0001F3AF OCO order placed — SL @ ${sl:.2f}  TP @ ${tp:.2f}",
                        color="yellow"
                    )
                except Exception as e:
                    self.log_message(f"[{STOCK_SYMBOL}] OCO order failed: {e}", color="red")

            def on_trading_iteration(self):
                asset    = Asset(STOCK_SYMBOL, asset_type=Asset.AssetType.STOCK)
                price    = self.get_last_price(STOCK_SYMBOL)
                position = self.get_position(asset)
                now      = self.get_datetime()

                if not (position and abs(position.quantity) > 0) and (
                    self.last_recalc is None or (now - self.last_recalc).total_seconds() >= POOL_RECALC_SECONDS
                ):
                    bars_1h = self.get_historical_prices(asset, POOL_LOOKBACK_1H, "1 hour")
                    if bars_1h is not None:
                        df_1h = bars_1h.pandas_df
                        if len(df_1h) >= POOL_LOOKBACK_1H:
                            self.pool_high, self.pool_low = compute_pools(df_1h, POOL_LOOKBACK_1H)
                            self.last_recalc = now
                            self.armed = True
                            self.log_message(
                                f"[{STOCK_SYMBOL}] \U0001F553 1H recalc — "
                                f"pool_high=${self.pool_high:.2f}  pool_low=${self.pool_low:.2f}"
                            )

                sl_str = f"  SL=${self.stop_loss:.2f}  TP=${self.take_profit:.2f}" if self.stop_loss else ""
                self.log_message(
                    f"[{STOCK_SYMBOL}] ${price:.2f}  pool_high={self.pool_high}  "
                    f"pool_low={self.pool_low}  armed={self.armed}{sl_str}"
                )

                # Manual SL/TP check — closes the position if the resting broker order
                # hasn't filled yet by the time we poll (keeps this in sync either way).
                if position and position.quantity > 0 and self.stop_loss and self.take_profit:
                    if price <= self.stop_loss or price >= self.take_profit:
                        label = "SL" if price <= self.stop_loss else "TP"
                        self._cancel_resting_orders()
                        self.submit_order(
                            self.create_order(asset, position.quantity, Order.OrderSide.SELL))
                        self.log_message(
                            f"[{STOCK_SYMBOL}] {'🔴' if label=='SL' else '🟢'} {label} hit @ ${price:.2f}",
                            color="red" if label == "SL" else "green")
                        self.armed = False   # retired until the next 1H recalc
                        return

                if position and abs(position.quantity) > 0:
                    return   # already in a trade — nothing else to do this tick

                if not self.armed or self.pool_high is None:
                    return

                bars_1m = self.get_historical_prices(asset, 2, "1 minute")
                if bars_1m is None:
                    return
                df_1m = bars_1m.pandas_df
                if len(df_1m) < 1:
                    return
                candle = df_1m.iloc[-1]
                direction = detect_sweep(float(candle["high"]), float(candle["low"]),
                                          float(candle["close"]), self.pool_high, self.pool_low)
                if direction is None:
                    return
                if direction == "SHORT":
                    # Stock pipeline is long-only by design — see Global Constraints scope note.
                    return

                sl, tp = compute_stop_target(direction, float(candle["high"]), float(candle["low"]),
                                              self.pool_high, self.pool_low, SL_BUFFER_PCT)
                rr = compute_rr(price, sl, tp)
                if rr < MIN_RR:
                    self.log_message(f"[{STOCK_SYMBOL}] ⏭ Sweep {direction} skipped — "
                                      f"R:R 1:{rr:.1f} < 1:{MIN_RR:.0f} min")
                    return

                qty = int(size_position(self.get_cash(), RISK_PCT, price, sl))
                if qty < 1:
                    self.log_message(f"[{STOCK_SYMBOL}] ⏭ Sweep {direction} skipped — qty rounds to zero")
                    return

                order = self.create_order(asset, qty, Order.OrderSide.BUY)
                self.entry_order = order
                self.stop_loss   = sl
                self.take_profit = tp
                self.armed       = False   # retire the pools until the next 1H recalc
                self.submit_order(order)
                self.log_message(f"[{STOCK_SYMBOL}] ⚡ SWEEP {direction} @ ${price:.2f}  "
                                  f"SL=${sl:.2f}  TP=${tp:.2f}  R:R=1:{rr:.1f}  qty={qty}", color="green")

        print(f"[STOCKS] 1H/1M Sweep-Reversal on {STOCK_SYMBOL} | Alpaca paper "
              f"(waits for NYSE open 9:30 AM ET)")
        broker   = Alpaca({"API_KEY": API_KEY, "API_SECRET": API_SECRET, "PAPER": PAPER})
        strategy = SweepTestBot(broker=broker)
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

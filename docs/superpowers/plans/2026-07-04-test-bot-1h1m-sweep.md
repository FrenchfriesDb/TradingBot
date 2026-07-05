# Test Bot 1H/1M Sweep-Reversal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the EMA 9/21 crossover in `test_bot.py` with a 1H-anchor / 1M-execution
liquidity sweep + reversal strategy, on both the stock (IWM/Alpaca) and crypto
(BTC/Coinbase) pipelines.

**Architecture:** Five pure, unit-tested helper functions (pool calc, sweep detection,
stop/target calc, R:R calc, position sizing) get called from two existing loops — the
crypto `while True` loop and the lumibot `Strategy.on_trading_iteration` — replacing
their EMA crossover logic. State machine per symbol: `WATCHING` → `IN_TRADE` →
`RETIRED` (until the next hourly pool recalculation re-arms it).

**Tech Stack:** Python, pandas, ccxt (Coinbase), lumibot/Alpaca, pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-04-test-bot-1h1m-sweep-design.md`
- 1H pool lookback: 24 candles, recomputed every `POOL_RECALC_SECONDS = 3600`.
- SL buffer: 0.05% (`SL_BUFFER_PCT = 0.0005`) past the spike wick, same formula both pipelines.
- TP: opposite pool. Minimum R:R floor: `MIN_RR = 3.0` — skip the trade below this.
- Risk per trade: `RISK_PCT = 0.01` (1% of balance/cash).
- No re-arm of a swept level after a trade fires — both pools retire until the next hourly recalc.
- No scale-out, no break-even trailing — single fixed SL/TP per trade.
- No automated test suite beyond the pure helper functions (matches spec's Testing section).
- **Scope note (not in the original spec, decided during planning):** the stock (IWM)
  pipeline stays **long-only** — SHORT sweep setups are detected but skipped on the
  stock side. This matches the *existing* `EMATestBot` behavior (it already never
  opened shorts) and avoids adding bidirectional OCO order construction to a live
  Alpaca account for what is explicitly a pipeline-sanity test bot. The crypto pipeline
  supports both directions, since `PaperTrader.sell()` already handles shorts
  symmetrically. Flag this to the user after implementation in case they want stock
  shorts added later — it's a deliberate scope cut, not an oversight.

---

### Task 1: Pure sweep-logic helper functions + unit tests

**Files:**
- Modify: `test_bot.py` (add new functions after `ohlcv_to_df`, currently ending at line 153)
- Create: `tests/test_sweep_logic.py`

**Interfaces:**
- Produces (used by Tasks 2 and 3):
  - `compute_pools(df_1h: pd.DataFrame, lookback: int) -> tuple[float, float]` — returns `(pool_high, pool_low)`
  - `detect_sweep(candle_high: float, candle_low: float, candle_close: float, pool_high: float, pool_low: float) -> str | None` — returns `"SHORT"`, `"LONG"`, or `None`
  - `compute_stop_target(direction: str, candle_high: float, candle_low: float, pool_high: float, pool_low: float, sl_buffer_pct: float) -> tuple[float, float]` — returns `(stop_loss, take_profit)`
  - `compute_rr(entry: float, sl: float, tp: float) -> float`
  - `size_position(balance: float, risk_pct: float, entry: float, sl: float) -> float`
  - Constants: `POOL_LOOKBACK_1H = 24`, `POOL_RECALC_SECONDS = 3600`, `SL_BUFFER_PCT = 0.0005`, `MIN_RR = 3.0`, `RISK_PCT = 0.01`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sweep_logic.py`:

```python
import pandas as pd
import pytest

from test_bot import (
    compute_pools, detect_sweep, compute_stop_target, compute_rr, size_position,
)


def _candles(highs, lows):
    return pd.DataFrame({"high": highs, "low": lows})


def test_compute_pools_uses_lookback_window():
    # 30 candles, but lookback=24 should ignore the oldest 6
    highs = [100] * 6 + [200] * 24
    lows  = [50]  * 6 + [150] * 24
    df = _candles(highs, lows)
    pool_high, pool_low = compute_pools(df, lookback=24)
    assert pool_high == 200
    assert pool_low == 150


def test_detect_sweep_short_on_wick_above_and_close_back_in():
    result = detect_sweep(candle_high=105, candle_low=99, candle_close=98,
                           pool_high=100, pool_low=90)
    assert result == "SHORT"


def test_detect_sweep_long_on_wick_below_and_close_back_in():
    result = detect_sweep(candle_high=95, candle_low=85, candle_close=92,
                           pool_high=100, pool_low=90)
    assert result == "LONG"


def test_detect_sweep_none_when_no_wick_past_pool():
    result = detect_sweep(candle_high=99, candle_low=91, candle_close=95,
                           pool_high=100, pool_low=90)
    assert result is None


def test_detect_sweep_none_when_wick_past_but_closes_outside():
    # Wicks above pool_high but closes above it too — a breakout, not a reversal
    result = detect_sweep(candle_high=105, candle_low=101, candle_close=103,
                           pool_high=100, pool_low=90)
    assert result is None


def test_compute_stop_target_short():
    sl, tp = compute_stop_target("SHORT", candle_high=105, candle_low=99,
                                  pool_high=100, pool_low=90, sl_buffer_pct=0.0005)
    assert sl == pytest.approx(105 * 1.0005)
    assert tp == 90


def test_compute_stop_target_long():
    sl, tp = compute_stop_target("LONG", candle_high=95, candle_low=85,
                                  pool_high=100, pool_low=90, sl_buffer_pct=0.0005)
    assert sl == pytest.approx(85 * 0.9995)
    assert tp == 100


def test_compute_rr_basic():
    rr = compute_rr(entry=100, sl=90, tp=130)
    assert rr == pytest.approx(3.0)


def test_compute_rr_zero_risk_returns_zero():
    assert compute_rr(entry=100, sl=100, tp=130) == 0.0


def test_size_position_risks_exact_percent_of_balance():
    qty = size_position(balance=10_000, risk_pct=0.01, entry=100, sl=95)
    # risk_per_unit = 5, risk_dollars = 100 -> qty = 20
    assert qty == pytest.approx(20.0)


def test_size_position_zero_when_risk_non_positive():
    assert size_position(balance=10_000, risk_pct=0.01, entry=100, sl=100) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_sweep_logic.py -v`
Expected: `ImportError` or `ModuleNotFoundError` — `compute_pools` (and the others) don't exist in `test_bot.py` yet.

- [ ] **Step 3: Add the constants and helper functions to `test_bot.py`**

Replace the existing constants block (currently lines 22-33):

```python
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
```

with:

```python
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
```

Then add these functions immediately after `ohlcv_to_df` (currently ends at line 153):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_sweep_logic.py -v`
Expected: all 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add test_bot.py tests/test_sweep_logic.py
git commit -m "Add pure sweep-reversal helper functions with unit tests"
```

---

### Task 2: Rewrite the crypto pipeline (`run_crypto_ema` → `run_crypto_sweep`)

**Files:**
- Modify: `test_bot.py` — `save_test_state` (currently lines 113-147) and `run_crypto_ema` (currently lines 156-252)

**Interfaces:**
- Consumes: `compute_pools`, `detect_sweep`, `compute_stop_target`, `compute_rr`, `size_position`, `POOL_LOOKBACK_1H`, `POOL_RECALC_SECONDS`, `SL_BUFFER_PCT`, `MIN_RR`, `RISK_PCT`, `POLL_SECONDS`, `CRYPTO_SYMBOLS`, `CRYPTO_BALANCE`, `TEST_STATE_FILE` (from Task 1)
- Produces: `run_crypto_sweep()` — entry point called from `__main__` in Task 4; `save_test_state(paper, sl_levels, tp_levels, prices, pools, trade_states)` — new signature (adds `pools` and `trade_states` params)

- [ ] **Step 1: Replace `save_test_state`**

Replace the function body (keep the same name and the first two params, add `pools` and `trade_states`):

```python
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
```

- [ ] **Step 2: Replace `run_crypto_ema` with `run_crypto_sweep`**

```python
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
                if now - last_recalc[symbol] >= POOL_RECALC_SECONDS:
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
```

- [ ] **Step 3: Verify the file still compiles and the Task 1 tests still pass**

Run: `python3 -m py_compile test_bot.py`
Expected: no output, exit code 0.

Run: `python3 -m pytest tests/test_sweep_logic.py -v`
Expected: all 11 tests still PASS (this task didn't touch the pure helpers).

- [ ] **Step 4: Commit**

```bash
git add test_bot.py
git commit -m "Replace crypto EMA loop with 1H/1M sweep-reversal state machine"
```

---

### Task 3: Rewrite the stock pipeline (`EMATestBot` → `SweepTestBot`)

**Files:**
- Modify: `test_bot.py` — the `run_stock_ema` function body (currently lines 257-380), specifically the `EMATestBot` class

**Interfaces:**
- Consumes: `compute_pools`, `detect_sweep`, `compute_stop_target`, `compute_rr`, `size_position`, `POOL_LOOKBACK_1H`, `POOL_RECALC_SECONDS`, `SL_BUFFER_PCT`, `MIN_RR`, `RISK_PCT`, `STOCK_SYMBOL` (from Task 1)
- Produces: `run_stock_sweep()` — entry point called from `__main__` in Task 4 (renamed from `run_stock_ema`)

**Note:** per the Global Constraints scope note above, this pipeline only takes LONG
sweep setups. A detected SHORT setup is logged and skipped.

- [ ] **Step 1: Rename `run_stock_ema` to `run_stock_sweep` and replace `EMATestBot`**

Inside the function (keep the `try`/`except` wrapper, the imports, and the final
`print`/`broker`/`trader` block unchanged), replace the `EMATestBot` class with:

```python
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

                if self.last_recalc is None or (now - self.last_recalc).total_seconds() >= POOL_RECALC_SECONDS:
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
```

Also update the surrounding `print`/`strategy = ...` lines in the same function to
reference `SweepTestBot` instead of `EMATestBot`, and rename the function definition
line from `def run_stock_ema():` to `def run_stock_sweep():`.

- [ ] **Step 2: Verify the file still compiles and Task 1 tests still pass**

Run: `python3 -m py_compile test_bot.py`
Expected: no output, exit code 0.

Run: `python3 -m pytest tests/test_sweep_logic.py -v`
Expected: all 11 tests still PASS.

- [ ] **Step 3: Commit**

```bash
git add test_bot.py
git commit -m "Replace stock EMATestBot with long-only sweep-reversal SweepTestBot"
```

---

### Task 4: Wire up `__main__`, update the file header, final verification

**Files:**
- Modify: `test_bot.py` — top docstring (currently lines 1-6) and the `__main__` block (currently lines 385-397)

**Interfaces:**
- Consumes: `run_crypto_sweep` (Task 2), `run_stock_sweep` (Task 3)

- [ ] **Step 1: Replace the top docstring**

Replace lines 1-6:

```python
"""
EMA Crossover Test Bot — stocks + crypto.
Stocks : IWM via Alpaca paper (NYSE hours, EMA 9/21 on 1m)
Crypto : BTC only via Kraken public (24/7, EMA 9/21 on 5m, paper)
Purpose: confirm execution works on both pipelines before trusting the SMC bot.
"""
```

with:

```python
"""
1H/1M SMC Sweep-Reversal Test Bot — stocks + crypto.
Stocks : IWM via Alpaca paper (NYSE hours, long-only)
Crypto : BTC only via Coinbase public (24/7, long + short)
Both pipelines: 1H swing high/low = liquidity pools (recalculated hourly),
1M candle wicks past a pool and closes back inside = sweep+reversal entry,
opposite pool = target, min 1:3 R:R, 1% risk per trade.
Purpose: confirm execution works on both pipelines before trusting the SMC bot.
"""
```

- [ ] **Step 2: Update the `__main__` block**

Replace:

```python
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
```

with:

```python
if __name__ == "__main__":
    print("=" * 65)
    print("1H/1M SWEEP-REVERSAL TEST BOT — STOCKS + CRYPTO")
    print(f"  Stocks : {STOCK_SYMBOL} via Alpaca paper  (long-only, fires at NYSE open)")
    print(f"  Crypto : {', '.join(s.split('/')[0] for s in CRYPTO_SYMBOLS)} via Coinbase  (24/7, long+short)")
    print(f"  Pools  : 1H, {POOL_LOOKBACK_1H}-candle lookback  |  Entries: 1M sweep+reversal")
    print(f"  Risk   : {RISK_PCT*100:.0f}% per trade  |  Min R:R: 1:{MIN_RR:.0f}")
    print("=" * 65)

    # Stock bot runs in a background thread (lumibot blocks internally)
    threading.Thread(target=run_stock_sweep, daemon=True).start()

    # Crypto bot runs in the main thread
    run_crypto_sweep()
```

- [ ] **Step 3: Full-file verification**

Run: `python3 -m py_compile test_bot.py`
Expected: no output, exit code 0.

Run: `python3 -m pytest tests/test_sweep_logic.py -v`
Expected: all 11 tests PASS.

Run: `grep -n "EMA\|FAST\|SLOW\|CRYPTO_RR\|CRYPTO_SL_PCT\|CRYPTO_RISK\|STOCK_SL_PCT\|STOCK_RR\|run_crypto_ema\|run_stock_ema\|EMATestBot" test_bot.py`
Expected: no matches (all old EMA references removed).

- [ ] **Step 4: Commit**

```bash
git add test_bot.py
git commit -m "Wire up 1H/1M sweep-reversal test bot entry point and header"
```

---

## Manual Verification (not automated — matches the spec's Testing section)

After all four tasks are committed:

1. Run `python3 test_bot.py` during a live period and watch the console for:
   - `[BTC] 🕓 1H recalc — pool_high=... pool_low=...` appearing once per hour
   - `[IWM] 🕓 1H recalc — ...` appearing once per hour during market hours
   - No exceptions/tracebacks over at least one full 1H recalculation cycle.
2. Confirm `test_state.json` after a save shows a top-level `"pools"` key with
   `pool_high`/`pool_low`/`state` per crypto symbol.
3. If a sweep fires during the observation window, confirm the console shows the R:R
   and that it's `>= 1:3`, and that `test_state.json`'s `positions` entry for that
   symbol has matching `stop_loss`/`take_profit`.

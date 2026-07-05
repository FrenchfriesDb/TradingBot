# Test Bot: 1H/1M SMC Sweep-Reversal Strategy

## Context

`test_bot.py` currently runs a pure EMA 9/21 crossover on two pipelines — IWM stock via
Alpaca paper trading, and BTC/USD crypto via Coinbase (ccxt) — with no SMC concepts at
all. Its stated purpose is to confirm both execution pipelines (order placement, SL/TP
handling, state persistence) work correctly before trusting the full SMC bot
(`binance_bot.py` / `bot/strategy.py`).

This spec replaces the EMA crossover with a simplified — but still genuinely SMC —
strategy: a multi-timeframe liquidity sweep + reversal, using the 1H chart as a
structural anchor and the 1M chart for precision entries. This is deliberately simpler
than the full bot: no AMD zones, no equal-highs/lows tracking, no FVGs, no sniper-arm
countdown, no AI confirmation gate, no scale-out/break-even trade management.

## Goals

- Replace the EMA crossover entry/exit logic on **both** pipelines (stock + crypto)
  with the same 1H/1M sweep-reversal logic — one code path, no per-asset special-casing.
- Keep the existing `PaperTrader` (crypto side) and Alpaca/lumibot integration (stock
  side) — only the signal generation and trade management logic changes.
- Preserve `test_state.json` as the state/output file, extended with the new pool
  levels so the live chart can display them.

## Non-goals

- No AMD phase detection, no FVGs, no equal-high/low liquidity tracking (that's what
  the main bot already does — this stays intentionally simpler).
- No scale-out or break-even SL trailing. Single fixed SL/TP per trade.
- No AI confirmation call.

## Strategy Logic

### 1H Anchor ("Key Liquidity Pools")

Recomputed once per hour, per symbol:

- Pull the last 24× 1H candles.
- `pool_high = max(high)` over that window.
- `pool_low  = min(low)` over that window.
- These two levels are the only structural reference points the bot uses. Recomputing
  replaces the previous pools even if they never triggered a trade.

### 1M Watchlist → Sniper Entry

Every 1M poll (per symbol), while flat and both pools are armed:

- Check the most recently **closed** 1M candle against `pool_high` / `pool_low`.
- A sweep+reversal fires when:
  - **Short setup:** candle `high > pool_high` AND candle `close` back inside (i.e.
    `close <= pool_high`).
  - **Long setup:** candle `low < pool_low` AND candle `close` back inside (i.e.
    `close >= pool_low`).
- Direction is a fade of the sweep: wick above `pool_high` → SHORT; wick below
  `pool_low` → LONG.

### Risk Parameters

- **Stop loss:** percentage buffer past the spike candle's wick — same formula for
  both IWM and BTC:
  - Short: `sl = spike_high * 1.0005`
  - Long:  `sl = spike_low  * 0.9995`
- **Take profit:** the *opposite* pool from the one that was swept (swept the low →
  target `pool_high`; swept the high → target `pool_low`).
- **R:R floor:** before entry, compute implied R:R from `entry`/`sl`/`tp`. If it's
  below **1:3**, skip the trade (log why, same style as the main bot's
  reward-too-small skip messages).
- **Position size:** risk **1%** of current balance per trade, sized off the SL
  distance (`qty = (balance * 0.01) / abs(entry - sl)`), same pattern as the existing
  `PaperTrader.buy`/`sell` sizing.

### State Machine (per symbol)

Replaces the flat EMA-loop state with two states:

- **`WATCHING`** — both pools armed, no open position. This is the state after each
  hourly recalculation and after a trade closes.
- **`IN_TRADE`** — single fixed SL/TP is being watched every 1M poll. No scale-out, no
  break-even trail. On SL or TP hit: close position, log the result, return to
  `WATCHING`.

**Re-arm rule:** once a trade fires from a swept level, **both** pools retire (no
re-arming) until the next hourly recalculation replaces them — even if the trade closes
quickly and price revisits the same level within the hour. This avoids repeated
fake-out entries chopping around one level, per the "don't overtrade the 1M chart"
concern that motivated the 1% risk cap.

## Symbols / Pipelines

Both existing symbols are kept as-is:

- **Stock:** IWM via Alpaca paper (lumibot `Strategy` subclass, replacing `EMATestBot`).
- **Crypto:** BTC/USD via Coinbase (ccxt, replacing `run_crypto_ema`).

Both pipelines run the identical sweep-reversal logic; only the data-fetching and
order-submission plumbing differs (as it already does today between the two loops).

## State File / Output

Keep `test_state.json` and `save_test_state`, with the schema extended to include,
per symbol:

- `pool_high`, `pool_low` (current armed 1H levels)
- `state` (`WATCHING` / `IN_TRADE`)

in addition to the existing `qty`, `side`, `entry_price`, `current_price`,
`stop_loss`, `take_profit`, `unrealized_pnl`, `risk_dollars`, `reward_dollars` fields
already written for open positions.

## Testing / Verification

- Manual run against live Coinbase data for BTC, confirm 1H pools compute correctly
  and reversal detection fires only on genuine wick+close-back-in candles.
- Manual run against Alpaca paper for IWM during market hours, confirm order
  submission/SL/TP still function through the existing lumibot `OCO` order path.
- No automated test suite exists for `test_bot.py` today; this spec does not add one
  (out of scope — matches the "test bot" nature of the file, which is itself the
  manual verification harness for the real bot's pipelines).

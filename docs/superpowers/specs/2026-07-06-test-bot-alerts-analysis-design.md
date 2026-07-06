# Test Bot: Trade Alerts, Richer Analysis, Multi-Symbol Crypto

## Context

`test_bot.py` was recently rewritten from a plain EMA crossover into a 1H/1M SMC
liquidity-sweep-and-reversal strategy (see
`docs/superpowers/specs/2026-07-04-test-bot-1h1m-sweep-design.md`). It currently:

- Trades a single crypto pair (`BTC/USD` via Coinbase) and a single stock
  (`IWM` via Alpaca).
- Prints a bare per-tick line: price, pool_high, pool_low, state. No sound/notification
  on trade events.
- Has no visibility into how the strategy is performing over a session (win rate,
  average win/loss) and no forward-looking read on how close price is to triggering
  a setup.

This spec adds trade alerts, more crypto coverage, and richer console output, reusing
existing patterns/helpers already in the codebase rather than inventing new ones:
`binance_bot.py`'s `alert()` function, `bot/indicators.py`'s `classify_candle()`, and
`test_bot.py`'s own `compute_stop_target`/`compute_rr` pure helpers from the prior
rewrite.

## Goals

1. Sound + desktop notification when a trade enters, hits TP, or hits SL — both pipelines.
2. Crypto pipeline covers 8 pairs (matching `binance_bot.py`'s watchlist) instead of
   just BTC. Stock pipeline stays IWM-only.
3. Richer per-tick console output: distance to each pool (%), current-candle
   classification, and a live R:R preview while armed.
4. Hourly running session-stats summary (trade count, win rate, avg win/loss),
   printed to console and persisted to `test_state.json`.

## Non-goals

- No stock multi-symbol support (explicitly out of scope per user decision).
- No changes to entry/exit logic itself (sweep detection, SL/TP formulas, R:R floor,
  risk sizing) — this spec is additive: alerts, logging, and symbol count only.
- No new sound files — reuse the same three macOS system sounds `binance_bot.py`
  already uses (`Submarine`, `Glass`, `Basso`).

## Design

### 1. Trade alerts

Add `alert(title, message, sound="Glass", speak=None)` to `test_bot.py`, copied from
`binance_bot.py:78-97`: fires `afplay` twice (detached `Popen`, no permissions needed)
and an `osascript` desktop notification, plus an optional `say` call — all wrapped in
`try/except` so a notification failure can never crash the trading loop. Gated by a
module-level `TRADE_ALERTS = True` constant (same pattern as `binance_bot.py`).

Call sites:

| Event | Sound | Pipeline |
|---|---|---|
| Sweep entry fires | `Submarine` | crypto (on paper fill) + stock (`on_filled_order`) |
| TP hit | `Glass` | crypto (SL/TP check in `run_crypto_sweep`) + stock (manual exit check) |
| SL hit | `Basso` | crypto + stock, same locations |

Each alert's message includes symbol, side, price, and P&L (for exits) — mirroring the
existing console log lines so the notification isn't missing context the terminal
already has.

### 2. Crypto symbol expansion

```python
CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD",
                   "XRP/USD", "AVAX/USD", "POL/USD", "ADA/USD"]
```

No other change — `run_crypto_sweep`'s per-symbol dicts (`pools`, `sl_levels`,
`tp_levels`, `trade_states`, `last_recalc`) already key on symbol from the prior
rewrite, so this is a one-line change plus verifying Coinbase (via ccxt) serves all 8
pairs as `/USD` spot markets.

### 3. Richer per-tick output

Extend the per-tick print (both pipelines) with:

- **Distance to each pool**, as a percentage of current price:
  `dist_high = (pool_high - price) / price * 100`,
  `dist_low = (price - pool_low) / price * 100`.
- **Candle classification** of the current 1m candle via
  `indicators.classify_candle(candle, prev_candle)` (already used by `binance_bot.py`;
  requires adding `from bot import indicators` to `test_bot.py`).
- **Live R:R preview**, shown only while `state == "WATCHING"` (armed, flat): using the
  *current* candle's high/low as a stand-in for "if this were the reversal wick" and
  current close as the hypothetical fill, call the existing
  `compute_stop_target("SHORT", ...)` / `compute_stop_target("LONG", ...)` and
  `compute_rr(...)` pure helpers from Task 1 of the prior rewrite — the exact same
  formulas a real signal would use, just evaluated speculatively every tick. Format:
  `preview: SHORT@high 1:X.X · LONG@low 1:Y.Y`.

Example enriched tick line:

```
[ETH] 14:32 UTC  $1,758.20  pool_high=$1,772.72(→0.8%)  pool_low=$1,730.04(→1.6%)
  candle=doji  state=WATCHING  preview: SHORT@high 1:2.1 · LONG@low 1:4.3
```

### 4. Running session stats

A small in-memory counter per pipeline (module-level dict for crypto keyed by symbol
plus a combined total; instance attributes for the stock `SweepTestBot`), updated
whenever a trade closes (SL or TP):

```python
{"trades": 0, "wins": 0, "losses": 0, "gross_win": 0.0, "gross_loss": 0.0}
```

Printed as a summary block once per hour, piggybacking on the existing hourly pool
recalculation (crypto: check elapsed time in the outer loop, independent of any single
symbol's recalc timer; stock: same elapsed-time check inside `on_trading_iteration`).
Format:

```
━━━ SESSION STATS (crypto) ━━━
Trades: 12   Win rate: 58.3%   Avg win: $14.20   Avg loss: $8.10
Balance: $6,026.83   Net P&L: +$1,026.83
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Also written into `test_state.json` under a new top-level `"stats"` key (crypto only —
`test_state.json` doesn't currently track the stock pipeline at all, and this spec
doesn't change that) so the numbers are available for future chart display without
another round of changes.

## Testing / Verification

- No automated tests added — this is console output, notification side effects, and a
  symbol-count change, none of which are meaningfully unit-testable (matches the
  precedent set in the prior rewrite's spec, which also left the live-loop wiring
  manually verified rather than automated).
- Manual verification: run `test_bot.py`, confirm alerts fire audibly on a paper
  trade's entry/exit, confirm all 8 crypto symbols appear in the console rotation,
  confirm the enriched tick line renders correctly for a symbol with and without an
  open position, and confirm the hourly stats block appears and updates after a trade
  closes.

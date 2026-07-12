# Google Sheets Trade Logging

## Context

`binance_bot.py` (crypto SMC, 8 coins) and `bot/strategy.py`'s `DebbieLaSMC` (stock SMC,
run via `tradingbot.py`) both trade continuously but have no external, human-readable
record of performance over time — only the current-state JSON files and scrollback
console logs. This adds automated logging to a Google Sheet the user already created,
so daily portfolio performance and individual trades are visible without reading logs.

`test_bot.py` is explicitly out of scope — it's the pipeline-sanity test bot, not one
of the two "real" strategies.

## Goals

- Once per day, each bot appends a portfolio snapshot row (balance, daily return %,
  benchmark price, benchmark daily return %) to its own "Macro" tab.
- On every completed round-trip trade, each bot appends one row (entry, exit, size,
  realized P&L, the signal that triggered entry) to its own "Ledger" tab.
- A Google Sheets outage, bad credentials, or network failure must never crash or
  pause either trading loop — logging is strictly best-effort.

## Non-goals

- `test_bot.py` does not get instrumented.
- No historical backfill of past trades — logging starts from whenever this ships.
- No second Google Sheet — both bots write to different tabs within the one sheet
  already shared.

## Design

### 1. Shared module: `sheets_logger.py`

One gspread implementation, imported by both bots, so there is exactly one place that
knows how to authenticate and write — not two copies that can drift apart.

```python
def get_sheet_client():
    """Lazy singleton: authenticates via the service-account JSON on first call,
    caches the client. Returns None (not a raised exception) on any failure —
    missing file, invalid JSON, network error, bad sheet URL. Every caller must
    treat None as "logging unavailable this run" and continue trading."""

def ensure_tabs(client, spreadsheet_url, tab_specs: dict[str, list[str]]):
    """tab_specs maps tab name -> header row, e.g. {"Crypto Macro": ["Date", "Balance", ...]}.
    Creates any tab that doesn't exist yet with its header row. Idempotent — safe to
    call every time a bot starts. Returns None on failure, same as get_sheet_client."""

def log_daily_snapshot(client, spreadsheet_url, tab_name, date_str,
                        balance, daily_return_pct, bench_price, bench_return_pct):
    """Appends one row: [Date, Balance, Daily Return %, Benchmark Price, Benchmark Return %]."""

def log_trade(client, spreadsheet_url, tab_name, timestamp_str, ticker, side,
              entry_price, exit_price, size, pnl, reason):
    """Appends one row: [Timestamp, Ticker, Side, Entry, Exit, Size, P&L, Reason]."""
```

Every function's body is wrapped in `try/except Exception`, prints
`f"[SHEETS] ... failed: {e}"` on failure, and returns without raising. This mirrors
`binance_bot.py`'s existing `alert()` — a side-effect that must never block the
trading loop it's attached to.

### 2. Config (`config.py`, `.env`-based like the existing Alpaca/NVIDIA keys)

```python
GOOGLE_SHEETS_CREDS_FILE = os.getenv("GOOGLE_SHEETS_CREDS_FILE", "google_credentials.json")
GOOGLE_SHEET_URL         = os.getenv("GOOGLE_SHEET_URL", "")
```

`google_credentials.json` (the service-account key) and any `.env` entry pointing at
it are added to `.gitignore` — this is a credential file, treated exactly like the
existing `.env` exclusion.

### 3. Tabs (auto-created if missing, via `ensure_tabs`)

| Tab | Header |
|---|---|
| `Crypto Macro` | Date, Balance, Daily Return %, BTC Price, BTC Daily Return % |
| `Crypto Ledger` | Timestamp, Ticker, Side, Entry, Exit, Size, P&L, Reason |
| `Stock Macro` | Date, Balance, Daily Return %, SPY Price, SPY Daily Return % |
| `Stock Ledger` | Timestamp, Ticker, Side, Entry, Exit, Size, P&L, Reason |

### 4. `binance_bot.py` integration

**Daily snapshot** — a new day-rollover check in `run()`'s main loop (same pattern as
`PaperTrader.check_daily_cap`'s date comparison already in this file): track
`last_sheet_log_date`; when the UTC date advances, compute `daily_return_pct` against
the balance recorded at the previous rollover (first-ever run uses the starting
balance as the baseline), fetch current BTC/USD price as the benchmark, and call
`log_daily_snapshot`.

**Trade closes** — two independent close paths both need the hook:
- `close_position()` closure inside `process_symbol` (`binance_bot.py:1091-1112`) —
  already computes `pnl`, `is_long`/`direction_label`, has `state.entry_price` and
  `price` (exit). Call `log_trade` right before `state.reset()`.
- The 10-second watcher's inline close block (`binance_bot.py:1837-1868`) — same
  data available (`pnl`, `fill`, `st.entry_price`). Call `log_trade` right before
  `st.reset()`.

To avoid duplicating the row-building logic at both call sites, add one small local
helper near these two blocks:
```python
def _log_trade_close(symbol, base, is_long, entry_price, exit_price, qty, pnl, state):
    reason = (f"{'CHASE ' if state.is_chase else ''}{state.amd_phase or 'BOS'} "
              f"{'LONG' if is_long else 'SHORT'}")
    log_trade(get_sheet_client(), GOOGLE_SHEET_URL, "Crypto Ledger",
              datetime.now(timezone.utc).isoformat(), base,
              "LONG" if is_long else "SHORT", entry_price, exit_price, qty, pnl, reason)
```
called from both close points.

### 5. `bot/strategy.py` integration

**Daily snapshot** — same day-rollover pattern, in `on_trading_iteration`, using
`self.get_last_price("SPY")` as the benchmark (SPY is already in the watchlist, no
extra API call).

**Trade closes** — two independent close paths:
- The manual SL/TP hit block (`bot/strategy.py:694-720`) — `pnl` already computed.
  Call the logging helper right before `self._reset(symbol)`, only when
  `submitted is not None` (matches the existing guard — don't log a trade that didn't
  actually close).
- The stale-timeout exit (`bot/strategy.py:736-752`) — **does not currently compute
  `pnl` at all**. Add the same P&L calc used in the SL/TP block
  (`(current_price - entry_p) * close_qty` for longs, mirrored for shorts) before
  logging, then call the same helper.

Reason field built from `self.amd_phase[symbol]` and the zone tag context already
computed earlier in `_process_symbol` (e.g. `"AMD LONG"`, `"FVG SHORT"`, or
`"Stale-exit"` for the timeout path).

## Setup prerequisite (user action required)

This cannot be automated — it requires the user's own Google account:
1. Create a Google Cloud project (or reuse an existing one).
2. Enable the Google Sheets API for it.
3. Create a service account, generate a JSON key, download it as
   `google_credentials.json` into the project root.
4. Share the target Google Sheet with the service account's email address
   (found inside the JSON key file) as an Editor.
5. Set `GOOGLE_SHEET_URL` in `.env` to the sheet's URL.

The implementation will include a short `README`-style comment block at the top of
`sheets_logger.py` restating these steps, so they're discoverable from the code
itself, not just this spec.

## Testing / Verification

- `sheets_logger.py`'s pure logic (row-building, not the actual gspread network
  calls) gets unit tests: given inputs, the row list is exactly right.
- The gspread calls themselves are not mocked/tested — matches this codebase's
  existing precedent (no tests exist for Alpaca/Coinbase API calls either); verified
  manually by checking the actual sheet after a real run.
- Manual verification: run each bot with valid credentials configured, confirm tabs
  get created with correct headers, confirm a daily snapshot row appears, confirm a
  trade close (can be forced/simulated in paper trading) produces a ledger row with
  correct entry/exit/P&L math.
- Manual verification with **no** credentials configured (or an invalid
  `GOOGLE_SHEET_URL`): confirm both bots run normally and keep trading, with only a
  one-time `[SHEETS] ... failed` warning printed, never a crash.

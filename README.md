# DEBBIE-LA INSTITUTIONAL SMC BOT

A Python algorithmic trading bot implementing the Debbie-La institutional smart money strategy. Trades stocks via Alpaca and crypto via Binance/CCXT simultaneously.

---

## Quick Start (Fast Startup — Always Use This)

The bot runs best under **miniforge conda** (Apple-notarized packages → starts in under 15 seconds every time). Using `.venv311` triggers macOS Gatekeeper OCSP scans that take 15–30 minutes after every reboot.

### First-time setup

```bash
# Install miniforge (one-time)
curl -LO https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-MacOSX-arm64.sh
bash Miniforge3-MacOSX-arm64.sh

# Create trading environment
conda create -n trading python=3.11 -y
conda activate trading
pip install -r requirements.txt
```

### Shell aliases (add to ~/.zshrc)

```bash
alias runbot='~/miniforge3/envs/trading/bin/python3 /Users/usahealthlife/Desktop/TradingBot/binance_bot.py'
alias runchart='~/miniforge3/envs/trading/bin/python3 /Users/usahealthlife/Desktop/TradingBot/chart_server.py'
alias tradepy='~/miniforge3/envs/trading/bin/python3'
```

Then reload: `source ~/.zshrc`

### Run

**Use the scripts in `scripts/` — do not launch bots bare.** See [Running the bots](#running-the-bots-use-the-scripts) below for why this matters.

```bash
# Start (detached, survives terminal close + sleep, writes logs/)
scripts/start_binance_bot.sh        # Crypto SMC bot (24/7)
scripts/start_stock_bot.sh          # Stock SMC bot (NYSE hours)
scripts/start_test_bot.sh           # Test bot (crypto-only sweep pipeline)

# Watch one live in your window (Ctrl-C stops WATCHING, not the bot)
scripts/watch_binance_bot.sh
scripts/watch_stock_bot.sh
scripts/watch_test_bot.sh

# Run one IN your window (Ctrl-C DOES stop the bot; still writes logs/)
scripts/run_binance_bot.sh
scripts/run_stock_bot.sh

# Stop
scripts/stop_binance_bot.sh  |  scripts/stop_stock_bot.sh  |  scripts/stop_test_bot.sh

# Live monitoring
runchart                            # Quant Desk + live chart at http://localhost:8888
tradepy monitor.py                  # Real-time P&L monitor

# Backtesting (see the Backtesting section — read its caveats first)
tradepy backtest_stocks.py --start 2026-05-01 --end 2026-06-01   # stocks: drives the REAL class
tradepy backtest_crypto.py --days 30                             # crypto: ⛔ currently under-reports badly

# Alternative
tradepy tradingbot.py crypto        # Crypto via Alpaca (alternative to binance_bot)
```

### Running the bots (use the scripts)

The scripts exist because launching bare (`caffeinate -i python3 binance_bot.py`) has
repeatedly caused real damage:

- **stdout goes to the terminal, not `logs/`.** The log then silently freezes while the
  bot keeps trading. This has happened 5+ times, once leaving 4 days with no record —
  when a trade later looked wrong, its reasoning had to be rebuilt from raw candles.
  Check with `lsof -p <pid> | grep 1[uw]`: it must show `logs/*.log`, not `/dev/ttysNNN`.
- **A bare `caffeinate -i` doesn't survive lid-close** (`-i -s`, which the scripts use,
  does). A sleeping laptop is a stopped bot.
- **A stopped bot misses everything.** On 2026-08-19 the bot was down 3 days and missed
  BTC's +5.75% 6h displacement entirely. Downtime also skips daily Macro snapshot rows
  (now auto-backfilled, but the trades themselves are simply gone).

`start_*.sh` wraps `nohup caffeinate -i -s … > logs/X.log 2>&1` with `PYTHONUNBUFFERED=1`
(without it Python block-buffers into a pipe and the log looks dead), and reads the bot's
own lock file to report the real PID.

**For detailed strategy explanation, see [DESIGN.md](DESIGN.md) — covers SMC psychology, entry/exit mechanics, risk management, and all the edge-case guards.**

### .env file (never commit this)

```bash
# Alpaca — stocks + crypto (tradingbot.py)
ALPACA_API_KEY=your_key_here
ALPACA_API_SECRET=your_secret_here
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_PAPER=True

# Binance — live trading (binance_bot.py uses Binance public data if left empty)
BINANCE_API_KEY=
BINANCE_SECRET=
BINANCE_TESTNET=True

# AI confirmation (optional — bot proceeds on technicals if not set)
OPENAI_API_KEY=
```

---

## What the Bot Does

Implements the 4-step Debbie-La institutional setup on every symbol:

1. **HTF bias** — 4H BOS determines macro direction (BULLISH / BEARISH / None)
2. **Liquidity sweep** — 5m wick hunts retail stops below EQL or above EQH
3. **FVG lock** — imbalance zone left by institutional displacement
4. **Sniper entry** — 10-second precision loop fires the moment price re-enters the zone

State machine per symbol: `IDLE → SWEEP_HUNT → ENTRY_WAIT → POSITION_OPEN`

---

## Bots

### Crypto Bot — `runbot` / `binance_bot.py`

The primary bot. Runs 24/7 on Binance data, in-memory paper trading.

- **Watchlist (13 as of 2026-08-20):**
  `BTC/USD, ETH/USD, SOL/USD, XRP/USD, AVAX/USD, DOGE/USD, POL/USD, ADA/USD,`
  `HYPE/USD, INJ/USD, SEI/USD, DRIFT/USD, ASTER/USD`
  The five added on 2026-08-20 were each verified on Coinbase for **every** timeframe
  `_process_symbol` pulls (5m/15m/1h/6h/1d) at full depth before being added. Edit
  `DEFAULT_SYMBOLS` in `binance_bot.py`, or override with `BINANCE_SYMBOL` in `.env`.
  **The list is duplicated in `chart_server.py` (`PAIRS` + `ALL_SYMS.smc`) — update both,
  nothing enforces agreement.** `test_bot.py` keeps its own separate 8-symbol list.
- **Data:** Coinbase public API via CCXT (no account needed)
- **Paper balance:** $10,000 start | **Leverage:** 10× simulated
- **Candles:** 5m (LTF) + 4H (HTF) — **but Bybit (the 4H source) returns 403 from a US IP.**
  `connect_htf_exchange()` catches it and falls back to **6H on Coinbase for every symbol**,
  so "4H" in the logs is really 6H. Nothing depends on Bybit returning.
- **Risk per trade:** 2% of balance (daily cap — resets midnight UTC)
- **Trading costs:** fees ARE charged (see *Trading Costs* below). Every number the bot
  reports is net.
- **State file:** `crypto_state.json` (written every 5 min, read by chart server + monitor)

**Trade management (auto, no intervention needed):**

| Trigger | Action |
| --- | --- |
| 50% of way to TP | Scale out 50% of position — banks profit, lets rest run |
| 80% of way to TP | Trail SL to **entry + 0.5R profit lock** — a reversal still keeps half an R |
| SL or TP crossed | Closes remaining position, resets state |
| 6h in position | Stale exit — force closes regardless of P&L |

Targets are armed at a **1:2 minimum R:R** (`MIN_AI_RR = 2.0`). The earlier 3R floor + 60% break-even trail produced inverted realized R:R — winners clipped to ~+$1 scratches while losers took the full stop. The ledger row P&L includes the scale-out leg, so the sheet shows each trade's true total.

**Entry precision:**

- 5-min cycle detects setup → **arms sniper** with pre-calculated SL/TP/qty
- 10-second loop fires entry the instant price touches the zone boundary
- Fills at exact zone price (not the 5-min candle close) — true limit-order precision

**Reliability features:**

- **Startup catch-up** — on restart, immediately checks if SL/TP was hit while offline
- **Orphan guard** — if paper position exists but state machine lost sync, auto-restores POSITION_OPEN
- **AI timeout** — Gemini/OpenAI call has a 25s timeout with non-blocking executor; trades proceed on technicals if AI hangs
- **Background library loading** — conda libraries load fast; shows progress counter if using pip

---

### Stock Bot — `tradepy tradingbot.py live`

- **Watchlist:** AAPL, QQQ, SPY, NVDA, TSLA, GOOGL, META, MSFT
- **Broker:** Alpaca paper trading (real orders, real fills)
- **Risk:** 2% total ÷ symbols = ~0.25% per trade
- **R:R:** AI-determined (min 1:2, typically 1:3–1:5)
- **Hours:** NYSE 9:30 AM–4:00 PM ET, auto-closes all at 3:45 PM
- **Interval:** Scans every 15 minutes
- **Orders:** Bracket orders (entry + SL + TP in one shot) → SL/TP lines visible in TradingView
- **Fallback:** If bracket rejected → market entry + OCO order attached after fill
- **State file:** `strategy_state.json` (persists across restarts)

---

### Crypto via Alpaca — `tradepy tradingbot.py crypto`

Same SMC logic as the stock bot but on Alpaca crypto (BTC, ETH, SOL, LINK, LTC, BCH). 24/7, same Alpaca paper account.

---

## Backtesting

Two backtesters, with genuinely different fidelity. **Read the limitations — a backtest
you trust more than it deserves is worse than none.**

### Crypto — `tradepy backtest_crypto.py`

```bash
tradepy backtest_crypto.py                             # 30d, all 13 symbols
tradepy backtest_crypto.py --days 60 --symbols BTC/USD,ETH/USD
tradepy backtest_crypto.py --days 14 --verbose         # print every trade
```

Replays historical 5m/6h candles through **the same pure functions `binance_bot.py` uses
live** (`bot/indicators.py`), and imports the live constants (`MAX_RISK_DOLLARS`,
`MIN_AI_RR`, `STALE_ZONE_BARS`, `TAKER_FEE_RATE`, …) so the test can't silently drift from
production. Charges the same round-trip fee the live `PaperTrader` charges and reports
fees paid alongside the gross number.

> ## ⛔ THIS BACKTESTER IS CURRENTLY NOT A VALID MODEL OF THE LIVE BOT
>
> Measured 2026-08-22 over the **same 30 days**: the backtester produced **2 trades**; the
> live bot took **93**. A 46× gap. It hand-mirrors only the `IDLE → BOS+FVG → tap` path;
> the live bot *also* enters via the AMD priority engine, the 10-second sniper watcher,
> and chase/continuation entries — **none of which exist in the mirror**.
>
> A near-zero-trade result from this tool is a **false negative**, not evidence the
> strategy is selective. Do not conclude anything about crypto entry logic from it until
> the missing engines are ported. Contrast `backtest_stocks.py`, which drives the real
> strategy class and therefore cannot drift.

**Also does NOT reproduce:** the AI confirmation gate (`get_ai_confirmation()` calls a live
LLM), news context, slippage/spread/partial fills, or intrabar order (same-bar stop AND
target ⇒ stop assumed first, pessimistically).

### Stocks — `tradepy backtest_stocks.py` ← use this one

```bash
tradepy backtest_stocks.py --days 60                                    # rolling window
tradepy backtest_stocks.py --start 2026-05-01 --end 2026-06-01          # explicit window
tradepy backtest_stocks.py --start 2026-05-01 --end 2026-06-01 \
        --symbols SPY,QQQ,AAPL,NVDA,TSLA,GOOGL --funnel --fee-pct 0.02
```

Runs the **real `DebbieLaSMC` class** against Alpaca **minute** bars via lumibot's
`AlpacaBacktesting`. No reimplementation, so drift is impossible by construction.

| Flag | Meaning |
|---|---|
| `--warm-up N` | Trading days of history loaded **before** the window. **Default 90 — do not lower below 65.** |
| `--funnel` | Tally state-machine transitions to show *where* setups died |
| `--fee-pct P` | Per-side cost as a percent (`0.02` = 2bps). Default 0 |
| `--ai` | Let the live Llama gate vote (slow, non-reproducible, look-ahead contaminated) |

**Three traps this tool exists to avoid:**

1. **Warm-up silently kills every trade.** `get_daily_trend()` needs 60 daily bars for its
   EMA20/50. With too few it returns `None`, the trend filter treats that as "no confirmed
   trend" and vetoes **everything** — reporting a clean zero-trade result that looks like
   selectivity. Measured: warm-up 10 → 2,814 iterations across 6 symbols, **zero** state
   transitions. Warm-up 90 on the identical window → 14 trades immediately.
2. **Never trust the Sheets-logging hook to count trades.** The old capture undercounted by
   **64%** (39 positions closed, 14 logged) because `_log_broker_side_close` asks the *live*
   Alpaca API for the real fill, which does not exist in a backtest, and its fail-soft
   `except` swallowed the trade. On a biased subsample May read −$47.85; capturing the
   simulated **fills** and pairing them FIFO gave **+$61.69** — same window, opposite sign.
3. **Alpaca blocks recent SIP data** on this plan (`subscription does not permit querying
   recent SIP data`). Keep the window out of the last ~15 minutes; a window ending "now"
   fails on the benchmark fetch at the very end of the run.

`tradepy tradingbot.py backtest` (the old `YahooDataBacktesting` path) still exists but
serves **daily** bars only — a 15m/4H strategy cannot be exercised on daily candles, so it
has never actually tested this strategy. Prefer `backtest_stocks.py`.

### Reading the results honestly

- **A backtest that reports zero/near-zero trades is a claim that needs checking, not a
  result.** Both real cases so far were tooling bugs (crypto: missing engines; stocks:
  warm-up), not selectivity.
- **Sample sizes here are noise.** The stock backtest gave 19 trades / +$61.69 with no
  fees and 18 trades / +$134.36 with 2bps — *one different trade* swung it 2×. Judge the
  sign and shape, never the dollars.
- **Historical live numbers overstate the current config.** 33 of the first 73 live trades
  (45%) came from the `wedge_breakout` and `trend_follow` engines, now gated off
  (`ENABLE_WEDGE_BREAKOUT` / `ENABLE_TREND_FOLLOW = False`).
- **Backtest 44.4% win vs live 18.2% (stocks, 2026-08-22)** — same entry logic. That gap is
  execution (stops blown through, multi-day holds), not entries.

---

### Test Bot — `tradepy test_bot.py`

**1H/1M sweep-reversal** bot for verifying execution infrastructure with fast, frequent entries. 1H swing high/low = liquidity pools (recalculated hourly); a 1M candle that wicks past a pool and closes back inside = sweep+reversal entry; opposite pool = target, min 1:3 R:R, 1% risk per trade.

**Stocks** (background thread) — IWM via Alpaca paper, long-only, NYSE hours. Kept off the SMC bot's watchlist on purpose so both bots never trade the same ticker.

**Crypto** (main thread) — BTC, ETH, SOL, DOGE, XRP, AVAX, POL, ADA via Coinbase public data, 24/7, longs + shorts, $5,000 in-memory paper balance, **no leverage**. Sizing: 1% risk per trade, min R:R 1:2.5, **each trade deploys at most 2% of the account (~$100)** and **at most 10% (~$500) is deployed per UTC day total across all trades** (~5 trades/day before the daily budget is spent). The daily counter persists to `test_state.json`, so restarts can't reset it.

```bash
tradepy test_bot.py                 # both pipelines
tradepy test_bot.py --crypto-only   # skip the stock thread
```

Every closed test trade logs to its own **Test Ledger** tab in the Google Sheet (auto-created, same 15 columns and clickable charts as the main bots — crypto gets the dashboard-style render, stocks get a real TradingView screenshot; Leverage column reads 1 since the test bot is unleveraged). Test trades never touch the real Crypto/Stock Ledger tabs.

---

## Maker vs taker fees (added 2026-09-12)

Fee drag is ~29% of the risk budget at a 1.73% stop, and it is the **only lever not caught
in the stop-width / R:R / hold-time triangle** — raising R:R forces the stop tighter, which
makes drag *worse*.

| Env var | Default | Meaning |
|---|---|---|
| `TAKER_FEE_RATE` | `0.0025` | Crossing the book |
| `MAKER_FEE_RATE` | = taker | Resting on it — **set from your real fee tier** |
| `MAKER_ENTRIES` | `0` (off) | Rest the sniper's entry instead of crossing |

**Defaults are deliberately inert.** Maker equals taker and resting is off, so nothing
changes until the tier is verified. Assuming a discount the account has not been shown to
get is the same optimism that had P&L reported gross.

### Which legs can actually earn it

```
entry        resting limit at the zone edge   -> MAKER
take-profit  resting limit above/below        -> MAKER
stop-loss    triggers and CROSSES the book    -> TAKER, always
```

A winner pays maker+maker; a loser pays maker+**taker**. So at a 1.73% stop:

| Outcome | Fee as % of risk |
|---|---:|
| today (taker/taker) | 28.9% |
| win (maker/maker) | 6.9% |
| loss (maker/taker) | 17.9% |
| **blended @35% win rate** | **~14%** |

Roughly half — **not** the ~7% that assuming maker on both legs suggests.
`round_trip_fee()` therefore takes a per-leg `exit_fee_rate`, and `PaperTrader` tracks
`maker_fees` / `taker_fees` separately so you can see whether the resting orders land.

### The fill model changes with the fee — on purpose

Charging the maker rate while still filling at whatever price printed would describe a cost
nobody paid. `maker_limit_fill()` models a genuine resting order:

- It fills **at its limit**, not at the bar's extreme.
- It fills **only if price traded through** — a mere touch does not fill, because a resting
  order at a level price only kisses sits behind the queue already posted there.
  Under-filling is the correct direction for a simulation to err.

**This is not a free win.** A resting buy at `fvg_high` fills at `fvg_high` even though
price went on to `fvg_low` — a *worse* price than market-buying after the drop. You pay for
the maker rate in fill quality, and some taps stop filling at all. That trade-off is the
reason the flag exists rather than the behaviour just being switched on.

---

## Risk sizing is percent-of-equity (changed 2026-09-12)

`MAX_RISK_DOLLARS = 20.0` was calibrated against a $10,000 paper balance, where it means
**0.2% per trade**. Pointed at a real $100 account the same constant means **20% per trade
— five losses from zero.** A fixed dollar amount is a different bet at every account size.

| Env var | Default | Meaning |
|---|---|---|
| `RISK_PCT` | `0.002` | Fraction of **equity** risked per trade (0.2%) |
| `RISK_FLOOR` | `0.0` | Never size below this many dollars |
| `RISK_CEILING` | off | Circuit breaker — caps the budget if an equity read goes wrong |

`0.002` reproduces today's $20 on today's equity: this is a change of **mechanism**, not of
risk appetite. It buys two things a dollar figure cannot — it **compounds** as the account
grows, and it **de-risks automatically in a drawdown**.

Computed **once per cycle** from `paper.equity(prices)` and stored on `paper.risk_budget`,
which all three sizing sites read. Equity, not balance: `balance` is free cash only and
understates the account whenever a position is open.

**Leverage is deliberately absent from the calculation.** Risk is `(entry − stop) × qty`;
leverage only changes the margin posted. That is precisely what lets the same constants
work on 1× spot and 10× perps, so `PAPER_LEVERAGE` stays free to change.

## Venue minimums (added 2026-09-12)

`exchange_minimums()` reads ccxt's `market['limits']`; `meets_exchange_minimums()` refuses
an order below them. It **refuses rather than rounds up** — rounding up silently breaks the
risk cap that produced the number, and a $20-risk order inflated to $50 is not the trade
that was approved. On a small account this bites: 0.2% of $100 is $0.20 of risk, which on a
2% stop is a ~$10 position, under the minimum notional on many pairs.

## Stops now survive the bot dying (added 2026-09-12)

binance_bot paper-trades, so **its stop exists only inside the process** — while the bot is
down there is no resting order anywhere. The startup catch-up compared only the *current*
price against the levels:

```python
_cur   = float(_t["last"])
sl_hit = (is_long and _cur <= st.stop_loss) or ...
```

So a stop blown through at 03:00 and recovered from by 08:00 was **invisible**, and the
position carried on reporting a result no broker would have produced.

`last_updated` is already persisted, so the downtime window is knowable. The catch-up now
replays that gap's 1-minute candles via `first_protective_breach()`. Wicks count — a
resting order fills the instant price *touches* a level. Two rules matter:

- **Chronology decides.** A trade stopped out at 03:00 cannot be rescued by its target
  printing at 05:00.
- **Both levels in one candle resolves to STOP.** The order within a bar is unknowable, and
  a simulation must not hand itself the better of two outcomes it cannot distinguish.

If the replay fetch fails it says so loudly and falls back to the current-price check,
naming the exact exposure rather than failing silently.

---

## Entry-Zone Invariant (added 2026-09-05)

A retest entry must fill **inside the zone it armed**. `price_in_entry_zone` checks this at
*tap* time, but `price` is never re-read afterwards and the AI confirmation call in between
can take **25 seconds**. `entry_respects_zone` re-checks at *execution*, in
`execute_confirmed_entry` — the one function every entry path funnels through.

**The incident.** ASTER/USD LONG, 2026-09-05 18:28 UTC:

```
armed demand zone   0.7190 .. 0.7588   (AMD manipulation_down, bullish_breaker)
OTE band            0.7812 .. 0.7863   <- entry was above this too
FILLED AT           0.7906             <- 4.19% ABOVE the top of its own zone
```

Price never traded down to the zone at all. **Which path produced that fill is still
unknown** — the bot was hand-started, so its reasoning went to `/dev/ttys003` and nothing
recorded it. The guard holds regardless of cause and names the offending path next time.

Chase entries are exempt — entering outside the zone is their entire premise, and
`state.is_chase` already marks them. Fails **open** on a missing zone, so engines that
legitimately carry none aren't silently switched off. Tolerance is 0.5% (looser than the
tap check's 0.15%): a few ticks of drift is ordinary, 4% is a defect.

Two related traps worth knowing:

- **`find_demand_zone` accepts zones up to `max_distance_pct=0.12` — 12% below price.** The
  AMD engine will happily arm a zone price may never reach, then sit on it for
  `AMD_ENTRY_WAIT_BARS = 96` (8 hours).
- **`state.sweep_low` holds the swept *high*** on the manipulation_down path
  (`state.sweep_low = sweep_high_wick_htf`). Not a functional bug, but it reads as
  nonsense in a state dump and has already cost debugging time.

## The chase engine is now gated (2026-09-05)

`ENABLE_TREND_FOLLOW` and `ENABLE_WEDGE_BREAKOUT` are off in "pure-sniper mode". **The chase
had no such flag** — it was the one continuation engine that always ran. Of the 26 trades in
the 2026-08/09 log, **all 13 whose armed zone could be recovered were chases**, each filling
5-8% above its zone (BTC $75,551 against a $69,865-71,065 zone). It was the dominant entry
path, not an edge case.

It is now behind `ENABLE_CHASE` (default **off**; set `ENABLE_CHASE=1` to restore).

Its momentum test was also the weakest check in the codebase:

```python
momentum_intact = close[-1] > close[-3]      # two bars of net drift
```

A run of *shrinking* green candles satisfies that perfectly — the exact pattern flagged on
ASTER ("the green candles kept getting smaller"). `indicators.momentum_expanding()` now
requires every bar to push the trade's way, each body within 5% of the previous or larger,
and the last body to clear the displacement floor. The 5% `decay_tol` is load-bearing:
`1.15-1.10` computes *smaller* than `1.10-1.05` in binary floating point, so a strict
non-decreasing test rejects a steady move on arithmetic dust alone.

It fails **closed** on short or malformed input — the chase enters at market with no zone
beneath it, so an unverifiable read must not become a free pass.

---

## Bots tee their own logs (added 2026-09-05)

`scripts/start_*.sh` redirects stdout into `logs/`, so a supervised bot is recorded. A bot
started **by hand in a terminal** was not — and that has blocked three live diagnoses:
ASTER 2026-09-01, POL 2026-09-04, and the ASTER zone violation above. Each time the answer
existed in scrollback for a few minutes and then was gone.

`bot/tee_logging.py` makes the bot tee its own stdout, so the record no longer depends on
how it was invoked. It is a **no-op under `start_*.sh`** (stdout is already a file there;
teeing would double every line), appends rather than truncates, flushes every line, and
never lets a full disk take down a trading loop.

---

## Setup Quality Gates (added 2026-09-04 — cuts arming events ~73%)

Three gates that stop a flat tape from manufacturing a setup out of noise. All are
env-overridable, and all live in `bot/indicators.py` so both bots share one definition.

| Constant | Default | Env var | What it rejects |
|---|---|---|---|
| `DISPLACEMENT_ATR_MULT` | `1.8` | `DISPLACEMENT_ATR_MULT` | A displacement body under 1.8× ATR |
| `DISPLACEMENT_MIN_PCT` | `0.0015` | `DISPLACEMENT_MIN_PCT` | …or under 0.15% of price, whichever is larger |
| `MIN_FVG_PCT` | `0.0015` | `MIN_FVG_PCT` | A gap thinner than 0.15% of price |
| `MAX_TARGET_ATR_MULT` | `1.5` | `MAX_TARGET_ATR_MULT` | A target beyond 1.5× the HTF ATR |

**Why `1.8` and not the old `0.6`.** A multiple below 1.0 means "smaller than an *average*
candle" — so an utterly ordinary bar cleared the momentum test. POL/USD armed a long on
2026-09-04 with a bar measuring **1.07× ATR**, invisible on the chart because there was
nothing to see. Displacement has to mean **outlier**. The percentage term is the backstop
for a flat tape: POL's 5m ATR was 0.169% of price while its 6H ATR (2.755%) sailed through
`ATR_GATE` — the tradeability gate and the momentum gate were reading different
timeframes, and that mismatch is what this closes.

**Minimum gap width.** Nothing previously constrained it. POL armed on a gap of `0.00001`
— **0.011% of price**, one tick. A gap that thin is a *line*, not a zone: price grazes it
on any tick and the "retest" carries no information.

**Target reachability.** `structural_take_profit` pins TP to a real HTF pool, but says
nothing about whether that pool can be *reached* before `STALE_TRADE_HOURS` force-closes
the position. POL was handed a target **13.3% away against a 2.755% 6H ATR** — roughly 4.8
average HTF candles of travel inside the span of one. It reported a gorgeous **1:9.4** and
could only ever exit on the timer, which is exactly what the POL trade before it did on the
identical `0.10721` target, for −$12.08. The gate *clamps* rather than vetoes (the setup may
be fine; the target was fantasy) and only stands aside if the honest target can no longer
pay `MIN_AI_RR`. On that trade: **1:9.4 → 1:3.1**.

Measured over 25h across 8 symbols: **424 → 113 arming events (27% kept)**. The cut lands
where it should — BTC/ETH/XRP fall modestly (18–32 → 9) while thin, noisy names are gutted
(POL 81 → 12, SEI 66 → 19). Pinned in `tests/test_dead_market_gates.py`.

> Changing these changes trading behaviour. They load at **startup** — a running bot keeps
> the old values until `scripts/stop_binance_bot.sh && scripts/start_binance_bot.sh`.

---

## Ledger P&L is NET of fees (fixed 2026-09-04)

All three close paths computed `pnl = (exit - entry) * qty` — the raw price difference.
Fees *were* charged (`PaperTrader._charge_fee`, all four legs) but were never subtracted
from the number handed to the ledger, the journal tiles, or the desktop alert. The sheet
reported **gross** while the balance moved by **net**, so every row overstated the trade by
a full round trip.

At 10× leverage this inverts outcomes. Four consecutive real rows:

| Trade | Notional | Ledger P&L | Fees | Actual |
|---|---:|---:|---:|---:|
| POL LONG | $1,359.94 | **+$3.16** | $6.81 | **−$3.65** |
| DRIFT SHORT | $1,480.11 | −$11.09 | $7.40 | −$18.49 |
| ETH LONG | $929.71 | **+$1.84** | $4.65 | **−$2.81** |
| XRP LONG | $727.38 | −$1.56 | $3.64 | −$5.20 |
| **Total** | $4,497.14 | **−$7.65** | $22.49 | **−$30.14** |

Two of the four were green **and losing**. The set understated the loss **4×**.

`bot/indicators.round_trip_fee()` now prices both legs at their own fill, and a **`Fees ($)`**
column was **appended last** — not placed beside P&L where it reads better — because ~177
historical rows already have their chart in column O, and inserting ahead of it would shift
every new row's chart one column right of every old one.

> **Rows written before 2026-09-04 are GROSS** and overstate by roughly the round trip.
> Don't compare them directly with rows after it. Pinned in `tests/test_fee_netted_pnl.py`.

Still not netted: the scale-out leg's own fee would belong with `banked_pnl`. That path is
currently inert (`manage_open_trade` deliberately does not scale out), so `pnl_total ==
pnl` today — but it becomes a leak the moment scale-outs are re-enabled.

### Exit Reason (added 2026-09-04)

The existing **Reason** column records the strategy that *opened* a trade (`BOS LONG`);
nothing recorded why it *closed*. **Exit Reason** does, as one of five groupable tokens:

`TARGET` · `STOP` · `BREAKEVEN` · `STALE` · `OTHER`

**Why BREAKEVEN is a separate token.** There is no break-even exit path. `manage_open_trade`
trails `stop_loss` up at +1.25R and the position then closes through the ordinary stop
branch labelled `"SL hit"` — byte-identical to a real stop-out. Only `state.breakeven_moved`
separates them, and folding them together hides the most addressable line item in the book:

| Exit | Net (121 trades) | |
|---|---:|---|
| TARGET | +$609.57 | the thesis working |
| STALE | +$184.15 | the 6h timer earning its keep |
| BREAKEVEN | −$228.35 | **costs paid, no move captured** |
| STOP | −$580.53 | the cost of being wrong |

Two derivations, because the bots learn about exits differently:

- **Crypto** — `normalize_exit_reason(label, breakeven_moved)` reads the bot's own label.
  Order matters: a winner that trailed then ran to target is `TARGET`, and one that trailed
  then timed out is `STALE` — the flag is consulted only in the stop branch.
- **Stock** — exits fire at the *broker* (bracket OCO legs) and are found by reconciliation,
  so there is no label. `infer_exit_reason(fill, sl, tp)` reads whichever leg the fill landed
  on, and returns `OTHER` when it landed near neither rather than guessing. This strategy has
  no break-even trail, so that bucket cannot occur here. The stale exit passes `"STALE"`
  explicitly, since its mid-range fill would correctly refuse inference.

`ensure_tabs` now also **backfills headings** on tabs that already exist — it previously only
created missing ones, so an appended column would have written data under a blank heading.
Fill-only: it never rewrites a label already present, in case one was renamed by hand.

---

## Trading Costs (added 2026-08-22 — changes every number)

`PaperTrader` charged **no fees at all** until 2026-08-22. Every P&L in the ledger, the
dashboard, and both backtests before that date is **gross**.

**What the audit of 121 real crypto trades found:**

```
$148,063 notional traded  ->  $725 gross profit  =  0.49% return on notional
break-even fee rate       =  0.245% PER SIDE
```

Typical taker fees are 0.25–0.60%/side. The strategy was sitting **exactly on its fee
line** while the dashboard showed it clearly green. At 0.25%/side the same 121 trades net
roughly **break-even to slightly negative**, not +$725.

| Setting | Default | Env var | Meaning |
|---|---|---|---|
| `TAKER_FEE_RATE` | `0.0025` | `TAKER_FEE_RATE` | Per-side fee, charged on **notional** |
| `ROUND_TRIP_COST` | derived | — | `TAKER_FEE_RATE × 2` |
| `MIN_FEE_CLEARANCE` | `3.0` | `MIN_FEE_CLEARANCE` | Target must clear the round trip by this multiple |

**Three changes, all in `binance_bot.py`:**

1. **Fees charged on all four legs** (open/close long, open/close short), on notional —
   at 10× leverage an $80 margin position controls $800 and the fee is on the $800.
   `PaperTrader.total_fees` accumulates them.
2. **`fee_buffer` now derived from real cost.** It was hardcoded `entry * 0.001` (0.1%) —
   under a quarter of a real 0.5% round trip, so every "break-even" exit was a guaranteed
   net **loss**. This was the single biggest leak: **34 break-even exits, −$1.15 gross but
   −$228 once fees applied**, churning $45,439 of notional for zero gross profit.
   Now `entry * ROUND_TRIP_COST * 1.5`, so break-even is a genuine tiny win.
3. **Fee-clearance gate before entry.** R:R says nothing about fees — a 1:2 setup whose
   target sits 0.3% away cannot survive a 0.5% round trip. Setups whose target is closer
   than `ROUND_TRIP_COST × MIN_FEE_CLEARANCE` (default **1.50%**) are skipped and logged.

**Measured impact of the gate** on the 122 historical trades — it blocks **11%** of them:

| Clearance | Threshold | Kept | Cut | Kept P&L (net) | Cut P&L (net) |
|---|---|---|---|---|---|
| none | — | 122 | 0 | −$40.24 | — |
| **3× (shipped)** | **1.50%** | **108** | **14** | **+$11.99** | −$52.24 |
| 4× | 2.00% | 95 | 27 | +$138.49 | −$178.73 |
| 5× | 2.50% | 82 | 40 | +$216.60 | −$256.84 |

Higher multiples score better on this sample, but **that is 122 trades — tuning to the
best historical number is curve-fitting.** 3× has a principled basis (clear the round trip
3×). It is env-tunable if you want to forward-test 4×.

Set your real tier in `.env` — Coinbase Advanced taker runs ~0.60% at low volume:

```bash
TAKER_FEE_RATE=0.004      # 0.40%/side
MIN_FEE_CLEARANCE=3.0
```

---

## Live Chart — `runchart`

Opens at **<http://localhost:8888>**

- Real-time candlestick chart (5m candles from Coinbase)
- Shows open position box from exact entry candle to current bar + 40 projected candles
- Entry / SL / TP price lines with labels and % distance
- EQL / EQH levels, trendline overlay
- Scale-out and break-even updates reflected live (chart refreshes every 5s)
- Supports all bots: `binance_bot (SMC)`, `tradingbot (SMC/Alpaca)`, `test_bot`

**Keep both running simultaneously:**

```bash
# Terminal 1
runbot

# Terminal 2
runchart
# → open http://localhost:8888 in browser
```

---

## Google Sheets Trade Ledger

Every trade is automatically logged to a Google Sheet with real-time chart screenshots.

### Setup (One Time)

```bash
# Create a Google Cloud project with Sheets API enabled
# Download service-account JSON and place at: ~/.config/trading-bot/sheets-key.json

# Set environment variables (or add to .env)
export GOOGLE_SHEET_URL="https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID"
export GITHUB_TOKEN="ghp_your_fine_grained_pat"  # for chart hosting
export GITHUB_REPO="your-username/your-repo"
```

### Chart System

**Crypto & Stock Trade Charts**
- Rendered at **retina resolution** (2360×1120 and higher) immediately after exit
- Hosted on GitHub in a dedicated `chart-images` branch (free, no login required)
- Fallback to local `charts/` folder if upload fails — trade still logs

**How Charts Appear in Sheets**
```
Chart Column (O): =HYPERLINK(url, IMAGE(url))
  ├─ Inline: thumbnail (175px row height)
  └─ Click: full-resolution PNG in new tab (2300+px)
```

**Crypto Charts** — Lightweight-charts engine (TradingView dark theme):
- Green/red candlesticks, exact price scale
- Entry (blue dashed), SL (red), TP (teal) lines
- Entry marker (dotted vertical line)
- Ticker, side, leverage, timeframe badge

**Stock Charts** — Real TradingView screenshot:
- Actual market data (Cboe One feed)
- Entry/SL/TP overlaid via coordinate APIs (pixel-perfect to TradingView's scale)
- Interval auto-picks from trade duration (5m → 15m → 1h)
- Live legend, volume, time scale

### Ledger Columns

| Column | Content | Example |
| --- | --- | --- |
| Entry Time | UTC timestamp | 2026-07-17T06:36:26+00:00 |
| Exit Time | UTC timestamp | 2026-07-17T08:36:31+00:00 |
| Ticker | Symbol | AVAX |
| Side | LONG or SHORT | LONG |
| Entry | Entry price | 6.50 |
| Stop Loss | SL level | 6.43 |
| Take Profit | TP level | 6.70 |
| Exit | Exit price | 6.45 |
| Size | Qty filled | 153.85 |
| Margin Invested ($) | Capital at risk | $99.54 |
| Notional Value ($) | Position size | $995.40 |
| Leverage | 10x or 4x | 10 |
| P&L | Profit/Loss | +$12.31 |
| Reason | Setup type | trend_follow SHORT |
| Chart | Clickable image | [image cell] |

**P&L Column Format**
- Conditional coloring: white at $0, red for losses, green for profits
- Color intensity scales with magnitude

---

## Monitor — `tradepy monitor.py`

Shows both bots at once in one terminal:

```bash
tradepy monitor.py
# or refresh every 60s:
watch -n 60 tradepy monitor.py
```

- **Alpaca stocks:** live portfolio value, open positions, unrealized P&L
- **Crypto paper trades:** balance vs start, per-symbol state, unrealized P&L

---

## Project Structure

```
TradingBot/
├── binance_bot.py              # Primary crypto SMC bot (Binance, 10s sniper, 10x paper leverage)
├── tradingbot.py               # Stock/crypto launcher (live / crypto / backtest)
├── test_bot.py                 # EMA 9/21 crossover test — stocks + crypto (simple, rapid)
├── backtest_crypto.py          # Crypto backtester — hand-mirrors entries ⛔ SEE CAVEAT (2 vs 93 trades)
├── backtest_stocks.py          # Stock backtester — drives the REAL DebbieLaSMC on Alpaca minute bars
├── chart_server.py             # Live chart server — http://localhost:8888
├── monitor.py                  # Real-time P&L monitor
├── chart_renderer.py           # Chart generation: lightweight-charts (crypto) + TradingView (stocks)
├── github_chart_uploader.py    # GitHub Contents API — uploads PNGs to chart-images branch
├── sheets_logger.py            # Google Sheets logging + duplicate guard
├── warmup.py                   # One-time macOS Gatekeeper warmup (only needed for .venv311)
├── healthcheck.py              # Pre-flight connection checker
├── config.py                   # All settings (loads from .env)
├── finbert_utils.py            # FinBERT AI sentiment (lazy-loaded)
├── requirements.txt            # Python dependencies
├── DESIGN.md                   # Strategy deep-dive: SMC psychology, entry/exit, edge cases
│
├── bot/
│   ├── strategy.py             # DebbieLaSMC — multi-asset stock strategy (LumiBot)
│   ├── crypto_strategy.py      # DebbieLaCrypto — 24/7 crypto subclass
│   ├── news.py                 # Alpaca news context (fed to the AI prompt, never a gate)
│   ├── single_instance_lock.py # Stdlib-only duplicate-start guard (imported before pandas)
│   └── indicators.py           # SMC indicators (BOS, sweep, FVG, CHoCH, EQL/EQH, AMD)
│
├── scripts/                    # launchd-aware start/stop/watch/run helpers (see Process Supervision)
├── templates/
│   └── journal.html            # Quant Desk dashboard (equity curve, calendar, ledger)
├── tests/                      # 192 tests — every entry-logic fix is pinned by one
│
├── vendor/
│   └── lightweight-charts.standalone.production.js  # TradingView charting library (vendored)
│
├── crypto_state.json           # Live state — written by binance_bot.py, read by chart + monitor
├── strategy_state.json         # Live state — written by tradingbot.py
├── charts/                     # Local fallback PNG folder (if GitHub upload fails)
├── equity_history.json         # 5s equity samples, 24h retention (feeds the dashboard curve)
└── logs/                       # ⚠️ ONLY for bots started by hand. launchd-managed bots
    └── bot_activity.log        #    log to ~/Library/Logs/debbiela/ — see Process Supervision
```

---

## Strategy Features

| Feature | Detail |
| --- | --- |
| Multi-symbol | 8 crypto + 8 stock symbols simultaneously, each fully independent |
| 10-second sniper | Arms on zone detection, fires entry at exact zone boundary — no 5-min lag |
| Scale-out | Sells 50% at halfway to TP — locks in profit, lets rest run |
| Break-even trail | Moves SL to entry + 0.1% at 60% progress — trade can no longer close at a loss |
| Orphan guard | Restores POSITION_OPEN if state machine loses sync with paper position |
| Startup catch-up | Checks all open positions against live price on every bot restart |
| AI confirmation | 25s timeout, non-blocking — proceeds on technicals if AI hangs |
| Spam suppression | Zone-tap and CHoCH prints only when candle type or alignment changes |
| AMD phase | Detects Accumulation / Manipulation / Distribution context for entry quality |
| EQL/EQH tracking | Counts equal lows/highs — institutional liquidity targets |
| Daily margin cap | Per-day risk limit resets at midnight UTC — won't over-trade one session |
| Bracket orders | Stock entries place SL + TP as live Alpaca orders — visible in TradingView |
| Stale exit | Force-closes any trade open longer than 12 hours |
| Longs + shorts | Both directions on all crypto symbols |

---

## macOS Startup Notes

| Environment | First startup after reboot | Subsequent |
| --- | --- | --- |
| miniforge conda (`runbot`) | ~5–15 seconds | ~2 seconds |
| pip venv (`.venv311`) | 15–30 minutes (Gatekeeper OCSP) | ~10 seconds |

**Always use `runbot` / `tradepy` / `runchart`.** Never activate `.venv311` and run `python3` — it's 30× slower after a reboot.

If you ever accidentally use `.venv311` and it's stuck loading, hit `Ctrl+C`, then open a new terminal tab and use the aliases.

---

## Process Supervision — launchd (the single most expensive class of bug here)

**Almost every "the bot did something insane" incident traced back to the process not
being alive.** The bot's own safeguards — `_ensure_protection`, the 6h stale-trade
timeout, `EOD_FLATTEN_MIN`, `needs_eod_catchup_flatten` — all live inside
`on_trading_iteration`. **None of them can run in a dead process.**

Real example: an NVDA position sat **95.3 hours** while a 6-hour stale timeout *and* a
daily EOD flatten *and* a per-iteration protection check all existed in the code.

### Bare terminal launches are the trap

`caffeinate python3 binance_bot.py` works, but stdout goes **only to the terminal** —
nothing is written to a file. When a trade later looks wrong there is no record to audit.
A frozen bot was found this way: alive for 2h24m, holding open positions, logging nothing.

Always check where a process is actually writing before trusting a log:

```bash
lsof -p <pid> | awk '$4 ~ /^1[uw]/ {print $9}'    # where does stdout really go?
```

### Supervised (recommended)

Three plists live in `~/Library/LaunchAgents/com.debbiela.{binance_bot,stock_bot,test_bot}.plist`
with `RunAtLoad` + `KeepAlive` (auto-restart) and `ThrottleInterval 30`.

```bash
# load AND start (bootstrap — NOT kickstart, which only restarts an already-loaded job)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.debbiela.binance_bot.plist

# restart a loaded job
launchctl kickstart -k gui/$(id -u)/com.debbiela.binance_bot

# real stop (a plain kill is undone by KeepAlive within ~30s)
launchctl bootout gui/$(id -u)/com.debbiela.binance_bot

# what is actually loaded?
launchctl list | grep debbiela
```

**Two macOS gotchas that cost hours:**

- **Logs are NOT in `logs/`.** launchd cannot write into the iCloud-synced Desktop, so
  supervised bots log to **`~/Library/Logs/debbiela/<bot>.log`**. Tailing `logs/<bot>.log`
  under launchd shows a file frozen days ago and looks like a dead bot.
- **Desktop is TCC-protected.** A launchd agent cannot `open()` files in `~/Desktop`
  without Full Disk Access — it fails with `PermissionError: Operation not permitted` even
  though `ls` works. Grant FDA to
  `/Library/Frameworks/Python.framework/Versions/3.14/Resources/Python.app`
  (the real bundle — adding the `bin/python3` symlink does not work).

The `scripts/*.sh` helpers are launchd-aware: `stop_*.sh` unloads the job rather than
issuing a pointless kill, `start_*.sh` re-bootstraps it, and `watch_*.sh` resolves to
whichever log is actually being written.

---

## Other Crypto Exchanges (via CCXT)

Change one line in `connect_exchange()` in `binance_bot.py`:

| Exchange | Change to | Needs account |
| --- | --- | --- |
| Binance (default) | `ccxt.binance` | No — public data |
| Kraken | `ccxt.kraken` | No — public data |
| Coinbase | `ccxt.coinbase` | Yes |
| OKX | `ccxt.okx` | Yes (testnet available) |
| Bybit | `ccxt.bybit` | Yes (testnet available) |

---

## Expected Log Output

```
⏳  Loading trading libraries...  (conda: usually done in <15s)
✅  All libraries ready (0.2 min) — starting trading loop

Checking open positions against current prices…
  ✅  SOL position intact  price=$74.19  SL=$73.82  TP=$74.66

[BTC] state=IDLE  bos=True(bullish/4H)  daily=bullish  candle=doji
[SOL] 🔫 Sniper armed — LONG zone $74.00–$74.41  SL $73.82  TP $74.66  (10-sec precision entry ready)
[SOL] ✦ SNIPER ENTRY — LONG (10-sec precision) — SOL @ $74.1000
      SL $73.8204  TP $74.6593  margin $187.59 → $1,875.88  risk $7.08  reward $14.16  [10x]
[SOL] 💰 SCALED OUT 50% @ $74.41  +$8.23  [10x]  13.45 remaining
[SOL] 🛡 60% to target — SL trailed to break-even $74.17 (winner locked in)
[SOL] 🟢 TP hit $74.66  LONG  P&L: +$7.52  Balance: $9,362.74
```

---

## Current state — read before trusting any number

Everything below is measured from the real ledgers, not estimated.

### Where the edge actually stands (re-audited 2026-08-22, WITH fees)

| | trades | gross | **net of fees** | win rate | expectancy (net) |
|---|---|---|---|---|---|
| **Crypto** | 121 | +$725.16 | **≈ −$15 to −$40** | 62.8% | ≈ **$0 / trade** |
| **Stock** | 40 | −$5,110.46 | −$5,110.46 | 18.4% | −$127.76 / trade |

**The crypto bot is break-even, not profitable.** The +$725 everyone was reading is gross;
$148,063 of notional at a 0.245%/side break-even rate means fees consume the entire edge.
The clearance gate nudges it barely positive (+$12). See *Trading Costs*.

**Crypto exit breakdown** (net of 0.25%/side fees) — where the money actually comes from:

| Exit | n | Gross | Net | Net/trade |
|---|---|---|---|---|
| TARGET | 20 | +$707.26 | **+$609.57** | +$30.48 |
| STALE (6h timeout) | 40 | +$416.63 | **+$184.15** | +$4.60 |
| BREAKEVEN | 34 | −$1.15 | **−$228.35** | −$6.72 |
| STOP | 27 | −$397.58 | −$580.53 | −$21.50 |

Two things this kills:
- **The 6h stale timeout is the second-most-profitable bucket.** Advice to "replace
  time-stops with invalidations" would delete +$184 of net profit.
- **Micro-wins ($0.17, $1.53) are BREAKEVEN exits, not timeouts** — 16 of 19 of them. The
  fix was the `fee_buffer`, not the time-stop.

### Stock bot — the sizing catastrophe is HISTORY, the strategy problem is not

| Period | trades | P&L | max notional |
|---|---|---|---|
| Before 2026-08-09 | 29 | −$4,807.46 | **$72,242** |
| Since 2026-08-09 | 11 | −$303.00 | $4,051 |

**94% of stock losses predate the sizing fix.** Position sizes have been bounded since
2026-08-09; the $72K NVDA and the overnight META gap are July history sitting in the
ledger, not a live danger. Do not read those totals as a present emergency.

What IS still wrong, from the 11 post-fix trades:
- **Three lost 2.7–3.0R when the stop should cap at 1.0R** (exits landed *past* the stop).
- **Hold times of 20–120 hours** — the daily EOD flatten never fired. See
  *Process Supervision*; it cannot fire in a dead process.
- **18.2% win rate at 1:2.33 R:R.** Break-even needs **30%**. Even with every stop honored
  the expectancy is −0.39R/trade. The entry edge, not just execution, is unproven.
- **Backtest of the same logic wins 44.4%** (`backtest_stocks.py`, May 2026). Same class,
  same entries — so the live gap is execution, not entry logic.

### Entry-logic bugs fixed (all TDD, all verified against the real trade that exposed them)

| Bug | Symptom | Fix |
|---|---|---|
| Zones re-armed for free | `bars_in_entry_wait` reset to 0 on every re-arm, so `STALE_ZONE_BARS` could **never** fire — a zone sat ~20 bars while reporting 3 | `carried_zone_age()` + `SymbolState.arm_zone()` |
| Zone snapshotted from a forming candle | Stored edge $6.24 while a fresh call returned $6.326; price was 1.5% below the live zone and only filled against the stale copy | `drop_forming_candle()` on all zone derivation |
| Symmetric entry tolerance | A SHORT filled **below** its supply zone — selling supply at a discount — clearing the threshold by $0.0003 | `price_in_entry_zone()`, tolerance on the far side only |
| Main path had no risk cap | Sized by margin, so a wide stop risked $79 while the sniper path capped at $8 | `cap_qty_for_risk()`, both paths honour `MAX_RISK_DOLLARS` |
| `test_bot` stock sizing | Sized off the **real shared Alpaca balance** (~$95k) → one IWM trade at $98,305 notional | `STOCK_TEST_BALANCE` reference |
| No time-based exit in `test_bot` | A position sat open 48.9 hours | `is_trade_stale()` + `STALE_TRADE_HOURS` |
| Macro date parsing | `Stock Macro` uses `'Tue, Aug 11, 2026'`; only the crypto format was parsed, so **100% of stock rows were silently dropped** and the equity curve rendered flat | all real formats in `_DATE_FORMATS` |
| Missed daily snapshots | A bot down across midnight produced no Macro row and the gap was swallowed | `missing_snapshot_dates()` auto-backfill |

**A reverted "fix" worth remembering:** a check requiring the displacement candle not to
wick into its own gap was added, then reverted on 2026-08-19. It is not the FVG
definition — the middle candle *always* traverses the gap, and that traversal is what
creates the imbalance. It effectively disabled detection (24/7 crypto has no true
inter-candle gaps) and rejected BTC's real +5.75% displacement over $72. Both real cases
are pinned in `tests/test_displacement_fvg.py`. **Don't re-add it.**

**2026-08-22 — duplicate fill on bracket fallback** (`bot/strategy.py`, 6 tests in
`tests/test_duplicate_fill_guard.py`). An Alpaca order POST can raise **after** Alpaca has
accepted it (read timeout, connection reset, unparseable body). The fallback treated every
exception as "nothing happened" and fired a second, naked market order. Real incident
2026-08-14: **two MSFT entries at the identical price $494.77**, minutes apart, both
closing near −3R (−$65.49 and −$61.99). The guard now queries Alpaca for a live order or
open position first and re-sends **only when the broker is provably clean** — and **fails
closed**: if the check itself errors, it does not re-send. A missed entry costs one setup;
a duplicate costs an unintended doubled position. Decision logic is the pure function
`indicators.should_resend_entry_after_error()`.

### Known open issues

**Dashboard equity curve — `fitContent()` cannot un-squash the axis.** When a series' point
count drops sharply (bucketing took 1D from ~17,280 raw 5s samples to 93 15-min buckets)
the chart keeps the OLD bar spacing, pinned near `minBarSpacing` 0.5, and renders the new
points into ~5% of the width as a near-vertical **straight line**. `fitContent()`,
`setVisibleLogicalRange()`, `resetTimeScale()` and even removing/re-adding the series all
fail to recover it. **Setting `barSpacing` explicitly via `chart.applyOptions({timeScale:
{...}})` DOES work** — see `fitByBarSpacing()` in `templates/journal.html`. Two traps:
calling `fitContent()` *after* it re-applies the squash and undoes the fix, and
`getVisibleLogicalRange()` keeps reporting the stale range long after the render corrects,
so **verify by looking at the pixels, not the getter.**

**Equity curve steps by interval, not continuously** (Robinhood-style). `_equity_series`
buckets 5s samples to the range's interval — 1H→1m, 6H→5m, 1D→15m — flooring against the
epoch so edges land on real clock marks (:00, :05, :15). The live tip snaps to the current
bucket so it overwrites the forming point instead of appending one per second.

**GitHub chart uploads fail if `GITHUB_TOKEN` expires** — `upload_chart_to_github` falls
back to saving locally in `charts/`, so charts render fine but the Sheets ledger points at
URLs that were never written and screenshots silently stop appearing. Check with a
`GET /user` against the token; a 401 is the tell.


- **No process supervision.** Nothing restarts a stopped bot. This is the single largest
  source of real loss so far — a 3-day outage missed BTC's biggest move of the month.
  A `launchd` job would close it.
- **Stock strategy is negative-expectancy** (see table). Highest-priority open problem.
- **GitHub token returns `401 Bad credentials`** — trade chart images no longer upload to
  the ledger. Needs a regenerated token.
- **`backtest_crypto.py` covers only one entry path**; AMD-manipulation and generic
  FVG/OB entries aren't replayed yet.
- **Stock backtest can't replay its own timeframe** (Yahoo is daily; strategy is 4H/15m).

### Debugging method that has actually worked

Every real bug here was found by **replaying raw candles from the exchange API against
the indicator functions** for the exact timestamp of the suspect trade — not by reading
code and reasoning about it, and not by trusting the log. Twice the log was frozen and
the reasoning had to be reconstructed entirely from candles and the ledger. When a trade
looks wrong: pull the real candles for that moment, run the indicators on them, and
compare against what the bot stored in `crypto_state.json` / the ledger.

---

## Important Notes

- **The `.env` file must never be committed** — API keys stay local only
- **Always paper trade first** — verify profitability over 2–4 weeks before real money
- **Crypto is volatile** — 2% total risk per session is intentionally conservative
- **10× leverage is simulated** — not borrowed money, just scales position size for realistic P&L tracking
- **SL/TP lines in TradingView** — visible for all new stock trades (bracket + OCO). Old trades pre-dating the current session won't have them — add manually via right-click on the position line
- **Everything is paper trading.** No real capital is at risk in any current configuration.

---

**Good luck. May your edge be sharp and your stops tight.**

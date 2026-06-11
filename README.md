# DEBBIE-LA INSTITUTIONAL SMC BOT

A Python algorithmic trading bot implementing the Debbie-La institutional smart money strategy. Trades stocks and crypto simultaneously using Alpaca (paper or live) and Kraken public data via CCXT.

---

## What This Bot Does

Implements the 4-step "Debbie-La" institutional setup on every symbol in your watchlist:

1. **Scan 4H chart** — macro bias: consolidation, displacement, break of structure (BOS)
2. **Detect liquidity sweep** on 5m chart — the "Wick" that hunts retail stops
3. **Lock in Fair Value Gap (FVG)** — the imbalance zone left by institutional displacement
4. **Wait for retracement** into the FVG zone, enter with AI sentiment confirmation (FinBERT)
5. **Execute bracket order** with structure-based stop loss
6. **Manage risk** — 3:1 R:R, 12h stale-trade exit, supports both longs and shorts

Each symbol runs its own independent state machine: `IDLE → SWEEP_HUNT → ENTRY_WAIT → POSITION_OPEN`

---

## Quick Start

### 1. Set up the environment

```bash
cd /Users/usahealthlife/Desktop/TradingBot
python3.11 -m venv .venv311
source .venv311/bin/activate
pip install -r requirements.txt
```

### 2. Create your .env file

```bash
# Alpaca paper trading keys (required for tradingbot.py and test_bot.py)
ALPACA_API_KEY=your_key_here
ALPACA_API_SECRET=your_secret_here
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_PAPER=True

# Optional: Binance live trading (binance_bot.py uses Kraken public data if left empty)
BINANCE_API_KEY=
BINANCE_SECRET=
BINANCE_TESTNET=True
```

### 3. Run

```bash
source .venv311/bin/activate

python3 tradingbot.py live      # Full SMC strategy — stocks (NYSE hours)
python3 tradingbot.py crypto    # Full SMC strategy — crypto on Alpaca (24/7)
python3 tradingbot.py backtest  # Backtest on full-year 2025 historical data

python3 binance_bot.py          # SMC bot — crypto via Kraken public data (no API needed)
python3 test_bot.py             # EMA crossover test — stocks + crypto simultaneously
python3 monitor.py              # Real-time monitor (stocks + crypto P&L)
```

---

## Modes

### Stock Bot — `python3 tradingbot.py live`

- **Watchlist:** AAPL, QQQ, SPY, NVDA, TSLA, GOOGL
- **Risk:** 2% total ÷ 6 symbols = ~0.33% per trade
- **R:R:** 3:1 (take profit at 3× the risk)
- **Hours:** NYSE market hours (9:30 AM–4:00 PM ET), auto-closes all positions at 3:45 PM
- **Interval:** Scans every 15 minutes
- **Stale exit:** Force-closes any trade open longer than 12 hours

### Crypto Bot — `python3 tradingbot.py crypto`

- **Watchlist:** BTC, ETH, SOL, LINK, LTC, BCH
- **Risk:** 2% total ÷ 6 symbols = ~0.33% per trade
- **Hours:** 24/7 — never sleeps, no market close
- **Interval:** Scans every 15 minutes
- **Account:** Same Alpaca paper trading account as stocks

### Binance Bot — `python3 binance_bot.py`

- **Watchlist:** BTC/USD, ETH/USD, SOL/USD, LINK/USD, LTC/USD
- **Data source:** Kraken public API by default — no account, no API key needed
- **Exchange:** Auto-switches to Binance if `BINANCE_API_KEY` is set in `.env`
- **Paper trading:** In-memory balance ($10,000 start), supports longs AND shorts
- **LTF:** 5m candles | **HTF:** 4H candles
- **FVG expiry:** Resets to IDLE if price hasn't tapped the FVG within 12 bars (1 hour)
- **State saved:** Writes `crypto_state.json` every iteration for `monitor.py` to read
- **R:R:** 3:1 | **Stale exit:** 12 hours

### EMA Crossover Test Bot — `python3 test_bot.py`

A simpler strategy used to verify that order routing works on both pipelines before trusting the full SMC bot. Runs two loops simultaneously in one script:

| Side   | Symbols       | Data          | Timeframe | Mode                        |
| ------ | ------------- | ------------- | --------- | --------------------------- |
| Stocks | SPY           | Alpaca paper  | 1m        | Real orders (paper account) |
| Crypto | BTC, ETH, SOL | Kraken public | 5m        | In-memory paper ($5,000)    |

- **Signal:** EMA 9 crosses above/below EMA 21
- **Stocks:** Waits for NYSE open (9:30 AM ET), fires bracket entries through Alpaca
- **Crypto:** Runs 24/7, supports paper longs and shorts, prints P&L on close
- **No SL/TP:** Pure crossover flip — not a production strategy

### Backtest — `python3 tradingbot.py backtest`

- Runs full-year 2025 (Jan 1 – Dec 31) on SPY using Yahoo historical data
- Generates trade stats, charts, and performance tearsheet

---

## Monitor — `python3 monitor.py`

Open a second terminal while either bot is running:

```bash
source .venv311/bin/activate
python3 monitor.py
```

Shows **both** bots at once:

- **Alpaca stocks** (`tradingbot.py`): live portfolio value, open positions, unrealized P&L
- **Crypto paper trades** (`binance_bot.py`): balance vs start, per-symbol state machine status, unrealized P&L on open positions

The monitor reads `crypto_state.json` (written by `binance_bot.py` every 5 minutes) and the Alpaca API in real-time. Refresh manually or loop it with `watch -n 60 python3 monitor.py`.

---

## Project Structure

```
TradingBot/
├── tradingbot.py           # Main launcher (live / crypto / backtest)
├── binance_bot.py          # SMC bot — Kraken public data, in-memory paper trading
├── test_bot.py             # EMA 9/21 crossover — stocks (Alpaca) + crypto (Kraken) in one script
├── monitor.py              # Real-time monitor — shows both Alpaca + crypto P&L
├── healthcheck.py          # Pre-flight connection checker
├── config.py               # All settings (loads from .env)
├── finbert_utils.py        # FinBERT AI sentiment engine (lazy-loaded)
├── requirements.txt        # Python dependencies
│
├── bot/
│   ├── strategy.py         # DebbieLaSMC — multi-asset stock strategy
│   ├── crypto_strategy.py  # DebbieLaCrypto — 24/7 crypto subclass
│   └── indicators.py       # SMC indicators (BOS, sweep, FVG, OTE, CHoCH)
│
├── crypto_state.json       # Written by binance_bot.py, read by monitor.py (auto-created)
└── logs/
    └── bot_activity.log    # Trade logs (all symbols prefixed [SYMBOL])
```

---

## Strategy Features

| Feature | Detail |
| --- | --- |
| Multi-symbol | Up to 6 symbols simultaneously, each fully independent |
| Risk management | Total risk split evenly across watchlist |
| R:R | 3:1 — take profit at 3× the distance to stop loss |
| Time-based exit | Force-closes any trade open longer than 12 hours |
| FVG expiry | Resets stale setups if price never reaches the entry zone (12 bars) |
| Longs + shorts | Both directions supported in `binance_bot.py` and `test_bot.py` |
| News-optional AI | FinBERT confirms entries; proceeds on technicals if no news available |
| Startup sync | Re-syncs live positions after crash/restart |
| Crypto 24/7 | Same SMC logic runs around the clock on BTC, ETH, SOL, etc. |

---

## Other Crypto Exchanges (via CCXT)

`binance_bot.py` uses CCXT. To switch exchanges, change one line in `connect_exchange()`:

| Exchange | Change to | Needs account |
| --- | --- | --- |
| Kraken (default) | `ccxt.kraken` | No — public data |
| Coinbase | `ccxt.coinbase` | Yes |
| OKX | `ccxt.okx` | Yes (testnet available) |
| Bybit | `ccxt.bybit` | Yes (testnet available) |
| KuCoin | `ccxt.kucoin` | Yes |

---

## Expected Log Output

```
[BTC]  04:01 $62,100.00  state=SWEEP_HUNT  bos=True(bearish)
[BTC]  04:01 STEP 1: HTF BOS → BEARISH. Hunting sweep.
[ETH]  04:06 STEP 2: Bearish FVG locked  $1,658.20-$1,661.40  sweep=yes
[ETH]  04:11 ✅ PAPER SHORT  $1,659.80  qty=0.152000  SL=$1,693.00  TP=$1,558.60
[ETH]  16:11 🟢 TP hit $1,558.60  SHORT  P&L: +$15.42  Balance: $10,015.42

[SPY]  09:45 ✅ BUY @ $529.10  (EMA crossover — test_bot)
[SOL]  07:25 UTC  $65.05  EMA9=65.077  EMA21=65.124  pos=flat  bull=False
```

---

## Important Notes

- **Always paper trade first** — verify profitability over 2–4 weeks before using real money
- **Crypto is volatile** — 2% total risk is intentionally conservative
- **The .env file must never be committed** — API keys stay local only
- **Bracket orders** — Alpaca supports these natively for both stocks and crypto
- **Kraken public data** — `binance_bot.py` and `test_bot.py` pull real crypto prices from Kraken with zero authentication

---

**Good luck. May your edge be sharp and your stops tight.**

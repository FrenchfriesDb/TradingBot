# DEBBIE-LA INSTITUTIONAL SMC BOT

A Python algorithmic trading bot implementing the Debbie-La institutional smart money strategy. Trades stocks and crypto simultaneously using Alpaca (paper or live) and optionally Binance via CCXT.

---

## What This Bot Does

Implements the 4-step "Debbie-La" institutional setup on every symbol in your watchlist:

1. **Scan 4H chart** — macro bias: consolidation, displacement, break of structure
2. **Detect liquidity sweep** on 15m chart — the "Wick" that hunts retail stops
3. **Confirm market structure shift** — the "Rocket" (aggressive institutional reversal)
4. **Wait for retracement** into Fair Value Gap + Fibonacci OTE zone (38.2%–61.8%)
5. **Execute bracket order** with AI sentiment confirmation (FinBERT)
6. **Manage risk** — structure-based stops, 2:1 R:R, 4h stale-trade exit

Each symbol runs its own independent state machine. Risk is split evenly across the watchlist.

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
# Copy this and fill in your Alpaca paper trading keys
ALPACA_API_KEY=your_key_here
ALPACA_API_SECRET=your_secret_here
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_PAPER=True

# Optional: Binance testnet (get keys at testnet.binance.vision)
BINANCE_API_KEY=
BINANCE_SECRET=
BINANCE_TESTNET=True
BINANCE_SYMBOL=BTC/USDT
```

### 3. Run

```bash
source .venv311/bin/activate

python3 tradingbot.py live      # Stocks (market hours: 9:30 AM–4:00 PM ET)
python3 tradingbot.py crypto    # Crypto on Alpaca (24/7, same paper account)
python3 tradingbot.py backtest  # Backtest on historical data
python3 binance_bot.py          # Binance testnet (needs BINANCE_* keys in .env)
```

---

## Modes

### Stock Bot — `python3 tradingbot.py live`

- **Watchlist:** AAPL, QQQ, SPY, NVDA, TSLA, GOOGL
- **Risk:** 3% total ÷ 6 symbols = ~0.5% per trade
- **Hours:** NYSE market hours (9:30 AM–4:00 PM ET), auto-closes all positions at 3:45 PM
- **Interval:** Scans every 15 minutes

### Crypto Bot — `python3 tradingbot.py crypto`

- **Watchlist:** BTC, ETH, SOL, DOGE, AVAX, LINK
- **Risk:** 2% total ÷ 6 symbols = ~0.33% per trade
- **Hours:** 24/7 — never sleeps, no market close
- **Interval:** Scans every 15 minutes
- **Account:** Same Alpaca paper trading account as stocks

### Binance Bot — `python3 binance_bot.py`

- **Symbol:** BTC/USDT (configurable in .env)
- **Exchange:** Binance testnet (sandbox) or live
- **Risk:** Configured via `BINANCE_CASH_AT_RISK` in .env
- **Interval:** 15 minutes continuous loop

### Backtest — `python3 tradingbot.py backtest`

- Runs on SPY using Yahoo historical data
- Generates trade stats, charts, and performance tearsheet

---

## Monitor

Open a second terminal and run:

```bash
source .venv311/bin/activate
python3 monitor.py
```

Shows live portfolio value, open positions with P&L, and recent bot activity.

---

## Project Structure

```
TradingBot/
├── tradingbot.py           # Main launcher (live / crypto / backtest)
├── binance_bot.py          # Standalone Binance CCXT bot
├── monitor.py              # Real-time portfolio monitor
├── healthcheck.py          # Pre-flight connection checker
├── config.py               # All settings (loads from .env)
├── finbert_utils.py        # FinBERT AI sentiment engine
├── requirements.txt        # Python dependencies
│
├── bot/
│   ├── strategy.py         # DebbieLaSMC — multi-asset stock strategy
│   ├── crypto_strategy.py  # DebbieLaCrypto — 24/7 crypto subclass
│   └── indicators.py       # SMC indicators (sweep, FVG, OTE, CHoCH)
│
└── logs/
    └── bot_activity.log    # Trade logs (all symbols prefixed [SYMBOL])
```

---

## Strategy Features

| Feature | Detail |
| ------- | ------ |
| Multi-symbol | Up to 6 symbols simultaneously, each independent |
| Risk management | Total risk split evenly across watchlist |
| Time-based exit | Force-close any trade open longer than 4 hours |
| News-optional AI | Proceeds on technicals if no news is available |
| Startup sync | Re-syncs state from live positions after crash/restart |
| Crypto support | Same SMC logic runs 24/7 on BTC, ETH, SOL, etc. |

---

## Other Crypto Exchanges (via CCXT)

The Binance bot uses CCXT. To switch exchanges, change one line in `binance_bot.py`:

| Exchange | Change to | Testnet |
| -------- | --------- | ------- |
| Coinbase | `ccxt.coinbase` | No |
| Kraken | `ccxt.kraken` | No |
| OKX | `ccxt.okx` | Yes |
| Bybit | `ccxt.bybit` | Yes |
| KuCoin | `ccxt.kucoin` | Yes |

---

## Expected Log Output

```
[SPY]  09:45 STEP 1-2: BLEED+SWEEP → BULLISH | Wick: 528.40
[NVDA] 09:45 STEP 3: MSS+FVG confirmed | OTE 131.20-133.80
[SPY]  10:00 AI: POSITIVE (74.2%) → ✅
[SPY]  10:00 ✅ ENTRY | Price: 529.10 | SL: 527.43 | TP: 532.44 | Qty: 9.0
[BTC]  14:15 STEP 1-2: BLEED+SWEEP → BULLISH | Wick: 103420.00
[NVDA] 16:45 ⏰ Stale exit after 4.1h — closing position.
```

---

## Important Notes

- **Always paper trade first** — verify profitability over 2–4 weeks before using real money
- **Crypto is volatile** — 2% total risk is intentionally lower than the 3% for stocks
- **The .env file must never be committed** — API keys stay local only
- **Bracket orders** — Alpaca supports these natively for both stocks and crypto

---

**Good luck. May your edge be sharp and your stops tight.**

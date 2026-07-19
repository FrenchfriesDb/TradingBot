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

```bash
# Primary bots (live trading)
runbot                              # Crypto SMC bot (Binance, 24/7)
tradepy tradingbot.py live          # Stock SMC bot (NYSE hours)

# Live monitoring
runchart                            # Live chart at http://localhost:8888
tradepy monitor.py                  # Real-time P&L monitor

# Alternative / testing
tradepy tradingbot.py crypto        # Crypto via Alpaca (24/7, alternative to binance_bot)
tradepy tradingbot.py backtest      # Backtest 2025 on SPY (full-year performance)

# Test bot (1H/1M sweep-reversal, rapid entries for pipeline verification)
tradepy test_bot.py                 # Stocks (IWM) + crypto (8 pairs) together
tradepy test_bot.py --crypto-only   # Crypto pipeline only (24/7)
```

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

- **Watchlist:** BTC/USD, ETH/USD, SOL/USD, XRP/USD, AVAX/USD, DOGE/USD, POL/USD, ADA/USD
- **Data:** Binance public API via CCXT (no account needed)
- **Paper balance:** $10,000 start | **Leverage:** 10× simulated
- **Candles:** 5m (LTF) + 4H (HTF)
- **Risk per trade:** 2% of balance (daily cap — resets midnight UTC)
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

### Backtest — `tradepy tradingbot.py backtest`

Full-year 2025 (Jan 1 – Dec 31) on SPY using Yahoo historical data. Generates trade stats and performance tearsheet.

---

### Test Bot — `tradepy test_bot.py`

**1H/1M sweep-reversal** bot for verifying execution infrastructure with fast, frequent entries. 1H swing high/low = liquidity pools (recalculated hourly); a 1M candle that wicks past a pool and closes back inside = sweep+reversal entry; opposite pool = target, min 1:3 R:R, 1% risk per trade.

**Stocks** (background thread) — IWM via Alpaca paper, long-only, NYSE hours. Kept off the SMC bot's watchlist on purpose so both bots never trade the same ticker.

**Crypto** (main thread) — BTC, ETH, SOL, DOGE, XRP, AVAX, POL, ADA via Coinbase public data, 24/7, longs + shorts, $5,000 in-memory paper balance, $200/day notional cap.

```bash
tradepy test_bot.py                 # both pipelines
tradepy test_bot.py --crypto-only   # skip the stock thread
```

Every closed test trade logs to its own **Test Ledger** tab in the Google Sheet (auto-created, same 15 columns and clickable charts as the main bots — crypto gets the dashboard-style render, stocks get a real TradingView screenshot; Leverage column reads 1 since the test bot is unleveraged). Test trades never touch the real Crypto/Stock Ledger tabs.

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
│   └── indicators.py           # SMC indicators (BOS, sweep, FVG, CHoCH, EQL/EQH, AMD)
│
├── vendor/
│   └── lightweight-charts.standalone.production.js  # TradingView charting library (vendored)
│
├── crypto_state.json           # Live state — written by binance_bot.py, read by chart + monitor
├── strategy_state.json         # Live state — written by tradingbot.py
├── charts/                     # Local fallback PNG folder (if GitHub upload fails)
└── logs/
    └── bot_activity.log        # Trade logs (all symbols prefixed [SYMBOL])
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

## Important Notes

- **The `.env` file must never be committed** — API keys stay local only
- **Always paper trade first** — verify profitability over 2–4 weeks before real money
- **Crypto is volatile** — 2% total risk per session is intentionally conservative
- **10× leverage is simulated** — not borrowed money, just scales position size for realistic P&L tracking
- **SL/TP lines in TradingView** — visible for all new stock trades (bracket + OCO). Old trades pre-dating the current session won't have them — add manually via right-click on the position line

---

**Good luck. May your edge be sharp and your stops tight.**

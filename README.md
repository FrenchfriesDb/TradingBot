# 🤖 DEBBIE-LA INSTITUTIONAL SMC BOT

A Python algorithmic trading bot that executes your institutional smart money order flow strategy on Alpaca.

## 📋 What This Bot Does

Implements the "Debbie-La" institutional trading setup:

1. **Scan 4H chart** for macro bias (consolidation, displacement, support/resistance)
2. **Detect liquidity sweep** on 15m chart (the "Wick")
3. **Confirm market structure shift** (the "Rocket")  
4. **Wait for price retracement** into Fair Value Gap + Fibonacci OTE zone
5. **Execute trade** with AI sentiment confirmation
6. **Manage risk** with structure-based stops and 2:1 risk:reward targets

## 🚀 Quick Start (Paper Trading in 3 Steps)

### Step 1: Install Dependencies

```bash
cd /Users/usahealthlife/Desktop/TradingBot
pip install -r requirements.txt
```

This installs:
- `lumibot` - Trading engine
- `alpaca-trade-api` - Broker connection  
- `transformers` + `torch` - AI sentiment analysis
- `pandas`, `numpy` - Data handling

### Step 2: Verify Alpaca Connection

```bash
python3 -c "
from alpaca_trade_api import REST
from config import API_KEY, API_SECRET, BASE_URL

api = REST(base_url=BASE_URL, key_id=API_KEY, secret_key=API_SECRET)
account = api.get_account()
print(f'✅ Connected! Account: {account.account_number}')
print(f'📊 Portfolio: ${float(account.portfolio_value):,.2f}')
print(f'💰 Cash: ${float(account.cash):,.2f}')
"
```

If you see ✅, you're connected to Alpaca paper trading.

### Step 3: Launch Bot

**Option A: Interactive Launcher (Recommended)**
```bash
chmod +x launch.sh
./launch.sh
```
Then choose: `1) Paper Trading`

**Option B: Direct Python**
```bash
python3 tradingbot.py live
```

The bot will start and execute trades based on your strategy every 15 minutes.

---

## 📊 Monitor Bot Activity in Real-Time

Open **another terminal** and run:

```bash
python3 monitor.py
```

This shows:
- Current portfolio value
- Open positions (qty, entry price, P&L)
- Latest bot activity log

Refresh every minute to see updates.

---

## 🔧 Configuration

Edit `config.py` to customize:

```python
SYMBOL = "SPY"  # Stock to trade (change to any ticker)
CASH_AT_RISK = 0.03  # 3% per trade (conservative for testing)
TIMEFRAME_HTF = "4H"  # Use 4H for macro bias
TIMEFRAME_LTF = "15m"  # Use 15m for execution
MIN_SENTIMENT_CONFIDENCE = 0.60  # AI confidence threshold
```

### Important: Paper Trading vs Real Money

**Paper Trading (SAFE):**
```python
PAPER_TRADING = True  # Simulates trades with virtual money
API_KEY = "..."  # Use paper trading credentials
```

**Real Money (CAREFUL):**
```python
PAPER_TRADING = False  # REAL TRADES ON REAL MONEY
API_KEY = "..."  # Use live trading credentials
CASH_AT_RISK = 0.01  # Reduce to 1% per trade
```

**⚠️ WARNING: Start with paper trading, prove profitability first!**

---

## 📈 Expected Output

When the bot finds a setup, you'll see logs like:

```
2026-06-08 14:30:00 - 📍 [STEP 1-2] BLEED + SWEEP confirmed. Bias: BULLISH
2026-06-08 14:35:00 - 🚀 [STEP 3] MSS + FVG confirmed! FVG Zone: 422.50 - 423.10
2026-06-08 14:45:00 - 🧠 AI Sentiment: POSITIVE (73.5%)
2026-06-08 14:50:00 - ✅ [STEP 4] ENTRY EXECUTION!
                      Entry: 422.80 | SL: 421.20 | TP: 424.40
                      Risk:Reward = 1:2.0
```

---

## 📁 Project Structure

```
TradingBot/
├── tradingbot.py           # Main bot launcher (backtest/live modes)
├── launch.sh               # Easy launcher script
├── monitor.py              # Real-time portfolio monitor
├── config.py               # All bot settings
├── requirements.txt        # Python dependencies
├── finbert_utils.py        # AI sentiment engine
│
├── bot/
│   ├── __init__.py
│   ├── strategy.py         # The Debbie-La strategy logic
│   └── indicators.py       # Technical indicators (sweeps, FVG, etc)
│
└── logs/
    ├── bot_activity.log    # Bot trade logs
    └── MLTrader_*.csv      # Backtest results
```

---

## 🧪 Backtest Before Live Trading

Test your strategy on historical data first:

```bash
python3 tradingbot.py backtest
```

This runs 1 week of backtests (2026-06-01 to 2026-06-07) and generates:
- Trade statistics (win rate, avg profit, drawdown)
- Charts and tearsheet
- Performance metrics

**Check backtest results before paper trading!**

---

## ⚠️ Important Notes

1. **Paper trading ≠ Real trading**
   - No slippage simulation
   - Orders fill instantly
   - Real trading has gaps and delays

2. **AI Sentiment requires news**
   - Bot needs recent news/headlines
   - Market closures = no sentiment data
   - Adjust confidence thresholds if needed

3. **Strategy is a framework**
   - Test in multiple market regimes
   - Paper trade for 2-4 weeks before real money
   - Only scale up if win rate > 55%

4. **Market hours**
   - Bot exits all positions at 3:45 PM ET
   - Only trades 9:30 AM - 3:45 PM ET

---

## 🐛 Troubleshooting

**Bot not finding setups?**
- Check that market is open (9:30 AM - 4:00 PM ET)
- Verify news is available (required for AI sentiment)
- Review logs: `tail -f logs/bot_activity.log`

**Permission denied on launch.sh?**
```bash
chmod +x launch.sh
```

**Import errors?**
```bash
pip install --upgrade -r requirements.txt
```

**Can't connect to Alpaca?**
- Verify API_KEY and API_SECRET in config.py
- Check internet connection
- Alpaca API might be down (rare)

---

## 📞 Next Steps

1. ✅ Install dependencies: `pip install -r requirements.txt`
2. ✅ Verify connection: `python3 monitor.py`
3. ✅ Run backtest: `python3 tradingbot.py backtest`
4. ✅ Paper trade: `python3 tradingbot.py live`
5. ✅ Monitor activity: `python3 monitor.py` (in another terminal)
6. ✅ After 2-4 weeks, review results and upgrade to real money

---

**Good luck! May your edge be sharp and your stops tight. 🎯**

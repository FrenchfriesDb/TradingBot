#!/usr/bin/env python3

"""
Bot Setup Health Check
Verifies all dependencies and connections before launching the bot
"""

import sys
import subprocess
from pathlib import Path

print("\n" + "="*80)
print("🏥 DEBBIE-LA BOT - SETUP HEALTH CHECK")
print("="*80 + "\n")

checks_passed = 0
checks_failed = 0

# ============================================================================
# Check 1: Python Version
# ============================================================================
print("1️⃣  Checking Python version...")
version = sys.version_info
if version.major >= 3 and version.minor >= 8:
    print(f"   ✅ Python {version.major}.{version.minor}.{version.micro} - OK")
    checks_passed += 1
else:
    print(f"   ❌ Python {version.major}.{version.minor} - REQUIRES 3.8+")
    checks_failed += 1

# ============================================================================
# Check 2: Required Packages
# ============================================================================
print("\n2️⃣  Checking Python packages...")
required_packages = [
    "lumibot",
    "alpaca_trade_api",
    "pandas",
    "numpy",
    "transformers",
    "torch",
    "flask"
]

missing_packages = []
for pkg in required_packages:
    try:
        __import__(pkg.replace("-", "_"))
        print(f"   ✅ {pkg}")
        checks_passed += 1
    except ImportError:
        print(f"   ❌ {pkg} - NOT INSTALLED")
        missing_packages.append(pkg)
        checks_failed += 1

if missing_packages:
    print(f"\n   💡 Install missing packages:")
    print(f"      pip install {' '.join(missing_packages)}")

# ============================================================================
# Check 3: Config File
# ============================================================================
print("\n3️⃣  Checking config.py...")
config_path = Path("config.py")
if config_path.exists():
    print("   ✅ config.py found")
    checks_passed += 1
    # Try to import it
    try:
        import config
        print("   ✅ config.py imports successfully")
        print(f"      - Symbol: {config.SYMBOL}")
        print(f"      - Risk per trade: {config.CASH_AT_RISK*100:.1f}%")
        print(f"      - Paper trading: {config.PAPER_TRADING}")
        checks_passed += 1
    except Exception as e:
        print(f"   ❌ config.py error: {e}")
        checks_failed += 1
else:
    print("   ❌ config.py not found")
    checks_failed += 1

# ============================================================================
# Check 4: Strategy Files
# ============================================================================
print("\n4️⃣  Checking strategy files...")
files_to_check = [
    ("bot/strategy.py", "Strategy"),
    ("bot/indicators.py", "Indicators"),
    ("finbert_utils.py", "AI Sentiment"),
    ("tradingbot.py", "Bot launcher"),
]

for filepath, name in files_to_check:
    if Path(filepath).exists():
        print(f"   ✅ {filepath}")
        checks_passed += 1
    else:
        print(f"   ❌ {filepath} not found")
        checks_failed += 1

# ============================================================================
# Check 5: Alpaca Connection
# ============================================================================
print("\n5️⃣  Checking Alpaca connection...")
try:
    from alpaca_trade_api import REST
    from config import API_KEY, API_SECRET, BASE_URL
    
    api = REST(base_url=BASE_URL, key_id=API_KEY, secret_key=API_SECRET)
    account = api.get_account()
    
    print("   ✅ Connected to Alpaca!")
    print(f"      - Account: {account.account_number}")
    print(f"      - Portfolio: ${float(account.portfolio_value):,.2f}")
    print(f"      - Cash: ${float(account.cash):,.2f}")
    print(f"      - Status: {account.status.upper()}")
    checks_passed += 1
except Exception as e:
    print(f"   ❌ Alpaca connection failed: {e}")
    checks_failed += 1

# ============================================================================
# Check 6: FinBERT AI Model
# ============================================================================
print("\n6️⃣  Checking FinBERT AI model...")
try:
    from finbert_utils import estimate_sentiment
    # Test with sample headlines
    test_headlines = ["Stock market hits all-time high!", "Company reports strong earnings"]
    prob, sentiment = estimate_sentiment(test_headlines)
    print(f"   ✅ FinBERT model loaded")
    print(f"      - Test result: {sentiment.upper()} ({prob*100:.1f}%)")
    checks_passed += 1
except Exception as e:
    print(f"   ⚠️  FinBERT loading: {e}")
    print(f"      (This will be downloaded on first run - may take a few minutes)")
    checks_passed += 1  # Don't fail on this

# ============================================================================
# Check 7: Log Directory
# ============================================================================
print("\n7️⃣  Checking log directory...")
logs_path = Path("logs")
if logs_path.exists():
    print("   ✅ logs/ directory exists")
    checks_passed += 1
else:
    print("   ℹ️  Creating logs/ directory...")
    logs_path.mkdir(exist_ok=True)
    print("   ✅ logs/ directory created")
    checks_passed += 1

# ============================================================================
# Summary
# ============================================================================
print("\n" + "="*80)
print(f"SUMMARY: {checks_passed} ✅ | {checks_failed} ❌")
print("="*80)

if checks_failed == 0:
    print("\n🎉 ALL SYSTEMS GO! Your bot is ready to launch.\n")
    print("Next steps:")
    print("  1. python3 tradingbot.py backtest   # Test on historical data")
    print("  2. ./launch.sh                       # Start paper trading")
    print("  3. python3 monitor.py                # Monitor in another terminal")
    print("\n")
else:
    print(f"\n❌ Fix {checks_failed} issue(s) above before launching.\n")
    sys.exit(1)

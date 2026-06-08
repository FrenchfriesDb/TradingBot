#!/bin/bash

# ============================================================================
# DEBBIE-LA BOT LAUNCHER
# ============================================================================

echo "╔════════════════════════════════════════════════════════════╗"
echo "║  🤖 DEBBIE-LA INSTITUTIONAL SMC BOT - LAUNCHER            ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""

# Check if we're in the right directory
if [ ! -f "tradingbot.py" ]; then
    echo "❌ Error: tradingbot.py not found. Are you in the TradingBot directory?"
    exit 1
fi

# Check Python installation
if ! command -v python3 &> /dev/null; then
    echo "❌ Error: Python3 not found. Please install Python 3.8+"
    exit 1
fi

# Dependencies are checked by healthcheck.py - just run the mode selection
clear
echo ""
echo "Select mode:"
echo "  1) Paper Trading (Simulate trades, no real money)"
echo "  2) Backtest (Test on historical data)"
echo ""
read -p "Enter choice [1 or 2]: " choice

case $choice in
    1)
        echo ""
        echo "🔴 LAUNCHING PAPER TRADING MODE"
        echo "📊 Bot will execute real orders on Alpaca PAPER account"
        echo "💰 No real money at risk - this is SAFE for testing"
        echo ""
        sleep 2
        python3 tradingbot.py live
        ;;
    2)
        echo ""
        echo "📈 LAUNCHING BACKTEST MODE"
        echo "Testing strategy on historical data from 2026-06-01 to 2026-06-07"
        echo ""
        sleep 2
        python3 tradingbot.py backtest
        ;;
    *)
        echo "❌ Invalid choice"
        exit 1
        ;;
esac

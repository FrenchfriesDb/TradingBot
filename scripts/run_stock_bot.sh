#!/bin/bash
# Run the stock + Alpaca-crypto bot IN THIS WINDOW. Ctrl-C stops it. Still writes logs/stock_bot.log.
source "$(dirname "$0")/_lib.sh"
_bot_run_foreground "stock_bot" ".tradingbot_live.lock" python3 tradingbot.py live

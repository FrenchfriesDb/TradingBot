#!/bin/bash
# Start the stock + Alpaca-crypto bot (tradingbot.py live), detached and sleep-resistant.
source "$(dirname "$0")/_lib.sh"
_bot_start "stock_bot" ".tradingbot_live.lock" python3 tradingbot.py live

#!/bin/bash
# Watch the stock + Alpaca-crypto bot live in this terminal (bot stays detached).
source "$(dirname "$0")/_lib.sh"
_bot_watch "stock_bot" ".tradingbot_live.lock"

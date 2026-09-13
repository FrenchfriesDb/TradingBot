#!/bin/bash
# Start the crypto pure-sniper bot (binance_bot.py), detached and sleep-resistant.
source "$(dirname "$0")/_lib.sh"
_bot_start "binance_bot" ".binance_bot.lock" python3 binance_bot.py

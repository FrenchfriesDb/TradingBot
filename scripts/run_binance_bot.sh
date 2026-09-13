#!/bin/bash
# Run the crypto SMC bot IN THIS WINDOW. Ctrl-C stops it. Still writes logs/binance_bot.log.
source "$(dirname "$0")/_lib.sh"
_bot_run_foreground "binance_bot" ".binance_bot.lock" python3 binance_bot.py

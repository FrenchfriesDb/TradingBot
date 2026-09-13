#!/bin/bash
# Watch the crypto SMC bot live in this terminal (bot stays detached).
source "$(dirname "$0")/_lib.sh"
_bot_watch "binance_bot" ".binance_bot.lock"

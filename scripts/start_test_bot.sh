#!/bin/bash
# Start the crypto sweep+trend-filter test bot (test_bot.py --crypto-only), detached
# and sleep-resistant.
source "$(dirname "$0")/_lib.sh"
_bot_start "test_bot" ".test_bot.lock" python3 test_bot.py --crypto-only

#!/bin/bash
# Run the sweep test bot IN THIS WINDOW. Ctrl-C stops it. Still writes logs/test_bot.log.
source "$(dirname "$0")/_lib.sh"
_bot_run_foreground "test_bot" ".test_bot.lock" python3 test_bot.py --crypto-only

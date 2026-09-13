#!/bin/bash
# Watch the sweep test bot live in this terminal (bot stays detached).
source "$(dirname "$0")/_lib.sh"
_bot_watch "test_bot" ".test_bot.lock"

#!/usr/bin/env python3
"""Check every bot's log and alarm when one goes quiet. Run from launchd every 5 min.

Deliberately has NO dependency on the bots themselves — it must keep working when they
are the thing that is broken. Stdlib only, no pandas, no ccxt, no network.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.watchdog import (silence_verdict, is_active_now, newest_log,
                          CRYPTO_MAX_SILENCE, STOCK_MAX_SILENCE)
from bot.indicators import is_regular_session

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# BOTH log locations. A bot relaunched by hand writes only the repo one (tee_stdout_to
# runs inside Python); launchd writes only its own. Statting one path alone is what made
# this watchdog scream SILENT for 1534m about a bot that was trading perfectly.
LOGS = os.path.join(os.path.expanduser("~"), "Library", "Logs", "debbiela")
REPO_LOGS = os.path.join(REPO, "logs")
STATE = os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                     "debbiela", "watchdog_state.json")

BOTS = [
    ("stock_bot",   "stock",  "tradingbot.py live", STOCK_MAX_SILENCE),
    ("binance_bot", "crypto", "binance_bot.py",     CRYPTO_MAX_SILENCE),
    ("test_bot",    "crypto", "test_bot.py",        CRYPTO_MAX_SILENCE),
]


def _load_state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
    except OSError:
        pass          # a state write failure must never stop the alarm itself


def _alive(pattern):
    try:
        return subprocess.run(["pgrep", "-f", pattern], capture_output=True,
                              timeout=10).returncode == 0
    except Exception:
        return True   # cannot tell -> do not manufacture a process-down alarm


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _alarm(name, why):
    msg = f"{name}: {why}"
    snd = "/System/Library/Sounds/Basso.aiff"
    # Sound first: afplay needs no permission, unlike notifications.
    subprocess.Popen(["sh", "-c", f"afplay '{snd}' 2>/dev/null; sleep 0.4; "
                                  f"afplay '{snd}' 2>/dev/null"])
    subprocess.Popen(["osascript", "-e",
                      f'display notification "{why}" with title "BOT SILENT — {name}"'])
    subprocess.Popen(["say", f"{name.replace('_', ' ')} has gone silent"])
    print(f"[WATCHDOG] 🚨 {msg}", flush=True)


def main():
    now = time.time()
    et = datetime.now(ZoneInfo("America/New_York"))
    state = _load_state()
    for name, kind, pattern, limit in BOTS:
        src, mtime = newest_log([
            ("launchd", _mtime(os.path.join(LOGS, f"{name}.log"))),
            ("BARE",    _mtime(os.path.join(REPO_LOGS, f"{name}.log"))),
        ])
        active = is_active_now(kind, et.weekday(), et.hour, et.minute, is_regular_session)
        alarm, why = silence_verdict(
            now, mtime, limit, active,
            last_alarm_ts=state.get(name), alive=_alive(pattern),
            source=None if src == "launchd" else src)
        if alarm:
            _alarm(name, why)
            state[name] = now
        else:
            print(f"[WATCHDOG] {name}: {why}", flush=True)
    _save_state(state)


if __name__ == "__main__":
    main()

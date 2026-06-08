"""
Thin wrapper to run the stock bot (`tradingbot.py`) in live mode.
Keeps launcher commands simple and gives a place for per-bot env overrides.
"""
import os
import sys

if __name__ == "__main__":
    # Optional: set any bot-specific env vars here
    cmd = "python3 tradingbot.py live"
    print("Running:", cmd)
    rc = os.system(cmd)
    sys.exit(rc)

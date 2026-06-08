"""
Thin wrapper to run the crypto bot (`binance_bot.py`) in live mode.
Use this to add exchange-specific env overrides later.
"""
import os
import sys

if __name__ == "__main__":
    # Optional: set any crypto-specific env vars here
    cmd = "python3 binance_bot.py live"
    print("Running:", cmd)
    rc = os.system(cmd)
    sys.exit(rc)

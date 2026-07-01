"""
Run this ONCE with: python3 warmup.py
It forces macOS Gatekeeper to verify every compiled binary (.so) the bot uses.
After this finishes, binance_bot.py will start in under 10 seconds forever.
"""
import time
start = time.time()

def step(name):
    print(f"  [{int(time.time()-start):>3}s] {name}...", flush=True)

print("=== ONE-TIME macOS WARMUP — do not close this window ===\n")

step("pandas")
import pandas
print(f"       ✓ pandas ({time.time()-start:.0f}s)", flush=True)

step("numpy")
import numpy
print(f"       ✓ numpy ({time.time()-start:.0f}s)", flush=True)

step("ccxt")
import ccxt
print(f"       ✓ ccxt ({time.time()-start:.0f}s)", flush=True)

step("requests")
import requests
print(f"       ✓ requests ({time.time()-start:.0f}s)", flush=True)

step("openai / anthropic")
try:
    import openai
    print(f"       ✓ openai ({time.time()-start:.0f}s)", flush=True)
except ImportError:
    print("       – openai not installed", flush=True)
try:
    import anthropic
    print(f"       ✓ anthropic ({time.time()-start:.0f}s)", flush=True)
except ImportError:
    print("       – anthropic not installed", flush=True)

step("dotenv")
from dotenv import load_dotenv
print(f"       ✓ dotenv ({time.time()-start:.0f}s)", flush=True)

step("datetime / json / subprocess")
from datetime import datetime, timezone
import json, subprocess
print(f"       ✓ stdlib ({time.time()-start:.0f}s)", flush=True)

print(f"\n=== DONE in {time.time()-start:.0f}s — bot will now start fast ===")

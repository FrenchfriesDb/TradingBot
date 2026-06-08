"""
Master launcher to run Stock (tradingbot.py) and Crypto (binance_bot.py) in separate processes.
Usage:
  python3 main_launcher.py           # runs both
  python3 main_launcher.py --dry-run # prints commands only

This keeps the two bots isolated and easier to monitor.
"""

import argparse
import subprocess
import os
import signal
import sys
import time


def start_process(cmd):
    # start in its own process group so we can kill the whole group
    return subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid)


def main(stock_cmd, crypto_cmd, dry_run=False):
    if dry_run:
        print("Stock command:", stock_cmd)
        print("Crypto command:", crypto_cmd)
        return 0

    procs = []
    try:
        print("Starting stock bot:", stock_cmd)
        p1 = start_process(stock_cmd)
        procs.append(("stock", p1))

        time.sleep(0.5)

        print("Starting crypto bot:", crypto_cmd)
        p2 = start_process(crypto_cmd)
        procs.append(("crypto", p2))

        # wait loop
        while True:
            for name, p in list(procs):
                ret = p.poll()
                if ret is not None:
                    print(f"{name} process exited with code {ret}")
                    procs.remove((name, p))
            if not procs:
                print("All processes exited.")
                break
            time.sleep(1)

    except KeyboardInterrupt:
        print("KeyboardInterrupt received — terminating children...")
        for name, p in procs:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                pass
        return 130

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stock-cmd", help="Command to run the stock bot", default="python3 tradingbot.py live")
    parser.add_argument("--crypto-cmd", help="Command to run the crypto bot", default="python3 binance_bot.py live")
    parser.add_argument("--dry-run", action="store_true", help="Print the commands and exit")
    args = parser.parse_args()

    sys.exit(main(args.stock_cmd, args.crypto_cmd, args.dry_run))

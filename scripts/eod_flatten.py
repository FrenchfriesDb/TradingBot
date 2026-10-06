#!/usr/bin/env python3
"""Close every stock position before the bell — INDEPENDENTLY of the trading bot.

WHY THIS EXISTS. The EOD flatten lived inside DebbieLaSMC.on_trading_iteration, so it only
ran if the trading loop ran. On 2026-10-05 the stock bot stalled from 10:12 to 13:01 — about
34 missed 5-minute cycles — and woke after the close. The 12:45 flatten window passed with no
iteration in it, and META was carried overnight.

A safety net that depends on the thing it is protecting against is not a safety net. This
script talks to Alpaca directly: no lumibot, no strategy object, no shared state. It works
when the bot is stalled, crash-looping, or dead.

The GTC bracket legs the bot leaves at the broker are NOT equivalent protection. A stop is a
trigger, not a fill: if the stock gaps below it overnight, the stop becomes a market order and
fills at the gap, not at the stop price. Flattening before the close is what actually avoids
that, which is the whole reason the rule exists.

Gated on ET minutes-to-close rather than a wall-clock launchd time, so it cannot drift with
daylight saving: run it as often as you like, it acts only inside the window.
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FLATTEN_WINDOW_MIN = int(os.getenv("EOD_FLATTEN_MIN", "15"))
ET = ZoneInfo("America/New_York")


def minutes_to_close(clock):
    """Minutes until the session close, or None when the market is shut."""
    if not getattr(clock, "is_open", False):
        return None
    nxt = getattr(clock, "next_close", None)
    if nxt is None:
        return None
    now = getattr(clock, "timestamp", None) or datetime.now(ET)
    return (nxt - now).total_seconds() / 60.0


def main():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    from alpaca.trading.client import TradingClient

    key, sec = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_API_SECRET")
    if not key or not sec:
        print("[EOD] no Alpaca credentials — cannot flatten", flush=True)
        return 2

    client = TradingClient(key, sec, paper=True)
    mtc = minutes_to_close(client.get_clock())
    stamp = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")

    if mtc is None:
        print(f"[EOD] {stamp} market closed — nothing to do", flush=True)
        return 0
    if mtc > FLATTEN_WINDOW_MIN:
        print(f"[EOD] {stamp} {mtc:.0f}m to close (> {FLATTEN_WINDOW_MIN}m) — not yet", flush=True)
        return 0

    positions = client.get_all_positions()
    if not positions:
        print(f"[EOD] {stamp} {mtc:.0f}m to close — already flat ✅", flush=True)
        return 0

    print(f"[EOD] {stamp} {mtc:.0f}m to close — FLATTENING {len(positions)} position(s)",
          flush=True)
    # Cancel resting bracket legs FIRST. A held stop/limit reserves the shares, so a close
    # submitted underneath one is rejected as insufficient quantity.
    try:
        client.cancel_orders()
    except Exception as e:
        print(f"[EOD] ⚠️ could not cancel open orders: {e}", flush=True)

    failed = 0
    for p in positions:
        try:
            client.close_position(p.symbol)
            print(f"[EOD]   ✅ closed {p.symbol} qty={p.qty} uPL={p.unrealized_pl}", flush=True)
        except Exception as e:
            failed += 1
            print(f"[EOD]   ❌ {p.symbol} FAILED to close: {e}", flush=True)
    if failed:
        print(f"[EOD] ⚠️ {failed} position(s) still open — they will carry overnight", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

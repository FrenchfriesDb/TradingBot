"""
Real-time Trading Bot Monitor
Shows live status of Alpaca stock trades + crypto paper trades.
"""

import os
import json
from datetime import datetime, timezone
from alpaca.trading.client import TradingClient
from config import API_KEY, API_SECRET, PAPER_TRADING

CRYPTO_STATE_FILE = "crypto_state.json"


def get_alpaca_status():
    try:
        api       = TradingClient(api_key=API_KEY, secret_key=API_SECRET, paper=PAPER_TRADING)
        account   = api.get_account()
        positions = api.get_all_positions()
        return {
            "account": {
                "cash":            float(account.cash),
                "portfolio_value": float(account.portfolio_value),
                "buying_power":    float(account.buying_power),
                "equity":          float(account.equity),
                "status":          account.status,
            },
            "positions": [
                {
                    "symbol":         p.symbol,
                    "qty":            float(p.qty),
                    "avg_fill_price": float(p.avg_entry_price),
                    "current_price":  float(p.current_price),
                    "market_value":   float(p.market_value),
                    "unrealized_pl":  float(p.unrealized_pl),
                    "unrealized_plpc": float(p.unrealized_plpc),
                }
                for p in positions
            ],
        }
    except Exception as e:
        print(f"❌ Error fetching Alpaca portfolio: {e}")
        return None


def load_crypto_state():
    try:
        if not os.path.exists(CRYPTO_STATE_FILE):
            return None
        with open(CRYPTO_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def display_status():
    print("\n" + "=" * 80)
    print("🤖 DEBBIE-LA BOT - REAL-TIME MONITOR")
    print(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    # ── Alpaca stock account ───────────────────────────────────────────────────
    status = get_alpaca_status()

    if status is None:
        print("❌ Could not connect to Alpaca API")
    else:
        account = status["account"]
        print(f"\n📊 ALPACA STOCKS (tradingbot.py)")
        print(f"   Account Status:    {account['status'].upper()}")
        print(f"   Portfolio Value:   ${account['portfolio_value']:,.2f}")
        print(f"   Cash Available:    ${account['cash']:,.2f}")
        print(f"   Buying Power:      ${account['buying_power']:,.2f}")
        print(f"   Total Equity:      ${account['equity']:,.2f}")

        positions = status["positions"]
        if positions:
            print(f"\n📈 OPEN STOCK POSITIONS ({len(positions)})")
            for p in positions:
                icon = "🟢" if p["unrealized_pl"] >= 0 else "🔴"
                print(f"\n   {p['symbol']}")
                print(f"      Quantity:        {p['qty']}")
                print(f"      Avg Entry:       ${p['avg_fill_price']:.2f}")
                print(f"      Current Price:   ${p['current_price']:.2f}")
                print(f"      Market Value:    ${p['market_value']:,.2f}")
                print(f"      {icon} Unrealized P&L: ${p['unrealized_pl']:,.2f} ({p['unrealized_plpc']*100:+.2f}%)")
        else:
            print(f"\n📈 OPEN STOCK POSITIONS")
            print(f"   None (waiting for setup...)")

    # ── Crypto paper trades ────────────────────────────────────────────────────
    crypto = load_crypto_state()
    print(f"\n🪙  CRYPTO PAPER TRADES (binance_bot.py)")

    if crypto is None:
        print("   binance_bot.py not running (no state file found)")
    else:
        updated  = crypto.get("last_updated", "?")
        balance  = crypto.get("balance", 0.0)
        start    = crypto.get("start_balance", 10_000.0)
        trades   = crypto.get("trade_count", 0)
        pnl_total = balance - start
        pnl_pct   = (pnl_total / start) * 100

        # Age of state file
        try:
            age_s = (datetime.now(timezone.utc) -
                     datetime.fromisoformat(updated)).total_seconds()
            age_str = f"{int(age_s // 60)}m {int(age_s % 60)}s ago"
        except Exception:
            age_str = updated

        icon = "🟢" if pnl_total >= 0 else "🔴"
        print(f"   Last updated:      {age_str}")
        print(f"   Paper Balance:     ${balance:,.2f}  {icon} ({pnl_pct:+.2f}% vs ${start:,.0f} start)")
        print(f"   Total Trades:      {trades}")

        # State machine
        sm = crypto.get("state_machine", {})
        if sm:
            print(f"\n   State Machine:")
            for sym, state in sm.items():
                base = sym.split("/")[0]
                icon = "🔵" if state == "POSITION_OPEN" else "🟡" if state == "ENTRY_WAIT" else "⚪"
                print(f"      {icon} {base:<6} {state}")

        # Open positions
        positions = crypto.get("positions", {})
        if positions:
            print(f"\n   Open Crypto Positions ({len(positions)}):")
            total_upnl = 0.0
            for sym, p in positions.items():
                base  = sym.split("/")[0]
                upnl  = p.get("unrealized_pnl", 0.0)
                total_upnl += upnl
                side  = p.get("side", "?")
                entry = p.get("entry_price", 0.0)
                cur   = p.get("current_price", 0.0)
                qty   = abs(p.get("qty", 0.0))
                icon  = "🟢" if upnl >= 0 else "🔴"
                print(f"\n      {base}  {side}")
                print(f"         Qty:        {qty:.6f}")
                print(f"         Entry:      ${entry:,.2f}")
                print(f"         Current:    ${cur:,.2f}")
                print(f"         {icon} Unrealized: ${upnl:+,.2f}")
            icon = "🟢" if total_upnl >= 0 else "🔴"
            print(f"\n      {icon} Total unrealized P&L: ${total_upnl:+,.2f}")
        else:
            print(f"\n   Open Crypto Positions: None")

    # ── Log tail ───────────────────────────────────────────────────────────────
    log_file = "./logs/bot_activity.log"
    if os.path.exists(log_file):
        print(f"\n📝 LATEST BOT ACTIVITY")
        with open(log_file) as f:
            lines = f.readlines()
        for line in lines[-5:]:
            print(f"   {line.strip()}")

    print("\n" + "=" * 80)
    print("💡 Refresh this monitor every minute to track bot activity")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    display_status()

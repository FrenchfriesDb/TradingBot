"""
Real-time Trading Bot Monitor
Shows live status of bot trades, portfolio, and strategy performance
"""

import os
import json
from datetime import datetime
from alpaca.trading.client import TradingClient
from config import API_KEY, API_SECRET, PAPER_TRADING, SYMBOL

def get_portfolio_status():
    """Fetch current portfolio status from Alpaca"""
    try:
        api = TradingClient(api_key=API_KEY, secret_key=API_SECRET, paper=PAPER_TRADING)

        # Get account
        account = api.get_account()

        # Get positions
        positions = api.get_all_positions()
        
        # Get today's trades
        today_date = datetime.now().strftime("%Y-%m-%d")
        
        return {
            "account": {
                "cash": float(account.cash),
                "portfolio_value": float(account.portfolio_value),
                "buying_power": float(account.buying_power),
                "equity": float(account.equity),
                "status": account.status
            },
            "positions": [
                {
                    "symbol": p.symbol,
                    "qty": float(p.qty),
                    "avg_fill_price": float(p.avg_entry_price),
                    "current_price": float(p.current_price),
                    "market_value": float(p.market_value),
                    "unrealized_pl": float(p.unrealized_pl),
                    "unrealized_plpc": float(p.unrealized_plpc)
                }
                for p in positions
            ]
        }
    except Exception as e:
        print(f"❌ Error fetching portfolio: {e}")
        return None

def display_status():
    """Display formatted status"""
    print("\n" + "="*80)
    print("🤖 DEBBIE-LA BOT - REAL-TIME MONITOR")
    print(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*80)
    
    status = get_portfolio_status()
    
    if status is None:
        print("❌ Could not connect to Alpaca API")
        return
    
    # Account Summary
    account = status["account"]
    print(f"\n📊 ACCOUNT STATUS")
    print(f"   Account Status:    {account['status'].upper()}")
    print(f"   Portfolio Value:   ${account['portfolio_value']:,.2f}")
    print(f"   Cash Available:    ${account['cash']:,.2f}")
    print(f"   Buying Power:      ${account['buying_power']:,.2f}")
    print(f"   Total Equity:      ${account['equity']:,.2f}")
    
    # Positions
    positions = status["positions"]
    if positions:
        print(f"\n📈 OPEN POSITIONS ({len(positions)})")
        for p in positions:
            pl_color = "🟢" if p["unrealized_pl"] >= 0 else "🔴"
            print(f"\n   {p['symbol']}")
            print(f"      Quantity:        {p['qty']}")
            print(f"      Avg Entry:       ${p['avg_fill_price']:.2f}")
            print(f"      Current Price:   ${p['current_price']:.2f}")
            print(f"      Market Value:    ${p['market_value']:,.2f}")
            print(f"      {pl_color} Unrealized P&L: ${p['unrealized_pl']:,.2f} ({p['unrealized_plpc']*100:+.2f}%)")
    else:
        print(f"\n📈 OPEN POSITIONS")
        print(f"   None (waiting for setup...)")
    
    # Check logs for latest activity
    log_file = "./logs/bot_activity.log"
    if os.path.exists(log_file):
        print(f"\n📝 LATEST BOT ACTIVITY")
        with open(log_file, 'r') as f:
            lines = f.readlines()
            # Show last 5 lines
            for line in lines[-5:]:
                print(f"   {line.strip()}")
    
    print("\n" + "="*80)
    print("💡 Refresh this monitor every minute to track bot activity")
    print("="*80 + "\n")

if __name__ == "__main__":
    display_status()

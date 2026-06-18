"""
Live trading dashboard — reads crypto_state.json (binance_bot) and
test_state.json (test_bot) and shows a refreshing terminal UI.
Run: python3 dashboard.py
"""

import json
import os
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich.live import Live
from rich.layout import Layout
from rich import box

CRYPTO_STATE = "crypto_state.json"
TEST_STATE   = "test_state.json"
REFRESH_SEC  = 5


def load(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def pct(current, reference):
    if not reference or reference == 0:
        return 0.0
    return (current - reference) / reference * 100


def dist_str(current, level, label):
    if level is None:
        return "—"
    d = pct(level, current)
    color = "red" if label == "SL" else "green"
    return f"[{color}]{d:+.2f}%[/{color}]"


def rr_str(entry, sl, tp, is_long):
    if not sl or not tp or not entry:
        return "—"
    risk   = abs(entry - sl)
    reward = abs(tp - entry)
    if risk == 0:
        return "—"
    rr = reward / risk
    return f"1:{rr:.1f}"


def build_positions_table(data, bot_name):
    """Build a Rich table from a bot's state JSON."""
    if not data:
        return Panel(f"[dim]{bot_name} — no data (bot not running?)[/dim]",
                     title=bot_name, border_style="dim")

    balance       = data.get("balance", 0)
    start_balance = data.get("start_balance", balance)
    trade_count   = data.get("trade_count", 0)
    updated_raw   = data.get("last_updated", "")
    positions     = data.get("positions", {})

    # Parse last updated time
    try:
        updated_dt = datetime.fromisoformat(updated_raw).replace(tzinfo=timezone.utc)
        age_sec    = (datetime.now(timezone.utc) - updated_dt).total_seconds()
        age_str    = f"{int(age_sec)}s ago"
        stale      = age_sec > 120
    except Exception:
        age_str = "unknown"
        stale   = False

    total_pnl    = balance - start_balance
    pnl_color    = "green" if total_pnl >= 0 else "red"
    stale_warn   = "  [yellow]⚠ STALE[/yellow]" if stale else ""

    header = (
        f"[bold]{bot_name}[/bold]  |  "
        f"Balance: [cyan]${balance:,.2f}[/cyan]  |  "
        f"Total P&L: [{pnl_color}]{total_pnl:+.2f}[/{pnl_color}]  |  "
        f"Trades: {trade_count}  |  "
        f"Updated: [dim]{age_str}[/dim]{stale_warn}"
    )

    if not positions:
        return Panel(
            Text.from_markup(header + "\n\n[dim]  No open positions[/dim]"),
            border_style="cyan"
        )

    tbl = Table(
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold white",
        expand=True,
    )
    tbl.add_column("Symbol", style="bold", width=6)
    tbl.add_column("Side",   width=6)
    tbl.add_column("Entry",  justify="right", width=12)
    tbl.add_column("Price",  justify="right", width=12)
    tbl.add_column("SL",     justify="right", width=12)
    tbl.add_column("TP",     justify="right", width=12)
    tbl.add_column("→ SL",   justify="right", width=8)
    tbl.add_column("→ TP",   justify="right", width=8)
    tbl.add_column("R:R",    justify="center", width=7)
    tbl.add_column("Unreal. P&L", justify="right", width=12)

    for sym, pos in positions.items():
        base    = sym.split("/")[0]
        side    = pos.get("side", "?")
        entry   = pos.get("entry_price")
        current = pos.get("current_price")
        sl      = pos.get("stop_loss")
        tp      = pos.get("take_profit")
        upnl    = pos.get("unrealized_pnl", 0)
        is_long = side == "LONG"

        side_txt   = f"[green]▲ LONG[/green]" if is_long else f"[red]▼ SHORT[/red]"
        upnl_color = "green" if upnl >= 0 else "red"
        upnl_txt   = f"[{upnl_color}]{upnl:+.2f}[/{upnl_color}]"

        # Price movement bar (mini visual)
        if entry and current:
            move_pct = pct(current, entry)
            move_color = "green" if (is_long and move_pct > 0) or (not is_long and move_pct < 0) else "red"
            price_txt = f"[{move_color}]${current:,.4f}[/{move_color}] ({move_pct:+.2f}%)"
        else:
            price_txt = f"${current:,.4f}" if current else "—"

        tbl.add_row(
            base,
            Text.from_markup(side_txt),
            f"${entry:,.4f}" if entry else "—",
            Text.from_markup(price_txt),
            f"${sl:,.4f}"  if sl else "—",
            f"${tp:,.4f}"  if tp else "—",
            Text.from_markup(dist_str(current, sl, "SL")) if current else Text("—"),
            Text.from_markup(dist_str(current, tp, "TP")) if current else Text("—"),
            rr_str(entry, sl, tp, is_long),
            Text.from_markup(upnl_txt),
        )

    inner = Table.grid(padding=(0, 0))
    inner.add_row(Text.from_markup(header))
    inner.add_row(tbl)
    return Panel(inner, border_style="cyan" if not stale else "yellow")


def build_dashboard():
    smc_data  = load(CRYPTO_STATE)
    test_data = load(TEST_STATE)
    now       = datetime.now(timezone.utc).strftime("%Y-%m-%d  %H:%M:%S UTC")

    title = Panel(
        f"[bold white]  DEBBIE-LA LIVE DASHBOARD[/bold white]   "
        f"[dim]{now}  |  refreshes every {REFRESH_SEC}s  |  Ctrl+C to exit[/dim]",
        style="on dark_blue",
        padding=(0, 1),
    )

    smc_panel  = build_positions_table(smc_data,  "binance_bot  (SMC strategy)")
    test_panel = build_positions_table(test_data, "test_bot  (EMA 9/21 crossover)")

    layout = Layout()
    layout.split_column(
        Layout(title,      size=3),
        Layout(smc_panel,  name="smc",  ratio=1),
        Layout(test_panel, name="test", ratio=1),
    )
    return layout


def main():
    console = Console()
    console.print("\n[bold cyan]Starting dashboard...[/bold cyan]  "
                  "(run binance_bot.py and test_bot.py in separate terminals)\n")

    with Live(build_dashboard(), console=console, refresh_per_second=0.5,
              screen=True) as live:
        while True:
            time.sleep(REFRESH_SEC)
            live.update(build_dashboard())


if __name__ == "__main__":
    main()

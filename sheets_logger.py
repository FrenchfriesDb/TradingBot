"""
Shared Google Sheets logging for binance_bot.py and bot/strategy.py — daily portfolio
snapshots and completed-trade ledger rows. Every public function fails soft: on any
error (missing credentials, network, bad sheet URL) it prints a warning and returns
without raising, so a Sheets outage can never block or crash a trading loop.

Per-trade chart screenshots are hosted on GitHub (see github_chart_uploader.py), not
Drive — service accounts have zero storage quota of their own and can't accept
ownership transfers (no human to click Accept), so Drive hosting isn't viable here.

One-time setup (see docs/superpowers/specs/2026-07-12-google-sheets-trade-logging-design.md):
  1. Create a Google Cloud project, enable the Google Sheets API for it.
  2. Create a service account, download its JSON key as google_credentials.json
     in the project root (gitignored — never commit this file).
  3. Share the target Google Sheet with the service account's email (found inside
     the JSON key as "client_email") as an Editor.
  4. Set GOOGLE_SHEET_URL in .env to the sheet's URL.
"""

from config import GOOGLE_SHEETS_CREDS_FILE, GOOGLE_SHEET_URL

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

MACRO_HEADER = ["Date", "Balance", "Daily Return %", "Benchmark Price", "Benchmark Daily Return %"]
LEDGER_HEADER = [
    "Entry Time", "Exit Time", "Ticker", "Side", "Entry", "Stop Loss", "Take Profit", "Exit",
    "Size", "Margin Invested ($)", "Notional Value ($)", "Leverage", "P&L", "Reason", "Chart",
]

_client_cache = None
_sheet_cache  = {}   # spreadsheet_url -> gspread.Spreadsheet


def get_sheet_client():
    """Lazy singleton: authenticates on first call, caches the client. Returns None
    (never raises) on any failure — missing file, invalid JSON, network error. Every
    caller must treat None as "logging unavailable this run" and continue trading."""
    global _client_cache
    if _client_cache is not None:
        return _client_cache
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(GOOGLE_SHEETS_CREDS_FILE, scopes=_SCOPES)
        _client_cache = gspread.authorize(creds)
        return _client_cache
    except Exception as e:
        print(f"[SHEETS] Auth failed (credentials file: {GOOGLE_SHEETS_CREDS_FILE}): {e}")
        return None


def _get_spreadsheet(client, spreadsheet_url):
    """Opens (and caches) the spreadsheet by URL. Returns None on any failure."""
    if not spreadsheet_url:
        print("[SHEETS] GOOGLE_SHEET_URL is not set — skipping.")
        return None
    if spreadsheet_url in _sheet_cache:
        return _sheet_cache[spreadsheet_url]
    try:
        sh = client.open_by_url(spreadsheet_url)
        _sheet_cache[spreadsheet_url] = sh
        return sh
    except Exception as e:
        print(f"[SHEETS] Could not open spreadsheet: {e}")
        return None


def ensure_tabs(client, spreadsheet_url, tab_specs: dict):
    """tab_specs maps tab name -> header row, e.g. {"Crypto Macro": MACRO_HEADER}.
    Creates any tab that doesn't exist yet with its header row. Idempotent — safe to
    call every time a bot starts. Returns the gspread.Spreadsheet on success, None on
    any failure (including client being None already)."""
    if client is None:
        return None
    sh = _get_spreadsheet(client, spreadsheet_url)
    if sh is None:
        return None
    try:
        existing = {ws.title for ws in sh.worksheets()}
        for tab_name, header in tab_specs.items():
            if tab_name not in existing:
                ws = sh.add_worksheet(title=tab_name, rows=1000, cols=max(len(header), 1))
                ws.append_row(header)
                print(f"[SHEETS] Created tab '{tab_name}'")
        return sh
    except Exception as e:
        print(f"[SHEETS] ensure_tabs failed: {e}")
        return None


def build_macro_row(date_str, balance, daily_return_pct, bench_price, bench_return_pct):
    """Pure row-building for the Macro tabs — no network call, unit-testable in isolation."""
    return [date_str, round(balance, 2), round(daily_return_pct, 3),
            round(bench_price, 4), round(bench_return_pct, 3)]


def build_ledger_row(entry_time_str, exit_time_str, ticker, side, entry_price, stop_loss,
                      take_profit, exit_price, size, margin, notional, leverage, pnl, reason,
                      chart_url=None):
    """Pure row-building for the Ledger tabs — no network call, unit-testable in isolation.
    An http(s) chart_url becomes a clickable Sheets image — =HYPERLINK(url, IMAGE(url))
    renders the thumbnail in-cell AND opens the full-resolution PNG on click, since the
    in-cell rendering is downscaled to cell size (row must be appended with
    value_input_option="USER_ENTERED" — see log_trade — for it to render instead of
    showing as literal text). Anything else (e.g. a local charts/ file path) is written
    as plain text — Sheets can't fetch a local path, so wrapping it in IMAGE() would
    just show a broken-image error instead of a usable reference."""
    if chart_url and chart_url.startswith(("http://", "https://")):
        chart_cell = f'=HYPERLINK("{chart_url}", IMAGE("{chart_url}"))'
    else:
        chart_cell = chart_url or ""
    return [entry_time_str, exit_time_str, ticker, side, round(entry_price, 6),
            round(stop_loss, 6), round(take_profit, 6), round(exit_price, 6),
            round(size, 6), round(margin, 2), round(notional, 2), leverage,
            round(pnl, 2), reason, chart_cell]


def log_daily_snapshot(client, spreadsheet_url, tab_name, date_str,
                        balance, daily_return_pct, bench_price, bench_return_pct):
    """Appends one row to a Macro tab. Never raises — logs a warning and returns on
    any failure (client is None, sheet unreachable, tab missing, network error)."""
    if client is None:
        return
    try:
        sh = _get_spreadsheet(client, spreadsheet_url)
        if sh is None:
            return
        ws = sh.worksheet(tab_name)
        ws.append_row(build_macro_row(date_str, balance, daily_return_pct,
                                       bench_price, bench_return_pct))
    except Exception as e:
        print(f"[SHEETS] log_daily_snapshot to '{tab_name}' failed: {e}")


def log_trade(client, spreadsheet_url, tab_name, entry_time_str, exit_time_str, ticker, side,
              entry_price, stop_loss, take_profit, exit_price, size, margin, notional,
              leverage, pnl, reason, chart_url=None):
    """Appends one row to a Ledger tab. Never raises — logs a warning and returns on
    any failure (client is None, sheet unreachable, tab missing, network error)."""
    if client is None:
        return
    try:
        sh = _get_spreadsheet(client, spreadsheet_url)
        if sh is None:
            return
        ws = sh.worksheet(tab_name)
        # Duplicate guard: a restart landing between a close and its state save can
        # replay the same close (seen live — one AVAX exit logged twice, 2 min apart).
        # Entry Time + Ticker uniquely identify one position lifecycle, so if a recent
        # row already carries this pair, the close was already recorded.
        try:
            for r in ws.get_all_values()[-10:]:
                if len(r) >= 3 and r[0] == entry_time_str and r[2] == ticker:
                    print(f"[SHEETS] Skipping duplicate {ticker} trade row (entry {entry_time_str})")
                    return
        except Exception:
            pass
        row = build_ledger_row(entry_time_str, exit_time_str, ticker, side, entry_price,
                                stop_loss, take_profit, exit_price, size, margin, notional,
                                leverage, pnl, reason, chart_url)
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        print(f"[SHEETS] log_trade to '{tab_name}' failed: {e}")

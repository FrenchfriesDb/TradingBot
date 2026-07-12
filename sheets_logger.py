"""
Shared Google Sheets logging for binance_bot.py and bot/strategy.py — daily portfolio
snapshots and completed-trade ledger rows. Every public function fails soft: on any
error (missing credentials, network, bad sheet URL) it prints a warning and returns
without raising, so a Sheets outage can never block or crash a trading loop.

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

MACRO_HEADER  = ["Date", "Balance", "Daily Return %", "Benchmark Price", "Benchmark Daily Return %"]
LEDGER_HEADER = ["Timestamp", "Ticker", "Side", "Entry", "Exit", "Size", "P&L", "Reason"]

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


def build_ledger_row(timestamp_str, ticker, side, entry_price, exit_price, size, pnl, reason):
    """Pure row-building for the Ledger tabs — no network call, unit-testable in isolation."""
    return [timestamp_str, ticker, side, round(entry_price, 6),
            round(exit_price, 6), round(size, 6), round(pnl, 2), reason]


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


def log_trade(client, spreadsheet_url, tab_name, timestamp_str, ticker, side,
              entry_price, exit_price, size, pnl, reason):
    """Appends one row to a Ledger tab. Never raises — logs a warning and returns on
    any failure (client is None, sheet unreachable, tab missing, network error)."""
    if client is None:
        return
    try:
        sh = _get_spreadsheet(client, spreadsheet_url)
        if sh is None:
            return
        ws = sh.worksheet(tab_name)
        ws.append_row(build_ledger_row(timestamp_str, ticker, side, entry_price,
                                        exit_price, size, pnl, reason))
    except Exception as e:
        print(f"[SHEETS] log_trade to '{tab_name}' failed: {e}")

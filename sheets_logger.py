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
    # Fees is APPENDED, not inserted next to P&L where it reads better, because ~177 rows
    # of history already have their chart in column O. Inserting ahead of Chart would put
    # every new row's chart one column right of every old one. P&L became NET of fees on
    # 2026-09-04; rows written before that date are GROSS and overstate by roughly this
    # column's value.
    "Fees ($)",
    # Likewise appended. Distinct from "Reason" above, which records the strategy that
    # OPENED the trade ("BOS LONG"); this records why it CLOSED. Canonical tokens only
    # (TARGET / STOP / BREAKEVEN / STALE / OTHER) so the column can be grouped — the
    # BREAKEVEN vs STOP split is the one that shows where the money actually goes.
    "Exit Reason",
    # APPENDED for the same reason Fees and Exit Reason were: ~177 rows already carry
    # their chart in column O, and inserting ahead of it would shift every new row's
    # chart one column right of every old one. missing_header_cells backfills the label
    # on the live tab.
    # Duplicates the middle field of "Reason" on purpose: that column is a human-readable
    # sentence, this one is a single token so the sheet can GROUP by structure and answer
    # "do breakers actually work?" without parsing strings.
    "Zone Type",
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


def _col_letter(idx):
    """0-based column index -> A1 letter. 0->A, 25->Z, 26->AA."""
    idx = int(idx)
    out = ""
    while True:
        idx, rem = divmod(idx, 26)
        out = chr(65 + rem) + out
        if idx == 0:
            break
        idx -= 1
    return out


def missing_header_cells(existing_header, header):
    """Which trailing header labels a live tab is missing — (a1_range, [values]) or None.

    ensure_tabs only ever CREATES tabs, so a column appended to LEDGER_HEADER after a tab
    already exists leaves the live sheet writing data under a blank heading. That happened
    twice on 2026-09-04 (Fees, then Exit Reason) against a ledger with ~177 rows.

    Only ever fills cells PAST the end of the existing header. Never rewrites a label that
    is already there — someone may have renamed a heading by hand, and clobbering that
    would be a destructive surprise for a cosmetic gain.
    """
    existing_header = list(existing_header or [])
    if len(existing_header) >= len(header):
        return None
    start = len(existing_header)
    rng = f"{_col_letter(start)}1:{_col_letter(len(header) - 1)}1"
    return rng, [header[start:]]


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
            else:
                # Backfill headings for columns appended since this tab was created,
                # so new data never lands under a blank heading. Fill-only — see
                # missing_header_cells. Wrapped: a header cosmetic must never stop a
                # bot from starting.
                try:
                    ws = sh.worksheet(tab_name)
                    todo = missing_header_cells(ws.row_values(1), header)
                    if todo:
                        rng, values = todo
                        if ws.col_count < len(header):
                            ws.add_cols(len(header) - ws.col_count)
                        ws.update(rng, values)
                        print(f"[SHEETS] Extended '{tab_name}' header: {', '.join(values[0])}")
                except Exception as e:
                    print(f"[SHEETS] Could not extend '{tab_name}' header: {e}")
        return sh
    except Exception as e:
        print(f"[SHEETS] ensure_tabs failed: {e}")
        return None


def build_macro_row(date_str, balance, daily_return_pct, bench_price, bench_return_pct):
    """Pure row-building for the Macro tabs — no network call, unit-testable in isolation."""
    return [date_str, round(balance, 2), round(daily_return_pct, 3),
            round(bench_price, 4), round(bench_return_pct, 3)]


def missing_snapshot_dates(last_snapshot_date, today, max_backfill=30):
    """Days between the last Macro row and today that never got one, oldest first.

    The daily snapshot only fires when a RUNNING bot notices a day rollover, so any day
    the bot was down/restarting across midnight silently gets no row — and the next start
    writes only the current day, swallowing the gap. REAL INCIDENT 2026-08-15: Crypto
    Macro jumped Aug 13 -> Aug 15, leaving a hole in the Quant Desk equity curve.

    Returns [] when there's nothing to fill, when there's no prior snapshot (inventing
    history backwards would be fabricating data), or when the clock moved backwards.
    Bounded by max_backfill so a months-long gap can't spam thousands of rows."""
    if last_snapshot_date is None or today is None:
        return []
    if today <= last_snapshot_date:
        return []
    from datetime import timedelta
    gap = (today - last_snapshot_date).days - 1
    if gap <= 0:
        return []
    gap = min(gap, max_backfill)
    # Anchor to `today` so a bounded window keeps the MOST RECENT missing days — the ones
    # adjacent to live data — rather than the oldest, least useful ones.
    return [today - timedelta(days=n) for n in range(gap, 0, -1)]


def build_ledger_row(entry_time_str, exit_time_str, ticker, side, entry_price, stop_loss,
                      take_profit, exit_price, size, margin, notional, leverage, pnl, reason,
                      chart_url=None, fees=0.0, exit_reason="", zone_type=""):
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
            round(pnl, 2), reason, chart_cell, round(fees or 0.0, 2),
            exit_reason or "", zone_type or ""]


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
              leverage, pnl, reason, chart_url=None, fees=0.0, exit_reason="",
              zone_type=""):
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
                                leverage, pnl, reason, chart_url, fees=fees,
                                exit_reason=exit_reason, zone_type=zone_type)
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        print(f"[SHEETS] log_trade to '{tab_name}' failed: {e}")

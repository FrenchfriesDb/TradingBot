"""Macro-tab date parsing for the Quant Desk equity curve.

REAL INCIDENT 2026-08-15: the stock account's equity curve showed a dead-flat line and
"1d total" on the ALL range, despite 26 daily rows sitting in the Stock Macro tab and a
Performance Calendar full of trades. Cause: the two Macro tabs carry DIFFERENT date
formats, and only the crypto one was covered —

    Crypto Macro: 'Tuesday, August 11, 2026 at 12:00:00 AM'   -> parsed
    Stock Macro:  'Tue, Aug 11, 2026'                         -> None

_parse_sheet_date returns None for unparseable cells and the caller SKIPS those rows, so
every stock row was discarded silently. The chart then had nothing but today's live
equity point to draw. A silent skip is the right call for one corrupt cell and the wrong
one for an entire tab, so these tests pin every format the sheets actually produce.
"""
import pytest

from chart_server import _parse_sheet_date


@pytest.mark.parametrize("raw,expected", [
    # What log_daily_snapshot writes itself.
    ("2026-08-11", "2026-08-11"),
    # Sheets' auto long format — real Crypto Macro cell (note the narrow no-break
    # space before AM, U+202F, which Sheets inserts and strptime won't match raw).
    ("Tuesday, August 11, 2026 at 12:00:00 AM", "2026-08-11"),
    # Real Stock Macro cells — the format that was silently dropping every row.
    ("Tue, Aug 11, 2026", "2026-08-11"),
    ("Fri, Aug 14, 2026", "2026-08-14"),
    ("Sat, Aug 15, 2026", "2026-08-15"),
    # Defensive: other shapes Sheets emits depending on locale/column formatting.
    ("August 11, 2026", "2026-08-11"),
    ("Aug 11, 2026", "2026-08-11"),
    ("Tuesday, August 11, 2026", "2026-08-11"),
    ("08/11/2026", "2026-08-11"),
])
def test_parses_every_format_the_sheets_actually_produce(raw, expected):
    assert _parse_sheet_date(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "   ", "not a date", "13/45/2026"])
def test_unparseable_cells_return_none_so_the_caller_can_skip_them(raw):
    assert _parse_sheet_date(raw) is None


def test_every_real_stock_macro_row_survives_parsing():
    """The regression that mattered: a whole tab silently reduced to zero usable rows."""
    rows = ["Mon, Jul 21, 2026", "Tue, Aug 11, 2026", "Wed, Aug 12, 2026",
            "Thu, Aug 13, 2026", "Fri, Aug 14, 2026"]
    parsed = [_parse_sheet_date(r) for r in rows]
    assert all(p is not None for p in parsed), f"dropped rows: {list(zip(rows, parsed))}"
    assert parsed == ["2026-07-21", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]

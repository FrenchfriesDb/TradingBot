"""Mark-to-market account equity for the paper margin account. `balance` is only FREE
CASH (margin is deducted on open), so both the live header AND the Macro snapshot must
report cash + Σ(locked margin + unrealized P&L) — otherwise opening a position makes the
portfolio value drop as if money vanished (the journal bug)."""
import pytest

from bot.indicators import account_equity


def test_no_positions_equity_is_free_cash():
    eq = account_equity(balance=10000, positions={}, entry_prices={},
                        margin_used={}, prices={}, leverage=10)
    assert eq == pytest.approx(10000)


def test_long_position_adds_locked_margin_and_unrealized_profit():
    # opened 0.03 BTC @65000 -> $195 margin locked (10x); cash already 10000-195=9805.
    # price now 66000 -> uPnL = (66000-65000)*0.03 = +30. equity = 9805+195+30 = 10030
    eq = account_equity(balance=9805, positions={"BTC/USD": 0.03},
                        entry_prices={"BTC/USD": 65000}, margin_used={"BTC/USD": 195},
                        prices={"BTC/USD": 66000}, leverage=10)
    assert eq == pytest.approx(10030)


def test_short_position_adds_margin_minus_unrealized_loss():
    # short 0.03 BTC @65000, price up to 66000 -> uPnL = (65000-66000)*0.03 = -30
    eq = account_equity(balance=9805, positions={"BTC/USD": -0.03},
                        entry_prices={"BTC/USD": 65000}, margin_used={"BTC/USD": 195},
                        prices={"BTC/USD": 66000}, leverage=10)
    assert eq == pytest.approx(9970)


def test_missing_price_falls_back_to_entry_zero_upnl():
    eq = account_equity(balance=9805, positions={"BTC/USD": 0.03},
                        entry_prices={"BTC/USD": 65000}, margin_used={"BTC/USD": 195},
                        prices={}, leverage=10)
    assert eq == pytest.approx(10000)   # 9805 + 195 margin + 0 uPnL


def test_margin_fallback_computed_when_not_tracked():
    # no margin_used entry -> derive from notional/leverage: 0.03*65000/10 = 195
    eq = account_equity(balance=9805, positions={"BTC/USD": 0.03},
                        entry_prices={"BTC/USD": 65000}, margin_used={},
                        prices={"BTC/USD": 65000}, leverage=10)
    assert eq == pytest.approx(10000)


def test_flat_dust_position_ignored():
    eq = account_equity(balance=10000, positions={"BTC/USD": 1e-12},
                        entry_prices={"BTC/USD": 65000}, margin_used={},
                        prices={"BTC/USD": 65000}, leverage=10)
    assert eq == pytest.approx(10000)


# ── Missing daily-snapshot backfill ───────────────────────────────────────────
# REAL INCIDENT 2026-08-15: Crypto Macro jumped Aug 13 -> Aug 15 with no Aug 14 row.
# The snapshot only fires when a running bot NOTICES a day rollover
# (`if _daily_snap["date"] != _today`). If the bot is down/restarting across midnight,
# that day never gets a row, and on the next start it writes only the CURRENT day —
# silently swallowing the gap. That leaves a hole in the Quant Desk equity curve.
from datetime import date

from sheets_logger import missing_snapshot_dates


def test_no_backfill_needed_on_a_normal_consecutive_day():
    assert missing_snapshot_dates(date(2026, 8, 14), date(2026, 8, 15)) == []


def test_backfills_a_single_skipped_day():
    # the real gap: last row Aug 13, bot back up Aug 15 -> Aug 14 must be filled
    assert missing_snapshot_dates(date(2026, 8, 13), date(2026, 8, 15)) == [date(2026, 8, 14)]


def test_backfills_a_multi_day_outage_in_order():
    assert missing_snapshot_dates(date(2026, 8, 10), date(2026, 8, 15)) == [
        date(2026, 8, 11), date(2026, 8, 12), date(2026, 8, 13), date(2026, 8, 14)]


def test_same_day_needs_nothing():
    assert missing_snapshot_dates(date(2026, 8, 15), date(2026, 8, 15)) == []


def test_no_previous_snapshot_backfills_nothing():
    # first run ever — inventing history backwards would be fabricating data
    assert missing_snapshot_dates(None, date(2026, 8, 15)) == []


def test_clock_moving_backwards_backfills_nothing():
    assert missing_snapshot_dates(date(2026, 8, 15), date(2026, 8, 13)) == []


def test_absurd_gap_is_bounded_rather_than_writing_thousands_of_rows():
    out = missing_snapshot_dates(date(2020, 1, 1), date(2026, 8, 15))
    assert len(out) <= 30, "a months-long gap must not spam the sheet"
    assert out[-1] == date(2026, 8, 14)

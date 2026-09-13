"""The P&L written to the ledger must be NET of trading costs.

THE BUG (found 2026-09-04). All three close paths in binance_bot.py computed:

    pnl = (price - state.entry_price) * qty_now

which is the raw price difference. Fees WERE charged — PaperTrader._charge_fee deducts
them from `balance` on all four legs — but they were never subtracted from the `pnl`
handed to the ledger, the journal tiles, or the desktop alert. So the sheet reported
gross while the balance moved by net, and every row overstated the trade by the full
round trip (0.50% of notional at the default 0.25%/side).

At 10x leverage this is not a rounding error. These four real rows were on screen when
the operator asked "we like made no money":

    POL   LONG   notional $1,359.94   ledger +$3.16   fees $6.80   ACTUALLY -$3.64
    DRIFT SHORT  notional $1,480.11   ledger -$11.09  fees $7.40   ACTUALLY -$18.49
    ETH   LONG   notional   $929.71   ledger +$1.84   fees $4.65   ACTUALLY -$2.81
    XRP   LONG   notional   $727.38   ledger -$1.56   fees $3.64   ACTUALLY -$5.20

Two of the four are shown GREEN and are losses. The ledger says -$7.65 for the set; the
real figure is -$30.14. Fees were 2.9x the entire gross P&L.
"""
import pytest

from bot.indicators import round_trip_fee
from sheets_logger import LEDGER_HEADER, build_ledger_row

FEE = 0.0025      # TAKER_FEE_RATE default, per side


def _net(notional, gross, fee_rate=FEE):
    """Approximate helper mirroring the real rows: entry and exit notional are within a
    hair of each other on these near-breakeven trades."""
    return gross - notional * fee_rate * 2


# ─────────────────────────────────────────────────────────────
# round_trip_fee
# ─────────────────────────────────────────────────────────────

def test_charges_both_legs_on_notional():
    """An exchange bills notional, not margin. 100 units at $10 in and $11 out."""
    assert round_trip_fee(10.0, 11.0, 100.0, FEE) == pytest.approx(
        100 * 10 * FEE + 100 * 11 * FEE)


def test_uses_each_leg_s_own_price():
    """A big winner pays more on the exit leg than the entry leg."""
    entry_only = 100 * 10 * FEE
    exit_only  = 100 * 20 * FEE
    assert round_trip_fee(10.0, 20.0, 100.0, FEE) == pytest.approx(entry_only + exit_only)


def test_short_qty_sign_is_ignored():
    assert round_trip_fee(10.0, 9.0, -100.0, FEE) == round_trip_fee(10.0, 9.0, 100.0, FEE)


def test_zero_qty_costs_nothing():
    assert round_trip_fee(10.0, 11.0, 0.0, FEE) == 0.0


def test_zero_rate_costs_nothing():
    assert round_trip_fee(10.0, 11.0, 100.0, 0.0) == 0.0


def test_bad_input_returns_zero_rather_than_crashing_a_close():
    """A close path must never raise — a position has already been exited by then."""
    assert round_trip_fee(None, 11.0, 100.0, FEE) == 0.0


# ─────────────────────────────────────────────────────────────
# the four real rows
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,notional,gross,expected", [
    ("POL LONG",    1359.94,   3.16,  -3.64),
    ("DRIFT SHORT", 1480.11, -11.09, -18.49),
    ("ETH LONG",     929.71,   1.84,  -2.81),
    ("XRP LONG",     727.38,  -1.56,  -5.20),
])
def test_real_ledger_rows_are_losses_once_fees_land(name, notional, gross, expected):
    assert _net(notional, gross) == pytest.approx(expected, abs=0.01)


def test_two_of_those_four_were_reported_green_but_lost_money():
    green_but_losing = [(1359.94, 3.16), (929.71, 1.84)]
    for notional, gross in green_but_losing:
        assert gross > 0, "the ledger showed a profit"
        assert _net(notional, gross) < 0, "the account lost money"


def test_the_set_understates_the_loss_fourfold():
    rows = [(1359.94, 3.16), (1480.11, -11.09), (929.71, 1.84), (727.38, -1.56)]
    reported = sum(g for _, g in rows)
    real = sum(_net(n, g) for n, g in rows)
    assert reported == pytest.approx(-7.65, abs=0.01)
    assert real == pytest.approx(-30.14, abs=0.01)


# ─────────────────────────────────────────────────────────────
# the ledger row itself
# ─────────────────────────────────────────────────────────────

def test_fees_column_exists():
    assert "Fees ($)" in LEDGER_HEADER


def test_fees_is_appended_after_chart_so_177_rows_of_history_stay_aligned():
    """Chart must keep its existing column. Inserting Fees before it would put new rows'
    charts one column right of every historical row. ("Exit Reason" was appended after
    Fees later the same day, for the same reason — so Fees is no longer last, but it is
    still after Chart, which is the property that matters.)"""
    assert LEDGER_HEADER.index("Chart") == 14
    assert LEDGER_HEADER.index("Fees ($)") == 15


def test_row_carries_the_fee_and_stays_header_length():
    row = build_ledger_row("t0", "t1", "POL", "LONG", 0.09465, 0.09332, 0.10721,
                           0.09487, 14368.07672, 135.99, 1359.94, 10, -3.64,
                           "BOS LONG", None, fees=6.80)
    assert len(row) == len(LEDGER_HEADER)
    assert row[LEDGER_HEADER.index("Fees ($)")] == 6.80
    assert row[LEDGER_HEADER.index("P&L")] == -3.64


def test_fees_defaults_to_zero_for_untouched_callers():
    row = build_ledger_row("t0", "t1", "POL", "LONG", 1, 1, 1, 1, 1, 1, 1, 10, 0, "r")
    assert len(row) == len(LEDGER_HEADER)
    assert row[LEDGER_HEADER.index("Fees ($)")] == 0.0

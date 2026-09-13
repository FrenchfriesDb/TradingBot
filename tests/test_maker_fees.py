"""Maker fees have to be EARNED by resting an order, not just relabelled.

Fee drag is ~29% of the risk budget at a 1.73% stop, and it is the only lever not caught
in the stop-width / R:R / hold-time triangle. Posting limit orders moves the entry from
taker to maker.

WHICH LEGS ACTUALLY QUALIFY — this is where the naive estimate goes wrong:

    entry        resting limit at the zone edge      MAKER
    take-profit  resting limit above/below           MAKER
    stop-loss    triggers and CROSSES the book       TAKER, always

So a winner pays maker+maker, a loser pays maker+TAKER. Blended at a ~35% win rate that
is ~14% of risk, not the ~7% both-maker figure.

AND THE FILL MODEL MUST CHANGE WITH IT. Charging the maker rate while still filling at
whatever price printed would describe a cost nobody paid — the same self-flattery as
reporting P&L gross. A resting order fills AT its limit, and only if the bar traded there.
"""
import pytest

from bot.indicators import maker_limit_fill, round_trip_fee

T, M = 0.0025, 0.0006


# ── per-leg rates ──

def test_a_winner_pays_maker_on_both_legs():
    assert round_trip_fee(100.0, 110.0, 10.0, M, M) == pytest.approx(
        10 * 100 * M + 10 * 110 * M)


def test_a_loser_pays_maker_in_and_TAKER_out():
    """The stop crosses the book. It is never a maker fill."""
    assert round_trip_fee(100.0, 98.0, 10.0, M, T) == pytest.approx(
        10 * 100 * M + 10 * 98 * T)


def test_one_blended_rate_misprices_both_outcomes():
    """Charging taker on both legs overstates a winner's cost and understates nothing;
    charging maker on both understates a loser's. Hence the per-leg split."""
    loser_honest = round_trip_fee(100.0, 98.0, 10.0, M, T)
    loser_all_maker = round_trip_fee(100.0, 98.0, 10.0, M, M)
    assert loser_honest > loser_all_maker


def test_omitting_the_exit_rate_keeps_the_old_single_rate_behaviour():
    assert round_trip_fee(100.0, 110.0, 10.0, T) == round_trip_fee(100.0, 110.0, 10.0, T, T)


def test_the_measured_saving_on_a_real_stop_width():
    """1.73% stop -> 57.8x notional per $1 risked."""
    ratio = 1 / 0.0173
    today = ratio * (T + T)
    win = ratio * (M + M)
    loss = ratio * (M + T)
    assert today == pytest.approx(0.289, abs=0.002)
    assert win == pytest.approx(0.069, abs=0.002)
    assert loss == pytest.approx(0.179, abs=0.002)
    blended = 0.35 * win + 0.65 * loss
    assert blended == pytest.approx(0.141, abs=0.003)
    assert blended < today / 2


# ── the fill model that earns it ──

def test_a_buy_limit_fills_at_its_limit_when_price_trades_through():
    assert maker_limit_fill(100.0, bar_low=99.5, bar_high=101.0, is_long=True) == 100.0


def test_a_bar_that_never_reaches_the_limit_does_not_fill():
    assert maker_limit_fill(100.0, bar_low=100.5, bar_high=101.0, is_long=True) is None


def test_a_mere_touch_does_not_fill_by_default():
    """A resting order at a level price only kisses sits behind the queue already posted
    there. Under-filling is the correct direction for a simulation to err."""
    assert maker_limit_fill(100.0, bar_low=100.0, bar_high=101.0, is_long=True) is None
    assert maker_limit_fill(100.0, 100.0, 101.0, True, require_through=False) == 100.0


def test_it_fills_AT_the_limit_not_at_the_low():
    """A resting order does not get the bar's extreme — it gets its own price."""
    assert maker_limit_fill(100.0, bar_low=95.0, bar_high=101.0, is_long=True) == 100.0


def test_short_side_mirrors():
    assert maker_limit_fill(100.0, 99.0, 100.5, is_long=False) == 100.0
    assert maker_limit_fill(100.0, 99.0, 99.8, is_long=False) is None


@pytest.mark.parametrize("args", [
    (None, 99.0, 101.0, True), ("x", 99.0, 101.0, True), (100.0, None, 101.0, True),
])
def test_bad_input_returns_none_rather_than_raising(args):
    assert maker_limit_fill(*args) is None


def test_an_inverted_bar_is_refused():
    assert maker_limit_fill(100.0, bar_low=101.0, bar_high=99.0, is_long=True) is None

"""A breakout chase must require EXPANDING momentum, not merely positive drift.

WHAT THE CHASE ENGINE IS. When price runs >1.5% away from an armed zone without ever
tapping it, the bot abandons the retest thesis and enters at market. It is the one path
exempt from the entry-zone invariant, because entering outside the zone IS its premise.

WHY IT MATTERS MORE THAN IT LOOKS. Of the 26 trades opened in the 2026-08/09 log, the 13
whose armed zone could be recovered were ALL chase entries, every one filling 5-8% above
its zone (BTC $75,551 against a $69,865-71,065 zone; SOL $88.72 against $82.97-83.94).
The chase is not an edge case — it was the dominant entry path.

THE DEFECT. check_chase_continuation's entire momentum test was:

    momentum_intact = close[-1] > close[-3]

Two bars of net drift. A sequence of SHRINKING green candles satisfies it perfectly, which
is what the operator described watching on ASTER: "the green candles after that showed slow
momentum that kept getting smaller... if the bot was going to enter it should have entered
the first 3 candles of BIG momentum."

That is the distinction encoded here: a chase is only valid while the move is still
ACCELERATING. Once bodies start shrinking, the move is being distributed into, and the
chaser is the exit liquidity.
"""
import pytest

from bot.indicators import momentum_expanding


def _bar(o, c, h=None, l=None):
    hi = h if h is not None else max(o, c)
    lo = l if l is not None else min(o, c)
    return {"open": o, "high": hi, "low": lo, "close": c}


def test_the_aster_pattern_is_rejected():
    """Green, but each body smaller than the last — the move is dying."""
    bars = [_bar(1.00, 1.06), _bar(1.06, 1.09), _bar(1.09, 1.10)]
    assert not momentum_expanding(bars, is_long=True, min_body_abs=0.0)


def test_accelerating_move_is_accepted():
    bars = [_bar(1.00, 1.02), _bar(1.02, 1.06), _bar(1.06, 1.13)]
    assert momentum_expanding(bars, is_long=True, min_body_abs=0.0)


def test_equal_bodies_count_as_sustained():
    """Steady is not decaying — only shrinking is disqualifying."""
    bars = [_bar(1.00, 1.05), _bar(1.05, 1.10), _bar(1.10, 1.15)]
    assert momentum_expanding(bars, is_long=True, min_body_abs=0.0)


def test_the_old_test_would_have_passed_the_aster_pattern():
    """Documents the defect being fixed."""
    bars = [_bar(1.00, 1.06), _bar(1.06, 1.09), _bar(1.09, 1.10)]
    assert bars[-1]["close"] > bars[0]["close"], "old check: close[-1] > close[-3]"
    assert not momentum_expanding(bars, is_long=True, min_body_abs=0.0)


def test_a_red_bar_in_the_sequence_disqualifies():
    """Every bar must push the trade's way; one against it is a stall."""
    bars = [_bar(1.00, 1.04), _bar(1.04, 1.02), _bar(1.02, 1.12)]
    assert not momentum_expanding(bars, is_long=True, min_body_abs=0.0)


def test_final_body_must_still_clear_the_size_floor():
    """Expanding but microscopic is still noise — same floor the displacement gate uses."""
    bars = [_bar(1.000, 1.001), _bar(1.001, 1.003), _bar(1.003, 1.006)]
    assert momentum_expanding(bars, is_long=True, min_body_abs=0.0)
    assert not momentum_expanding(bars, is_long=True, min_body_abs=0.01)


def test_short_side_mirrors():
    falling = [_bar(1.13, 1.06), _bar(1.06, 1.02), _bar(1.02, 1.00)]
    assert not momentum_expanding(falling, is_long=False, min_body_abs=0.0), "decaying"
    accelerating = [_bar(1.13, 1.11), _bar(1.11, 1.07), _bar(1.07, 1.00)]
    assert momentum_expanding(accelerating, is_long=False, min_body_abs=0.0)


def test_a_long_sequence_uses_only_the_last_three():
    bars = [_bar(9, 1), _bar(1, 1), _bar(1.00, 1.02), _bar(1.02, 1.06), _bar(1.06, 1.13)]
    assert momentum_expanding(bars, is_long=True, min_body_abs=0.0)


@pytest.mark.parametrize("bars", [[], [_bar(1, 2)], [_bar(1, 2), _bar(2, 3)], None])
def test_too_few_bars_fails_closed(bars):
    """Cannot verify acceleration -> do not chase. This path enters at MARKET with no
    zone protecting it, so an unverifiable read must not become a free pass."""
    assert not momentum_expanding(bars, is_long=True, min_body_abs=0.0)


def test_malformed_bars_fail_closed_rather_than_raising():
    assert not momentum_expanding([{"open": 1}, {"open": 2}, {"open": 3}], True, 0.0)


def test_float_dust_does_not_read_as_decay():
    """1.15-1.10 computes SMALLER than 1.10-1.05 in binary floating point. A strictly
    non-decreasing test rejects a perfectly steady move on arithmetic noise alone."""
    a, b = 1.10 - 1.05, 1.15 - 1.10
    assert b < a, "the float artefact this tolerance exists for"
    bars = [_bar(1.00, 1.05), _bar(1.05, 1.10), _bar(1.10, 1.15)]
    assert momentum_expanding(bars, is_long=True, min_body_abs=0.0)


def test_a_small_wobble_is_not_decay_but_real_decay_still_fails():
    slight = [_bar(1.00, 1.10), _bar(1.10, 1.199), _bar(1.199, 1.297)]   # ~1-2% smaller
    assert momentum_expanding(slight, is_long=True, min_body_abs=0.0)
    halving = [_bar(1.00, 1.10), _bar(1.10, 1.15), _bar(1.15, 1.175)]    # ~50% smaller
    assert not momentum_expanding(halving, is_long=True, min_body_abs=0.0)

"""The OTE has to be the actual OTE, of an actual leg, in the actual direction.

The operator, looking at the chart: "the OTE, EQH 3 and Fib retracement, those things
I'm talking about the technical analysis". Three separate defects sat behind those two
purple lines, and they have to be fixed together.

1. THE BAND WAS NOT THE OTE. calculate_fib_levels() returned 38.2%-61.8% and both the
   code and its docstring called that the Optimal Trade Entry. The OTE in ICT/SMC is
   61.8%-78.6%, with ~0.705 the midpoint. Measured on ADA's live 60-bar swing the two
   bands did not even overlap:

       drawn as "OTE 0.382"/"OTE 0.618":  0.22951 - 0.23130
       the actual OTE (61.8-78.6%):       0.22823 - 0.22951

   0.22951 was the boundary between them. The bot was waiting for price in a zone
   shallower than the one it was named after.

2. THE ANCHOR WAS NOT A LEG. binance_bot did:

       _sw_lo = float(df_ltf['low'].tail(60).min())
       _sw_hi = float(df_ltf['high'].tail(60).max())

   the minimum and maximum of an arbitrary 5-hour window. A retracement only means
   something measured against an impulse leg; window extremes slide as the window rolls
   and carry no relationship to structure.

3. IT NEVER ASKED WHICH CAME FIRST. calculate_fib_levels always measured DOWN from the
   high. On a DOWN leg a retracement runs UP from the low, and the code had no way to
   express that.

(3) stayed invisible because of (1): the 38.2-61.8 band is symmetric — measured from the
high it is [hi-0.618d, hi-0.382d], and from the low it is [lo+0.382d, lo+0.618d], the
same prices. The REAL OTE is not symmetric: [hi-0.786d, hi-0.618d] against
[lo+0.618d, lo+0.786d]. So correcting the band alone would have switched on a latent
direction bug and made the level wrong half the time. Hence one change, not three.
"""
import pandas as pd
import pytest

from bot.indicators import find_swing_leg, optimal_trade_entry


# ── the band ──────────────────────────────────────────────────────────────────

def test_an_up_leg_retraces_down_from_the_high():
    """Leg 100 -> 200. The OTE is 61.8-78.6% BACK DOWN from 200."""
    lo, hi = optimal_trade_entry(100.0, 200.0, is_up_leg=True)
    assert hi == pytest.approx(200 - 100 * 0.618)      # 138.2
    assert lo == pytest.approx(200 - 100 * 0.786)      # 121.4


def test_a_down_leg_retraces_up_from_the_low():
    """Leg 200 -> 100. The OTE is 61.8-78.6% BACK UP from 100 — different prices."""
    lo, hi = optimal_trade_entry(100.0, 200.0, is_up_leg=False)
    assert lo == pytest.approx(100 + 100 * 0.618)      # 161.8
    assert hi == pytest.approx(100 + 100 * 0.786)      # 178.6


def test_the_two_directions_give_different_bands():
    """The whole reason the direction bug mattered once the band was corrected."""
    up = optimal_trade_entry(100.0, 200.0, is_up_leg=True)
    down = optimal_trade_entry(100.0, 200.0, is_up_leg=False)
    assert up != down
    assert up[1] < down[0], "they should not even overlap on a clean leg"


def test_the_old_band_is_not_what_this_returns():
    """Guards against someone 'simplifying' it back to 38.2-61.8."""
    lo, hi = optimal_trade_entry(100.0, 200.0, is_up_leg=True)
    assert (lo, hi) != (pytest.approx(138.2), pytest.approx(161.8))
    assert lo < 138.2, "the real OTE is DEEPER than the 38.2-61.8 band"


def test_the_band_is_returned_low_first():
    for is_up in (True, False):
        lo, hi = optimal_trade_entry(100.0, 200.0, is_up_leg=is_up)
        assert lo < hi


def test_a_zero_height_leg_yields_nothing_rather_than_a_degenerate_band():
    assert optimal_trade_entry(150.0, 150.0, is_up_leg=True) is None


@pytest.mark.parametrize("bad", [(None, 200.0), (100.0, None), ("x", 200.0)])
def test_unreadable_input_returns_nothing(bad):
    assert optimal_trade_entry(bad[0], bad[1], is_up_leg=True) is None


def test_the_percentages_are_configurable_but_default_to_the_real_ote():
    deep = optimal_trade_entry(100.0, 200.0, is_up_leg=True, lo_pct=0.5, hi_pct=0.5)
    assert deep == (pytest.approx(150.0), pytest.approx(150.0))


# ── the leg ───────────────────────────────────────────────────────────────────

def _df(highs, lows):
    idx = pd.date_range("2026-09-21", periods=len(highs), freq="5min")
    return pd.DataFrame({"high": highs, "low": lows,
                         "open": lows, "close": highs}, index=idx)


def test_a_clean_up_leg_is_found_low_then_high():
    """A pivot low, then a pivot high after it: price impulsed UP."""
    highs = [10, 10, 10, 11, 10, 10, 16, 15, 14, 13, 12, 11, 10]
    lows  = [9,   9,  5,  9,  9,  9, 15, 14, 13, 12, 11, 10,  9]
    leg = find_swing_leg(_df(highs, lows), swing_window=2)
    assert leg is not None
    low, high, is_up = leg
    assert is_up is True and low == 5 and high == 16


def test_a_clean_down_leg_is_found_high_then_low():
    # exactly ONE pivot high and ONE pivot low. An earlier draft put a second high at
    # index 6, which correctly became the leg start — the algorithm takes the most
    # RECENT opposite pivot, not the most extreme one.
    highs = [10, 10, 20, 10, 10, 10, 10, 10, 10, 10, 10]
    lows  = [9,   9,  9,  9,  9,  9,  9,  9,  2,  9,  9]
    leg = find_swing_leg(_df(highs, lows), swing_window=2)
    assert leg is not None
    low, high, is_up = leg
    assert is_up is False and high == 20 and low == 2


def test_the_leg_ends_on_the_MOST_RECENT_pivot():
    """Two legs in the window; the live one is the one that ended last."""
    # the 30 must sit at index >= swing_window or it is inside the edge band and is
    # never a pivot at all — an earlier draft put it at index 1 and found nothing.
    highs = [10, 10, 30, 10, 10, 10, 10, 10, 10, 10, 10]
    lows  = [9,   9,  9,  9,  1,  9,  9,  9,  9,  9,  9]
    low, high, is_up = find_swing_leg(_df(highs, lows), swing_window=2)
    assert is_up is False, "the last pivot was the low at index 4 — a down leg"
    assert high == 30 and low == 1


def test_a_window_with_no_opposite_pivot_before_the_last_one_yields_nothing():
    """A single pivot is not a leg. Refuse rather than inventing the other end."""
    flat = _df([10] * 12, [9] * 12)
    assert find_swing_leg(flat, swing_window=2) is None


@pytest.mark.parametrize("bad", [None, "nope"])
def test_unreadable_frames_yield_nothing(bad):
    assert find_swing_leg(bad) is None


def test_a_frame_too_short_to_hold_a_pivot_yields_nothing():
    assert find_swing_leg(_df([10, 11], [9, 8]), swing_window=2) is None


def test_the_lookback_bounds_how_far_back_a_leg_may_be_anchored():
    """A leg whose START falls outside the lookback is not a leg you can anchor to.

    30 bars: a pivot high at 24 and a pivot low at 27 form the live down leg. Seen
    through a 6-bar window the high is outside it, so there is no leg to report rather
    than a half-leg measured from whatever the window's edge happens to be."""
    highs = [10] * 24 + [30] + [10] * 5          # pivot high at index 24
    lows  = [9] * 27 + [1] + [9] * 2             # pivot low at index 27
    assert len(highs) == len(lows) == 30

    full = find_swing_leg(_df(highs, lows), lookback=60, swing_window=2)
    assert full == (1, 30, False), full

    short = find_swing_leg(_df(highs, lows), lookback=6, swing_window=2)
    assert short is None, "the leg's start is outside the window — refuse it"


# ── the two together, on the shape that prompted this ────────────────────────

def test_an_up_leg_puts_the_ote_below_the_high_where_a_long_would_buy():
    """Sanity in trading terms: after an up impulse you buy the pullback, so the band
    must sit between the leg's midpoint and its origin — not up near the high."""
    leg = find_swing_leg(_df([10, 10, 10, 11, 10, 10, 20, 15, 12, 11, 10],
                             [9,   9,  5,  9,  9,  9, 15, 14, 11, 10,  9]), swing_window=2)
    low, high, is_up = leg
    ote_lo, ote_hi = optimal_trade_entry(low, high, is_up)
    mid = (low + high) / 2
    assert low < ote_lo < ote_hi < mid, (low, ote_lo, ote_hi, mid, high)

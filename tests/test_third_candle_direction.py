"""Candle 3 of an FVG must close in the direction of the displacement.

The operator, looking at the DOGE entry: "the 3rd candle has to be in the direction of
the momentum, it's common sense". They are right, and the detector never checked it. For
a bullish gap it read exactly one number off candle 3:

    if (disp['close'] > disp['open']                 # c2 is bullish
            and disp['close'] > swing_high * (1+c)   # it broke structure
            and prior['high'] < nxt['low']):         # c1.high < c3.low

`nxt['low']` and nothing else. c3 could close red, be a shooting star, be a full
rejection — the gap still armed.

THE REAL ENTRY, DOGE 5m, 2026-09-24:

    14:20  c1   high 0.09510
    14:25  c2   body 0.000900 = 1.89x ATR, UP     <- the displacement
    14:30  c3   open 0.09597  close 0.09592, DOWN <- red. armed anyway
           gap 0.09510-0.09549, armed as 0.0951-0.0955

Price then sat flat for three bars and collapsed at 14:45 on a 2.34x ATR bearish candle,
which is when the sniper filled the long at 0.0954.

MEASURED, 49 armed setups across 13 symbols of 5m tape, outcome = 2R before 1R
within 24 bars (43 decided):

    c3 AGREES with the displacement   n=19   win 89%
    c3 OPPOSES                        n=24   win 54%

and 55% of everything the bot arms today is in the losing bucket.
"""
import pandas as pd
import pytest

from bot.indicators import detect_displacement_fvg


def _frame(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def _base():
    """Flat structure the displacement breaks out of.

    Long enough to clear the detector's own lookback (20). An earlier draft used 6 bars
    and EVERY call returned False for that reason alone — which made two of the
    "refused" tests pass while proving nothing."""
    return [(100.0, 100.4, 99.6, 100.0) for _ in range(20)]


def _bull(c3_close):
    """c1, a big bullish c2 clearing structure, then c3 whose close is the variable."""
    rows = _base()
    rows.append((100.0, 100.5, 99.9, 100.2))        # c1  high 100.5
    rows.append((100.2, 106.0, 100.1, 105.8))       # c2  displacement, +5.6%
    rows.append((105.0, 105.9, 101.0, c3_close))    # c3  low 101.0 > c1.high
    return _frame(rows)


def _bear(c3_close):
    rows = _base()
    rows.append((100.0, 100.1, 99.5, 99.8))         # c1  low 99.5
    rows.append((99.8, 99.9, 94.0, 94.2))           # c2  displacement, -5.6%
    rows.append((95.0, 99.0, 94.1, c3_close))       # c3  high 99.0 < c1.low
    return _frame(rows)


# ── the rule ─────────────────────────────────────────────────────────────────

def test_a_bullish_gap_with_a_GREEN_third_candle_is_found():
    found, dirn, lo, hi, _ = detect_displacement_fvg(_bull(105.7))   # close > open 105.0
    assert found and dirn == "bullish"
    assert (lo, hi) == (100.5, 101.0)


def test_a_bullish_gap_with_a_RED_third_candle_is_refused():
    """The DOGE case: c3 opened 0.09597 and closed 0.09592."""
    assert not detect_displacement_fvg(_bull(104.0))[0]   # close < open 105.0


def test_a_bearish_gap_with_a_RED_third_candle_is_found():
    found, dirn, lo, hi, _ = detect_displacement_fvg(_bear(94.5))    # close < open 95.0
    assert found and dirn == "bearish"
    assert (lo, hi) == (99.0, 99.5)


def test_a_bearish_gap_with_a_GREEN_third_candle_is_refused():
    assert not detect_displacement_fvg(_bear(96.0))[0]   # close > open 95.0


def test_a_doji_third_candle_is_refused_in_both_directions():
    """close == open is not 'in the direction of' anything."""
    assert not detect_displacement_fvg(_bull(105.0))[0]
    assert not detect_displacement_fvg(_bear(95.0))[0]


# ── it must not be switched off by accident ──────────────────────────────────

def test_the_fixture_is_long_enough_to_reach_the_detector():
    """Guard the guard: if the frame is shorter than `lookback` the detector bails before
    any rule runs, and every assertion below becomes vacuous."""
    assert len(_bull(105.7)) >= 20   # detector's lookback default
    assert detect_displacement_fvg(_bull(105.7))[0], "positive control must be found"


def test_the_rule_can_be_disabled_explicitly_for_comparison():
    assert detect_displacement_fvg(_bull(104.0), require_c3_direction=False)[0]


def test_it_is_ON_by_default():
    assert not detect_displacement_fvg(_bull(104.0))[0]


# ── the gap geometry it must NOT change ──────────────────────────────────────

def test_the_gap_bounds_are_unchanged_when_c3_agrees():
    """This rule filters; it must not move the zone."""
    for close, expect in ((105.7, (100.5, 101.0)),):
        _f, _d, lo, hi, _b = detect_displacement_fvg(_bull(close))
        assert (lo, hi) == expect


def test_a_c3_that_agrees_but_leaves_no_gap_is_still_refused():
    """Direction alone is not enough — the imbalance still has to exist."""
    rows = _base()
    rows.append((100.0, 100.5, 99.9, 100.2))
    rows.append((100.2, 106.0, 100.1, 105.8))
    rows.append((105.0, 105.9, 100.4, 105.7))        # c3.low 100.4 < c1.high 100.5
    assert not detect_displacement_fvg(_frame(rows))[0]

"""The sniper's R:R must survive the fill, not just the arm.

The 2026-09-01 fix re-caps QTY at the fill so a stop-out still loses only
MAX_RISK_DOLLARS. That holds the LOSS fixed — but as the fill drifts away from the armed
SL, the REWARD shrinks, and the ratio quietly falls under the floor the rest of the
system enforces. The only reward check at fill was an absolute $25 floor, which says
nothing about the ratio.

REAL FILL — SOL/USD LONG, 2026-09-10 00:20 UTC:

    armed zone   101.06 .. 101.25   SL 99.5707   TP 104.3579
    at zone LOW   risk 1.4893  reward 3.2979  ->  1:2.21   passes
    at zone HIGH  risk 1.6793  reward 3.1079  ->  1:1.85   already under
    FILLED 101.32 risk 1.7493  reward 3.0379  ->  1:1.74   traded anyway

Risk was a correct $20.00 throughout — which is exactly why it went unnoticed. The
operator caught it off the dashboard tile reading "R:R 1:1.7".
"""
import pytest

MIN_AI_RR = 2.0
SL, TP = 99.57071428571429, 104.35785714285714


def rr_at(fill):
    return abs(TP - fill) / abs(fill - SL)


def test_the_real_sol_fill_is_under_the_floor():
    assert rr_at(101.32) == pytest.approx(1.74, abs=0.01)
    assert rr_at(101.32) < MIN_AI_RR


def test_it_qualified_at_the_zone_low_which_is_why_it_armed():
    assert rr_at(101.06) == pytest.approx(2.21, abs=0.01)
    assert rr_at(101.06) >= MIN_AI_RR


def test_it_was_already_failing_at_the_zone_high():
    """The decay does not need slippage — the top of its own zone is enough."""
    assert rr_at(101.25) == pytest.approx(1.85, abs=0.01)
    assert rr_at(101.25) < MIN_AI_RR


def test_the_dollar_floor_could_not_have_caught_it():
    """reward $34.73 clears the $25 dust floor while the RATIO is 1:1.74."""
    qty = 11.433238056349593
    assert abs(TP - 101.32) * qty == pytest.approx(34.73, abs=0.01)
    assert abs(TP - 101.32) * qty > 25.0
    assert rr_at(101.32) < MIN_AI_RR


def test_a_fill_at_or_better_than_the_arm_still_passes():
    assert rr_at(101.00) >= MIN_AI_RR


def test_zero_risk_distance_is_refused_not_divided_by():
    risk = 0.0
    rr = (abs(TP - SL) / risk) if risk > 0 else 0.0
    assert rr == 0.0 and rr < MIN_AI_RR


def test_short_side_mirrors():
    sl, tp, fill = 105.0, 95.0, 104.0
    rr = abs(tp - fill) / abs(fill - sl)
    assert rr == pytest.approx(9.0)
    fill_bad = 100.5
    assert abs(tp - fill_bad) / abs(fill_bad - sl) == pytest.approx(1.222, abs=0.01)

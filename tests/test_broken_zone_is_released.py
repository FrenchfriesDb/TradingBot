"""A zone price has broken through is dead. Release the symbol.

2026-09-29, live, five of six armed stock symbols were holding demand zones that price
had already fallen THROUGH:

    QQQ    738.32   zone 739.51-740.69   -0.2% below
    TSLA   354.08   zone 377.78-379.80   -6.3% below   (armed two sessions earlier)
    GOOGL  338.21   zone 344.76-348.00   -1.9% below
    META   733.02   zone 739.00-760.50   -0.8% below
    NVDA   228.09   zone 230.65-230.68   -1.1% below
    MSFT   511.19   zone 493.65-508.50   +0.5% above   <- the only live one

None of those can ever fill: tap_chase_ok correctly refuses a long below its own demand
zone, and says so on every poll — "the zone failed, not a tap". Nothing acted on it. The
symbol stayed in ENTRY_WAIT until AMD_ENTRY_WAIT_ITERS=96 expired, which at a 15-minute
loop is 24 hours of market time, roughly four sessions.

So the bot was not refusing good trades. It was OCCUPYING five of its eight symbols with
setups that were already invalid, unable to hunt new structure on any of them.

There WAS an abandon check and it was one-sided:

    ran_away = (bias == "BEARISH" and price < zsp * 0.985) or
               (bias == "BULLISH" and price > zsp * 1.015)

That releases a demand zone when price runs UP and away from it — the "the move left
without us" case. It has no case for price breaking DOWN through it, which is the one
that actually invalidates the setup.

A wick through must NOT kill the zone: a tap is supposed to poke into it. Only a
decisive break counts, measured in ATR so it scales with the instrument.
"""
import pytest

from bot.indicators import zone_broken


ATR = 1.0


def test_the_marginal_case_is_NOT_broken_yet():
    """QQQ, the shallowest of the five: 738.32 against a 739.51 zone low is 1.19 under,
    which with a 2.0 ATR is half a bar's range. That can still recover, and killing it
    would delete legitimate setups. The rule is deliberately not that eager."""
    assert not zone_broken(738.32, 739.51, 740.69, is_long=True, atr=2.0)


def test_the_four_genuinely_broken_ones_are_released():
    """The cases that had been stuck for one to two sessions."""
    assert zone_broken(354.08, 377.78, 379.80, is_long=True, atr=5.0)    # TSLA -6.3%
    assert zone_broken(338.21, 344.76, 348.00, is_long=True, atr=1.5)    # GOOGL -1.9%
    assert zone_broken(228.09, 230.65, 230.68, is_long=True, atr=1.0)    # NVDA -1.1%
    assert zone_broken(733.02, 739.00, 760.50, is_long=True, atr=3.0)    # META -0.8%


def test_teslas_real_case():
    """6.3% below the zone, held for two sessions."""
    assert zone_broken(354.08, 377.78, 379.80, is_long=True, atr=5.0)


def test_a_shallow_wick_through_does_NOT_break_it():
    """A tap pokes into the zone. Killing the setup on that would delete the entry."""
    assert not zone_broken(99.7, 100.0, 101.0, is_long=True, atr=ATR)


def test_price_inside_the_zone_is_not_broken():
    assert not zone_broken(100.5, 100.0, 101.0, is_long=True, atr=ATR)


def test_price_above_a_demand_zone_is_not_broken():
    """That is the 'ran away' case, handled separately — not this one."""
    assert not zone_broken(110.0, 100.0, 101.0, is_long=True, atr=ATR)


def test_the_short_side_mirrors():
    assert zone_broken(105.0, 100.0, 101.0, is_long=False, atr=ATR)      # through the top
    assert not zone_broken(101.3, 100.0, 101.0, is_long=False, atr=ATR)  # shallow wick
    assert not zone_broken(90.0, 100.0, 101.0, is_long=False, atr=ATR)   # ran away


def test_the_tolerance_scales_with_atr():
    """A $2 break is noise on NVDA and a collapse on a penny stock."""
    assert not zone_broken(98.0, 100.0, 101.0, is_long=True, atr=10.0)
    assert zone_broken(98.0, 100.0, 101.0, is_long=True, atr=0.5)


def test_the_multiplier_is_configurable():
    assert zone_broken(99.0, 100.0, 101.0, is_long=True, atr=ATR, atr_mult=0.5)
    assert not zone_broken(99.0, 100.0, 101.0, is_long=True, atr=ATR, atr_mult=2.0)


def test_a_dead_atr_falls_back_to_a_percentage_rather_than_never_breaking():
    """Fail-safe direction matters here: with atr=0 a zero tolerance would break a zone
    on any tick below it, deleting every legitimate tap. A percentage floor keeps it
    sane."""
    assert not zone_broken(99.95, 100.0, 101.0, is_long=True, atr=0.0)
    assert zone_broken(95.0, 100.0, 101.0, is_long=True, atr=0.0)


@pytest.mark.parametrize("bad", [(None, 100.0, 101.0), (99.0, None, 101.0), (99.0, 100.0, None)])
def test_unreadable_input_never_reports_a_break(bad):
    """Refusing to break is the safe direction — the zone simply expires as before."""
    assert not zone_broken(bad[0], bad[1], bad[2], is_long=True, atr=ATR)


def test_an_inverted_zone_is_refused():
    assert not zone_broken(99.0, 101.0, 100.0, is_long=True, atr=ATR)

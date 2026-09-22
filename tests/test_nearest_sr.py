"""A level below price is not resistance.

2026-09-22, the stock bot's own status lines. Seven of eight symbols reported a
"resistance" that price had already traded clean through:

    AAPL  price 342.33   sr=res=335.73    1.9% BELOW price
    QQQ   price 746.85   sr=res=718.86    3.7% BELOW
    SPY   price 774.32   sr=res=762.47    1.5% BELOW
    NVDA  price 229.49   sr=res=222.24    3.2% BELOW
    TSLA  price 379.56   sr=res=369.30    2.7% BELOW
    META  price 740.48   sr=res=664.30   10.3% BELOW
    GOOGL price 353.18   sr=res=348.97    1.2% BELOW
    MSFT  price 497.28   sr=res=509.82    2.5% above   <- the only honest one

CAUSE: find_support_resistance() clusters swing highs into `resistance` and swing lows
into `support`, sorts each by TOUCH COUNT, and never once compares them to current
price. `resistance[0]` therefore means "most-tested swing-high cluster in the last 50
bars" — which after a rally sits below price. The status line printed it as res= anyway.

Which list a level came from does not decide what it is doing now; the side of price
does. A resistance price has broken through has flipped to support — that polarity flip
is the whole basis of the breaker and IFVG zones this same file already trades.

Note this is a DISPLAY fix. is_near_sr_level() takes resistance + support together to
ask whether a BOS cut through a known level, and a broken level is exactly what a BOS
cuts through — that use was never wrong and is untouched.
"""
import pytest

from bot.indicators import nearest_sr


def test_the_meta_line_that_was_10_percent_wrong():
    """664.30 was the most-tested cluster; price was 740.48. It is support now."""
    above, below = nearest_sr([664.30, 700.00, 755.00], 740.48)
    assert below == 664.30 or below == 700.00
    assert above == 755.00
    assert below == 700.00, "it must pick the NEAREST below, not the most-tested"


def test_a_level_below_price_is_never_reported_as_resistance():
    for price, levels in [(342.33, [335.73]), (746.85, [718.86]), (229.49, [222.24])]:
        above, below = nearest_sr(levels, price)
        assert above is None, f"{levels[0]} is below {price} and was called resistance"
        assert below == levels[0]


def test_msfts_line_was_right_and_stays_right():
    above, below = nearest_sr([509.82], 497.28)
    assert above == 509.82 and below is None


def test_both_sides_are_reported_when_both_exist():
    above, below = nearest_sr([90.0, 95.0, 105.0, 120.0], 100.0)
    assert above == 105.0 and below == 95.0


def test_it_picks_the_nearest_on_each_side_not_the_extreme():
    above, below = nearest_sr([10.0, 50.0, 99.0, 101.0, 150.0, 900.0], 100.0)
    assert above == 101.0 and below == 99.0


def test_the_two_lists_are_pooled_because_polarity_flips():
    """A swept swing-LOW sitting above price is resistance; a broken swing-HIGH below is
    support. Keeping the lists separate would mislabel both."""
    resistance_list = [95.0]      # a swing high price has broken above
    support_list    = [110.0]     # a swing low price has fallen below... from above
    above, below = nearest_sr(resistance_list + support_list, 100.0)
    assert above == 110.0, "a level above price is resistance whatever list it came from"
    assert below == 95.0


def test_a_level_exactly_at_price_is_neither():
    above, below = nearest_sr([100.0], 100.0)
    assert above is None and below is None


@pytest.mark.parametrize("bad_price", [0, -1, None, float("nan"), "x"])
def test_an_unusable_price_yields_nothing_rather_than_a_wrong_label(bad_price):
    assert nearest_sr([90.0, 110.0], bad_price) == (None, None)


def test_no_levels_yields_nothing():
    assert nearest_sr([], 100.0) == (None, None)
    assert nearest_sr(None, 100.0) == (None, None)


def test_junk_entries_are_skipped_not_fatal():
    above, below = nearest_sr([None, "abc", float("nan"), 0.0, -5.0, 105.0, 95.0], 100.0)
    assert above == 105.0 and below == 95.0

"""Inducement (IDM) detection — the minor liquidity pool sitting BETWEEN current price
and the real HTF zone, which price typically sweeps first to trap early entrants before
delivering to the actual zone.

Debbie-La relevance: if an inducement level exists between price and our armed zone, the
zone tap is likely to be preceded by a stop-hunt through that minor level. Marking it on
the chart shows WHY a "clean looking" entry got wicked first.

Pure function, no live data — geometry is provably correct.

Convention: candles are (open, high, low, close), oldest first.
For a BULLISH setup (we intend to buy at a demand zone BELOW price), inducement is the
nearest minor swing LOW between the zone and price — the stops resting under it get
swept on the way down into the zone.
For a BEARISH setup (selling at supply ABOVE price), inducement is the nearest minor
swing HIGH between price and the zone.
"""
import pytest

from bot.indicators import detect_inducement


def test_bullish_finds_minor_swing_low_between_zone_and_price():
    # demand zone at 95-96, price at 105. A minor swing low at 99 sits between them.
    candles = [
        (104, 105, 103, 104),
        (104, 105, 99, 100),    # minor swing low = 99
        (100, 103, 100, 102),
        (102, 106, 101, 105),
    ]
    idm = detect_inducement(candles, zone_low=95, zone_high=96,
                            current_price=105, is_long=True)
    assert idm == pytest.approx(99, abs=1e-6)


def test_bullish_ignores_lows_below_the_zone():
    # a low at 90 is BELOW the demand zone (95-96) — that's not inducement, it's
    # beyond the target; only levels BETWEEN zone and price count.
    candles = [
        (104, 105, 90, 100),    # 90 is below zone_low -> ignored
        (100, 103, 92, 102),    # 92 also below the zone -> ignored
    ]
    idm = detect_inducement(candles, zone_low=95, zone_high=96,
                            current_price=105, is_long=True)
    assert idm is None


def test_bullish_ignores_lows_above_current_price():
    candles = [(104, 112, 110, 111)]   # low 110 is ABOVE price 105 -> not between
    idm = detect_inducement(candles, zone_low=95, zone_high=96,
                            current_price=105, is_long=True)
    assert idm is None


def test_bullish_picks_the_lowest_qualifying_low_closest_to_the_zone():
    # two candidates in range: 99 and 97. The one nearest the zone (97) is the
    # inducement price must clear last before reaching the zone.
    candles = [
        (104, 105, 99, 100),
        (100, 103, 97, 98),
        (98, 104, 100, 103),
    ]
    idm = detect_inducement(candles, zone_low=95, zone_high=96,
                            current_price=105, is_long=True)
    assert idm == pytest.approx(97, abs=1e-6)


def test_bearish_finds_minor_swing_high_between_price_and_zone():
    # supply zone 115-116, price 105. A minor swing high at 110 sits between.
    candles = [
        (106, 107, 105, 106),
        (106, 110, 105, 107),   # minor swing high = 110
        (107, 109, 106, 108),
    ]
    idm = detect_inducement(candles, zone_low=115, zone_high=116,
                            current_price=105, is_long=False)
    assert idm == pytest.approx(110, abs=1e-6)


def test_bearish_ignores_highs_above_the_zone():
    candles = [(106, 120, 105, 118)]   # 120 is above zone_high 116 -> ignored
    idm = detect_inducement(candles, zone_low=115, zone_high=116,
                            current_price=105, is_long=False)
    assert idm is None


def test_bearish_picks_the_highest_qualifying_high_closest_to_the_zone():
    candles = [
        (106, 110, 105, 107),
        (107, 113, 106, 112),   # 113 is nearer the supply zone
        (112, 111, 108, 109),
    ]
    idm = detect_inducement(candles, zone_low=115, zone_high=116,
                            current_price=105, is_long=False)
    assert idm == pytest.approx(113, abs=1e-6)


def test_returns_none_without_a_zone():
    candles = [(104, 105, 99, 100)]
    assert detect_inducement(candles, zone_low=None, zone_high=None,
                             current_price=105, is_long=True) is None


def test_returns_none_on_empty_candles():
    assert detect_inducement([], zone_low=95, zone_high=96,
                             current_price=105, is_long=True) is None


def test_respects_lookback_window():
    # the qualifying low is 5 bars back; a lookback of 2 must not see it
    # filler candles sit entirely ABOVE current_price so they can never qualify —
    # only the oldest candle's 99 low is a real inducement candidate.
    candles = [
        (104, 105, 99, 100),   # the only qualifying low, oldest
        (106, 108, 106, 107),
        (106, 108, 106, 107),
        (106, 108, 106, 107),
        (106, 108, 106, 107),
    ]
    assert detect_inducement(candles, zone_low=95, zone_high=96,
                             current_price=105, is_long=True, lookback=2) is None
    assert detect_inducement(candles, zone_low=95, zone_high=96,
                             current_price=105, is_long=True, lookback=5) == pytest.approx(99)

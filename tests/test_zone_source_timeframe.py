"""A level drawn on the wrong chart looks invented. Say which chart it came from.

The operator: "i don't even see an FVG". Verified live — BTC's armed zone was
82087-84753, and:

    BTC 5m:  no matching 3-candle gap in 350 bars
    BTC 6H:  REAL bullish FVG at 09-21 06:00 UTC
             c1.high 82087.11 -> c3.low 84753.25   exact match

The zone was correct. It came from the SIX-HOUR candles, because binance_bot picks
`is_fvg_bull_htf or is_fvg_bull_ltf` and prefers the HTF when both exist. The chart
renders FIVE-MINUTE candles. So a real 6H gap gets drawn as two lines across a 5m chart
with no three-candle structure anywhere near them, and it reads as a fabricated level.

Nothing about the number is wrong; the chart just never said which timeframe produced
it. A second, smaller version of the same problem: tf_tag hardcoded "4H" in the log
while the HTF actually fetched is "6h" (binance_bot.py:1717).
"""
import pytest

from bot.indicators import zone_source_tf


def test_the_htf_wins_when_both_timeframes_have_one():
    """Matches the existing selection: `htf_value if is_htf else ltf_value`."""
    assert zone_source_tf(True, True, "6h", "5m") == "6h"


def test_the_ltf_is_reported_when_only_it_has_one():
    assert zone_source_tf(False, True, "6h", "5m") == "5m"


def test_the_htf_is_reported_when_only_it_has_one():
    assert zone_source_tf(True, False, "6h", "5m") == "6h"


def test_no_zone_reports_no_timeframe_rather_than_guessing():
    assert zone_source_tf(False, False, "6h", "5m") is None


@pytest.mark.parametrize("htf,ltf", [("6h", "5m"), ("4h", "15m"), ("1d", "1h")])
def test_the_names_are_passed_through_not_hardcoded(htf, ltf):
    """tf_tag said "4H" while the bot fetched "6h". The label must come from the same
    place the data does, or it drifts again the moment someone changes the timeframe."""
    assert zone_source_tf(True, False, htf, ltf) == htf
    assert zone_source_tf(False, True, htf, ltf) == ltf


def test_truthiness_is_enough_so_callers_can_pass_their_detection_flags():
    assert zone_source_tf(1, 0, "6h", "5m") == "6h"
    assert zone_source_tf(None, "yes", "6h", "5m") == "5m"

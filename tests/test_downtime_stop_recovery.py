"""A stop must still be honoured for the time the bot was NOT running.

binance_bot paper-trades, so its stop exists only inside the process. While the bot is
down there is no resting order anywhere — and the startup catch-up compared only the
CURRENT price against the levels:

    _cur   = float(_t["last"])
    sl_hit = (is_long and _cur <= st.stop_loss) or ...

So a stop blown through at 03:00 and recovered from by 08:00 was invisible. The position
carried on, and the account went on to report a result no broker would have produced.
Walking the downtime's candles gives the stop its authority back.
"""
import pytest

from bot.indicators import first_protective_breach


def _c(ts, high, low):
    return [ts, low, high, low, low, 1.0]     # [ts, open, high, low, close, vol]


LONG = dict(stop=99.0, target=110.0, is_long=True)


def test_the_exact_miss_this_fixes():
    """Dives through the stop mid-gap, recovers before the bot is back up."""
    candles = [_c(1, 101, 100), _c(2, 100, 98.5), _c(3, 103, 102)]
    hit = first_protective_breach(candles, **LONG)
    assert hit is not None, "current-price-only checking missed this entirely"
    kind, price, ts = hit
    assert (kind, price, ts) == ("STOP", 99.0, 2)


def test_a_wick_counts_even_though_the_close_recovered():
    """A resting order fills the instant price TOUCHES the level."""
    kind, _, _ = first_protective_breach([_c(1, 105, 98.0)], **LONG)
    assert kind == "STOP"


def test_chronology_decides_stop_then_target():
    candles = [_c(1, 101, 98.0), _c(2, 111, 109)]
    assert first_protective_breach(candles, **LONG)[0] == "STOP"


def test_chronology_decides_target_then_stop():
    candles = [_c(1, 111, 109), _c(2, 101, 98.0)]
    assert first_protective_breach(candles, **LONG)[0] == "TARGET"


def test_both_in_one_candle_resolves_to_stop():
    """Order within a bar is unknowable. A sim must not award itself the better outcome."""
    assert first_protective_breach([_c(1, 111, 98.0)], **LONG)[0] == "STOP"


def test_untouched_range_returns_none():
    assert first_protective_breach([_c(1, 105, 101), _c(2, 106, 100)], **LONG) is None


def test_bars_predating_the_entry_are_skipped():
    """Their wicks carry prices from before the position existed — an order that did not
    exist yet cannot fill on them."""
    candles = [_c(100, 120, 90), _c(200, 105, 101)]
    assert first_protective_breach(candles, since_ms=150, **LONG) is None
    assert first_protective_breach(candles, since_ms=0, **LONG)[0] == "STOP"


def test_short_side_mirrors():
    S = dict(stop=110.0, target=90.0, is_long=False)
    assert first_protective_breach([_c(1, 111, 105)], **S)[0] == "STOP"
    assert first_protective_breach([_c(1, 105, 89)], **S)[0] == "TARGET"
    assert first_protective_breach([_c(1, 109, 91)], **S) is None


@pytest.mark.parametrize("bad", [None, [], [[]], [["x"]]])
def test_malformed_input_returns_none_rather_than_raising(bad):
    assert first_protective_breach(bad, 99.0, 110.0, True) is None


def test_a_single_unreadable_candle_does_not_abort_the_scan():
    candles = [["junk"], _c(2, 100, 98.0)]
    assert first_protective_breach(candles, **LONG)[0] == "STOP"

"""The bar that FILLS a crypto zone must itself be decisive — pinned to two real losses.

WHY (2026-10-02). SOL and DOGE both entered LONG on a dying tape and both stopped, -$45.79
for the day. Real 5m bars leading into each fill:

    SOL   08:40  0.87x ATR DOWN | 08:45 0.30x DOWN | 08:50 0.33x DOWN | 08:55 0.14x UP  <- filled
    DOGE  08:40  0.41x ATR DOWN | 08:45 0.56x DOWN | 08:50 0.77x DOWN | 08:56 0.36x UP  <- filled

Three down bars, then a doji, bought both times.

binance_bot's fresh-tap path had NO momentum requirement. tap_candle_opposes_bias is a VETO
— its own docstring says "a `normal` or `doji` tap still passes" — so only a decisively
opposing bar could stop an entry. The gate that fixes this (has_displacement) already
existed and was wired onto the STOCK bot on 2026-09-29; this bot never got it.

The operator reported this failure repeatedly before it was fixed. The 60-day/85k-tap study
also agreed, on NET EXPECTANCY: 1.4x is -0.18R vs -0.24R for veto-only at 0.25% fees, and
+0.04R vs -0.03R at 0.10%. It was initially read by TOTAL R — the wrong criterion for a bot
taking ~3 trades a month, where per-trade expectancy is all it ever experiences.
"""
import pytest

from bot import indicators
import binance_bot as bb


def _floor(atr, px, mult):
    return indicators.displacement_min_body(atr, px, mult, bb.DISPLACEMENT_MIN_PCT)


def _allows(bars, atr, px, mult):
    return indicators.has_displacement(
        bars, True, min_body_frac=bb.DISPLACEMENT_BODY_FRAC,
        min_body_abs=_floor(atr, px, mult))


# Real bars, [open, high, low, close], the three ending at each fill.
SOL = [[122.22, 122.22, 122.07, 122.11],
       [122.10, 122.17, 121.95, 121.98],
       [121.98, 122.21, 121.96, 122.03]]
SOL_ATR, SOL_PX = 0.36643, 122.03

DOGE = [[0.09743, 0.09744, 0.09724, 0.09724],
        [0.09722, 0.09732, 0.09696, 0.09696],
        [0.09697, 0.09721, 0.09697, 0.09709]]
DOGE_ATR, DOGE_PX = 0.00034, 0.09709


def test_the_gate_is_wired_and_defaults_to_the_measured_best_row():
    assert bb.TAP_DISPLACEMENT_ATR_MULT == 1.4
    assert 'os.getenv("TAP_DISPLACEMENT_ATR_MULT"' in open("binance_bot.py", encoding="utf-8").read()


@pytest.mark.parametrize("name,bars,atr,px", [("SOL", SOL, SOL_ATR, SOL_PX),
                                              ("DOGE", DOGE, DOGE_ATR, DOGE_PX)])
def test_both_real_losing_taps_are_refused(name, bars, atr, px):
    assert not _allows(bars, atr, px, bb.TAP_DISPLACEMENT_ATR_MULT), (
        f"{name}: the weak tap that lost money would still be taken")


@pytest.mark.parametrize("name,bars,atr,px", [("SOL", SOL, SOL_ATR, SOL_PX),
                                              ("DOGE", DOGE, DOGE_ATR, DOGE_PX)])
def test_they_are_refused_even_at_the_loosest_setting_worth_running(name, bars, atr, px):
    """Both fills were under 0.4x ATR, so no sane threshold admits them."""
    assert not _allows(bars, atr, px, 1.0)


def test_a_genuinely_strong_tap_still_enters():
    """The gate must not simply block everything — a decisive bar in the trade's
    direction has to pass, or the bot stops trading entirely."""
    strong = [[122.00, 122.05, 121.95, 121.98],
              [121.98, 122.02, 121.90, 121.95],
              [121.95, 123.10, 121.93, 123.05]]        # body 1.10 = 3.0x ATR, 93% of range
    assert _allows(strong, SOL_ATR, 123.05, bb.TAP_DISPLACEMENT_ATR_MULT)


def test_an_opposing_strong_bar_is_still_refused_for_a_long():
    """Decisive but DOWN — the POL/USD marubozu_bear shape. Must not pass a LONG."""
    against = [[122.00, 122.05, 121.95, 121.98],
               [121.98, 122.02, 121.90, 121.95],
               [122.95, 122.98, 121.80, 121.85]]
    assert not _allows(against, SOL_ATR, 121.85, bb.TAP_DISPLACEMENT_ATR_MULT)


def test_the_gate_fails_closed_in_source():
    """An unreadable tape must refuse, not wave the entry through."""
    src = open("binance_bot.py", encoding="utf-8").read()
    i = src.index("Could not read tap momentum")
    assert "return price" in src[i:i + 200], "tap-momentum failure path does not stand aside"


def test_the_stock_bot_keeps_its_own_measured_value():
    """Two bots, two separately measured thresholds — 1.0x stock, 1.4x crypto."""
    from bot import strategy
    assert strategy.TAP_DISPLACEMENT_ATR_MULT == 1.0
    assert bb.TAP_DISPLACEMENT_ATR_MULT == 1.4

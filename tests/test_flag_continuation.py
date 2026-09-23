"""Flag continuation: a pole, a flag, a break, and then the retest we actually trade.

detect_bull_flag/detect_bear_flag have existed in this file for months and were wired to
exactly one thing — the text `struct=🚩bull_flag` in a log line. The pole bounds, the flag
bounds and the measured target were all thrown away at the call site (`bull_flag, *_bfd =
...`). Nothing traded them.

THE AWKWARD BIT. The detector cannot see its own breakout. detect_bull_flag refuses when
`flag_high >= pole_high`, so the instant price breaks out of the flag, the flag stops
being detected. Asking "is there a flag AND has it broken out" in one call is impossible.

So this helper asks it as two questions against one dataframe, with no new persistent
state: "was there an intact flag as of N bars ago, and has price closed through its edge
since?" — by running the existing detector on a shifted window.

THE OTHER AWKWARD BIT, which is a live trap for anyone editing this:

    detect_bull_flag -> (found, pole_low,  pole_high, flag_low, flag_high, target)
    detect_bear_flag -> (found, pole_high, pole_low,  flag_low, flag_high, target)

Positions 1 and 2 swap meaning between the two functions. Positions 3 and 4 do not.
"""
import pandas as pd
import pytest

from bot.indicators import detect_bull_flag, detect_bear_flag, flag_breakout_retest


def _df(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def _bull(breakout_bars=1, breakout_close=109.5):
    """10-bar pole 100->110 (10%), 8-bar flag 106-109, then a close above 109."""
    rows = [(100 + i, 101 + i, 100 + i, 100.5 + i) for i in range(10)]      # pole
    rows += [(108, 109, 106, 107) for _ in range(8)]                        # flag
    rows += [(108.5, breakout_close + 0.7, 108.0, breakout_close)
             for _ in range(breakout_bars)]                                 # break up
    return _df(rows)


def _bear(breakout_bars=1, breakout_close=100.5):
    """Mirror: pole 110->100, flag 101-104, then a close below 101."""
    rows = [(110 - i, 110 - i, 109 - i, 109.5 - i) for i in range(10)]      # pole down
    rows += [(102, 104, 101, 103) for _ in range(8)]                        # flag
    rows += [(101.5, 102.0, breakout_close - 0.7, breakout_close)
             for _ in range(breakout_bars)]                                 # break down
    return _df(rows)


# ── the fixtures must actually be flags, or nothing below proves anything ────

def test_the_bull_fixture_really_is_an_intact_flag_before_the_breakout():
    found, pole_low, pole_high, flag_low, flag_high, target = detect_bull_flag(_bull().iloc[:-1])
    assert found, "fixture is not a flag; every bull test below is vacuous"
    assert (pole_low, pole_high) == (100.0, 110.0)
    assert (flag_low, flag_high) == (106.0, 109.0)
    assert target == pytest.approx(119.0)          # flag_high + pole_body


def test_the_bear_fixture_really_is_an_intact_flag_before_the_breakout():
    found, pole_high, pole_low, flag_low, flag_high, target = detect_bear_flag(_bear().iloc[:-1])
    assert found, "fixture is not a bear flag"
    assert (pole_high, pole_low) == (110.0, 100.0)
    assert (flag_low, flag_high) == (101.0, 104.0)
    assert target == pytest.approx(91.0)           # flag_low - pole_body


def test_the_detector_really_does_go_blind_after_the_breakout():
    """The premise of this whole helper. If this ever stops being true, simplify."""
    assert not detect_bull_flag(_bull(breakout_bars=1, breakout_close=112.0))[0]


# ── finding the setup ────────────────────────────────────────────────────────

def test_a_bull_flag_that_broke_out_is_found():
    found, lo, hi, stop_ref, target = flag_breakout_retest(_bull(), is_long=True)
    assert found
    assert lo < 109.0 < hi, f"retest band {lo}-{hi} must bracket the broken flag high"
    assert stop_ref == 106.0, "stop anchors to the flag low"
    assert target == pytest.approx(119.0)


def test_a_bear_flag_that_broke_down_is_found():
    found, lo, hi, stop_ref, target = flag_breakout_retest(_bear(), is_long=False)
    assert found
    assert lo < 101.0 < hi, f"retest band {lo}-{hi} must bracket the broken flag low"
    assert stop_ref == 104.0, "stop anchors to the flag high"
    assert target == pytest.approx(91.0)


def test_a_flag_that_never_broke_out_is_refused():
    """Still inside the flag — there is nothing to retest yet."""
    df = _bull(breakout_bars=1, breakout_close=108.0)      # 108 < flag_high 109
    assert not flag_breakout_retest(df, is_long=True)[0]


def test_a_bull_flag_that_broke_the_WRONG_way_is_refused():
    """Price fell out of the bottom of the flag. That is a failure, not a continuation."""
    df = _bull(breakout_bars=1, breakout_close=104.0)
    assert not flag_breakout_retest(df, is_long=True)[0]


def test_a_stale_breakout_is_refused():
    """max_shift bounds how long ago the flag could have been intact. Beyond it the
    'retest' is just a level someone drew a week ago."""
    assert flag_breakout_retest(_bull(breakout_bars=3), is_long=True, max_shift=6)[0]
    assert not flag_breakout_retest(_bull(breakout_bars=9), is_long=True, max_shift=6)[0]


def test_flat_data_yields_nothing():
    flat = _df([(100, 100.5, 99.5, 100) for _ in range(30)])
    assert not flag_breakout_retest(flat, is_long=True)[0]
    assert not flag_breakout_retest(flat, is_long=False)[0]


def test_direction_is_not_interchangeable():
    """A bull flag must not be reported to a short, or vice versa."""
    assert not flag_breakout_retest(_bull(), is_long=False)[0]
    assert not flag_breakout_retest(_bear(), is_long=True)[0]


# ── the retest band ──────────────────────────────────────────────────────────

def test_the_band_is_atr_scaled_not_a_fixed_width():
    narrow = flag_breakout_retest(_bull(), is_long=True, atr_mult=0.25)
    wide   = flag_breakout_retest(_bull(), is_long=True, atr_mult=1.0)
    assert (wide[2] - wide[1]) > (narrow[2] - narrow[1])


def test_a_dead_atr_refuses_rather_than_emitting_a_zero_width_band():
    """range_atr() returns 0.0 when it cannot compute. A band of [x, x] is the 4-cent
    AAPL zone all over again — price crosses it inside a tick. Fail closed."""
    rows = [(100 + i, 100 + i, 100 + i, 100 + i) for i in range(10)]   # zero-range bars
    rows += [(105, 105, 105, 105) for _ in range(8)]
    rows += [(106, 106, 106, 106)]
    assert not flag_breakout_retest(_df(rows), is_long=True)[0]


def test_the_band_never_has_zero_width_on_any_accepted_setup():
    for mult in (0.1, 0.25, 0.5, 1.0):
        found, lo, hi, _s, _t = flag_breakout_retest(_bull(), is_long=True, atr_mult=mult)
        if found:
            assert hi > lo, (mult, lo, hi)


def test_the_stop_is_on_the_far_side_of_the_band_from_the_target():
    """Sanity in trading terms: long -> stop below the band, target above it."""
    _f, lo, hi, stop_ref, target = flag_breakout_retest(_bull(), is_long=True)
    assert stop_ref < lo < hi < target
    _f, lo, hi, stop_ref, target = flag_breakout_retest(_bear(), is_long=False)
    assert target < lo < hi < stop_ref


# ── junk input ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [None, "nope", 42])
def test_unreadable_input_yields_nothing(bad):
    assert flag_breakout_retest(bad, is_long=True)[0] is False


def test_a_frame_too_short_to_hold_a_pole_and_flag_yields_nothing():
    assert not flag_breakout_retest(_df([(1, 2, 0.5, 1.5)] * 5), is_long=True)[0]


# ── the feature flag itself ──────────────────────────────────────────────────

def test_the_feature_is_off_by_default():
    """This path has never traded. detect_bull_flag has fired ZERO times in the whole
    log history, so it is unproven in the most literal sense — it must not switch itself
    on for the live bots just because the code landed."""
    import importlib, os
    import bot.strategy
    saved = os.environ.pop("ENABLE_FLAG_CONTINUATION", None)
    try:
        importlib.reload(bot.strategy)
        assert bot.strategy.ENABLE_FLAG_CONTINUATION is False
    finally:
        if saved is not None:
            os.environ["ENABLE_FLAG_CONTINUATION"] = saved
        importlib.reload(bot.strategy)


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("yes", True),
    ("0", False), ("false", False), ("", False),
])
def test_the_flag_reads_the_environment(val, expected):
    import importlib, os
    import bot.strategy
    saved = os.environ.get("ENABLE_FLAG_CONTINUATION")
    os.environ["ENABLE_FLAG_CONTINUATION"] = val
    try:
        importlib.reload(bot.strategy)
        assert bot.strategy.ENABLE_FLAG_CONTINUATION is expected
    finally:
        if saved is None:
            os.environ.pop("ENABLE_FLAG_CONTINUATION", None)
        else:
            os.environ["ENABLE_FLAG_CONTINUATION"] = saved
        importlib.reload(bot.strategy)


def test_the_flag_state_is_cleared_on_reset():
    """flag_stop_ref/flag_target override the stop and the target. Left set after a
    reset they would silently steer the NEXT setup on that symbol, which would be a
    cross-trade contamination bug rather than a visible one."""
    import io as _io, re as _re
    src = _io.open("bot/strategy.py", encoding="utf-8").read()
    reset = src[src.index("def _reset(self, symbol):"):]
    reset = reset[:reset.index("\n    def ", 10)]
    for field in ("flag_stop_ref", "flag_target"):
        assert _re.search(rf"self\.{field}\[symbol\]\s*=\s*None", reset), \
            f"{field} is not cleared in _reset — it would leak into the next setup"


def test_the_accumulation_override_requires_all_three_conditions():
    """Feature on, a confirmed flag, AND a matching daily trend. The guard is the only
    existing behaviour this feature touches, so its exception is pinned here."""
    import io as _io
    src = _io.open("bot/strategy.py", encoding="utf-8").read()
    blk = src[src.index('if htf.get("amd_phase") == "accumulation":'):]
    blk = blk[:blk.index("# ── FIX #1")]
    assert "ENABLE_FLAG_CONTINUATION" in blk
    assert 'daily_trend in ("bullish", "bearish")' in blk
    assert "flag_breakout_retest" in blk
    assert "if not _flag_ok:" in blk and "return" in blk

"""Zone edges come from CLOSED bars on the 5m frame too, not just the 6H one.

drop_forming_candle() was written for exactly this and applied to ONE frame:

    binance_bot.py:1782   df_htf_closed = indicators.drop_forming_candle(df_htf)

The 5m frame never got it. A choch_fvg zone is [c1.high, c3.low] and c3 was allowed to
be the STILL-FORMING bar, so the gap was snapshotted from a low that was still moving.
As that candle finished, its wick extended down into the zone the bot had already armed
— the operator's "the FVG was filled by the 3rd candle, it had a long wick that filled
half of the fvg".

It is the same incident the helper's own docstring describes, on the other timeframe:

    "the stored edge was $6.24; by entry the same finder returned $6.326 because that
     bar had printed a higher high"

and it is this repo's recurring shape — a gate applied to some of the paths that reach
it. fvg_tf covered 3 of 9 arming branches; MIN_FVG_PCT covered 3 of 5 zone producers;
the candle veto covered the 5m cycle and not the 10s sniper.

WHAT DELIBERATELY KEEPS THE LIVE BAR: price, ATR, the tap candle and sweep detection.
Those have to describe now. Only zone DERIVATION moves to closed bars — a stored level
must still mean what it meant when it was stored.
"""
import ast
import io
import re

import pandas as pd
import pytest

from bot.indicators import drop_forming_candle, find_bullish_fvg

SRC = io.open("binance_bot.py", encoding="utf-8").read()


# ── the helper itself ────────────────────────────────────────────────────────

def test_dropping_removes_exactly_the_newest_bar():
    d = pd.DataFrame({"high": [1, 2, 3], "low": [0, 1, 2]})
    assert len(drop_forming_candle(d)) == 2
    assert list(drop_forming_candle(d)["high"]) == [1, 2]


def test_a_moving_last_bar_cannot_change_a_closed_bar_zone():
    """The mechanism, minimally: same closed history, different forming low."""
    base = [(100.0, 100.4, 99.6, 100.0)] * 20
    base += [(100.0, 100.5, 99.9, 100.2), (100.2, 106.0, 100.1, 105.8),
             (105.0, 105.9, 101.0, 105.7)]
    cols = ["open", "high", "low", "close"]
    early = pd.DataFrame(base + [(105.7, 105.8, 105.0, 105.5)], columns=cols)
    later = pd.DataFrame(base + [(105.7, 105.8, 100.6, 100.8)], columns=cols)  # wick grew
    a = find_bullish_fvg(drop_forming_candle(early))
    b = find_bullish_fvg(drop_forming_candle(later))
    assert a == b, "the armed zone moved because a still-forming bar moved"


# ── the wiring ───────────────────────────────────────────────────────────────

def _process_symbol_src():
    t = ast.parse(SRC)
    fn = next(n for n in ast.walk(t) if isinstance(n, ast.FunctionDef)
              and n.name == "process_symbol")
    lines = SRC.splitlines()
    return "\n".join(lines[fn.lineno - 1: fn.end_lineno])


def test_the_closed_5m_frame_exists():
    assert re.search(r"df_ltf_closed\s*=\s*indicators\.drop_forming_candle\(df_ltf\)", SRC)


@pytest.mark.parametrize("fn", ["find_bullish_fvg", "find_bearish_fvg",
                                "detect_displacement_fvg", "find_swing_leg"])
def test_no_zone_deriving_call_still_takes_the_live_5m_frame(fn):
    """These produce STORED levels — a zone edge or an OTE anchor. If any of them is
    handed df_ltf again, the level starts redrawing itself every tick."""
    body = _process_symbol_src()
    bad = re.findall(rf"indicators\.{fn}\(\s*df_ltf\b(?!_closed)", body)
    assert not bad, f"{fn}() is deriving a stored level from the still-forming bar"


def test_the_live_frame_is_still_used_for_things_that_must_describe_now():
    """Guard the other direction: this must not become a blanket swap. Price, ATR and
    the tap candle have to come from the live bar."""
    body = _process_symbol_src()
    assert re.search(r"price\s*=\s*float\(df_ltf\[", body), \
        "live price must still come from the forming bar"
    assert "df_ltf[" in body or "df_ltf." in body, "df_ltf became entirely unused"


def test_the_htf_displacement_floor_survived_the_swap():
    """The 1H-ATR term added earlier must still be on both 5m gate call sites."""
    assert SRC.count("MIN_FVG_PCT, htf_atr=atr_1h") >= 2

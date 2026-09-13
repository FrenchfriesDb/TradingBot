"""The FALLBACK zone detector needs the same size discipline as the strict one.

binance_bot's STEP 2 arms a zone in two ways:

    if disp_found ...:                       <- detect_displacement_fvg, fully gated
    elif state.bias == "BULLISH" and is_fvg_bull:   <- find_bullish_fvg, gated by NOTHING

Tightening only the first branch does not reduce trades — it REROUTES them. After the
2026-09-04 tightening (1.8x ATR body, 0.15% minimum gap), six of six setups in the next
log armed through the fallback instead, and the operator spotted it on the chart:

    "for an FVG to work ... it needs 3 candles, with the middle one being much much
     bigger than the average candle, that you can easily identify it, i don't see that
     here. there isn't even a single 3 candle pattern, nor an FVG bro"

They were right. find_bullish_fvg's true-FVG branch required only `c1.high < c3.low` and
a middle candle that was GREEN — of any size — and its order-block branch required only a
40%-of-range body. Neither is "much bigger than the average candle".
"""
import pandas as pd
import pytest

from bot.indicators import find_bearish_fvg, find_bullish_fvg


def _df(rows):
    pad = [{"open": 100.0, "high": 100.2, "low": 99.8, "close": 100.0} for _ in range(12)]
    return pd.DataFrame(pad + rows)


def _c(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


# ── the true-FVG branch ──

def _tiny_middle():
    """A valid 3-candle gap whose middle candle is microscopic."""
    return _df([_c(100.0, 100.10, 99.90, 100.00),
                _c(100.00, 100.30, 99.95, 100.02),      # green, body 0.02
                _c(100.20, 100.60, 100.15, 100.50)])


def test_ungated_accepts_a_microscopic_middle_candle():
    """Documents the defect: green is enough when there is no size floor."""
    found, lo, hi = find_bullish_fvg(_tiny_middle())
    assert found and (lo, hi) == (100.10, 100.15)


def test_size_floor_rejects_it():
    assert not find_bullish_fvg(_tiny_middle(), min_body_abs=0.30)[0]


def test_a_real_displacement_middle_still_passes():
    df = _df([_c(100.0, 100.10, 99.90, 100.00),
              _c(100.00, 100.90, 99.95, 100.80),        # body 0.80 — identifiable
              _c(100.60, 101.00, 100.40, 100.90)])
    found, lo, hi = find_bullish_fvg(df, min_body_abs=0.30)
    assert found and (lo, hi) == (100.10, 100.40)


def test_gap_width_floor_applies_to_the_fallback_too():
    df = _df([_c(100.0, 100.10, 99.90, 100.00),
              _c(100.00, 100.90, 99.95, 100.80),
              _c(100.20, 101.00, 100.11, 100.90)])      # gap only 0.01 wide
    assert find_bullish_fvg(df, min_body_abs=0.30)[0]
    assert not find_bullish_fvg(df, min_body_abs=0.30, min_gap_abs=0.15)[0]


# ── the order-block branch ──

def test_order_block_body_must_clear_the_floor_as_well():
    """The OB branch only ever required 40% of its OWN range — scale-free, so a doji-sized
    block in dead chop scored the same as a real one."""
    df = _df([_c(100.00, 100.06, 99.96, 99.96),          # bearish OB, body 0.04
              _c(99.97, 100.30, 99.95, 100.25)])         # breaks above its high
    assert find_bullish_fvg(df)[0], "sanity: qualifies with no floor"
    assert not find_bullish_fvg(df, min_body_abs=0.30)[0]


# ── bearish mirror ──

def test_bearish_mirror():
    df = _df([_c(100.00, 100.10, 99.90, 100.00),
              _c(100.00, 100.05, 99.20, 99.30),          # big red middle
              _c(99.40, 99.60, 99.10, 99.20)])
    assert find_bearish_fvg(df, min_body_abs=0.30)[0]
    tiny = _df([_c(100.00, 100.10, 99.90, 100.00),
                _c(100.00, 100.05, 99.20, 99.98),        # body 0.02
                _c(99.40, 99.60, 99.10, 99.20)])
    assert not find_bearish_fvg(tiny, min_body_abs=0.30)[0]


def test_defaults_are_off_so_other_callers_are_unaffected():
    assert find_bullish_fvg(_tiny_middle())[0] is True


def test_a_shorter_gap_keeps_scanning_rather_than_bailing():
    """A rejected candidate must not stop the scan — an older bar may hold a real zone."""
    df = _df([_c(100.00, 100.10, 99.90, 100.00),
              _c(100.00, 100.90, 99.95, 100.80),         # real displacement
              _c(100.60, 101.00, 100.40, 100.90),
              _c(100.50, 100.55, 100.45, 100.52),        # then noise
              _c(100.52, 100.58, 100.50, 100.54)])
    assert find_bullish_fvg(df, min_body_abs=0.30)[0]

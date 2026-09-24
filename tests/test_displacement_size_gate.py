"""min_body_abs — the ABSOLUTE size floor on the displacement candle that arms a zone.

WHY THIS EXISTS (2026-08-22). `min_body_pct` is scale-free: "body >= 40% of its own
range" scores a $0.02 candle in dead chop identically to a $2,000 one. So the ARMING gate
had no size requirement at all, while the TAP-TIME gate (`has_displacement`) demanded
0.6x ATR — a zone could be armed by a bar the confirmation step would reject.

binance_bot defined DISPLACEMENT_ATR_MULT = 0.6 but only ever passed it to
`has_displacement`; it was never wired into `detect_displacement_fvg`. The stock bot had
no absolute size check anywhere in its path.

Default is 0.0 (no floor) so every pre-existing caller keeps its old behaviour.
"""

# ── NOTE, added 2026-09-24 ────────────────────────────────────────────────────
# detect_displacement_fvg now ALSO requires candle 3 to close in the direction of the
# displacement (require_c3_direction, default True). That is a QUALITY filter, not part
# of the geometric definition of an FVG, and the tests below pin the DEFINITION — several
# of them use real candles whose c3 closed against the move, including the real ADA
# 2026-08-06 bars (c3 opened 0.20443 and closed 0.20093, red, after a +6% displacement).
# They therefore pass require_c3_direction=False explicitly: they are asserting "this IS
# an imbalance", while the bot separately asks "is it one worth trading". Both are true,
# and keeping them apart is what stops a future edit from concluding the geometry broke.
# The filter's own behaviour is pinned in tests/test_third_candle_direction.py.

import pandas as pd
import pytest

from bot.indicators import detect_displacement_fvg, range_atr


def _candle(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def _bars(disp_index, prior, disp, nxt, n=20, baseline=100.0):
    rows = [_candle(baseline, baseline, baseline, baseline) for _ in range(n)]
    rows[disp_index - 1] = prior
    rows[disp_index]     = disp
    rows[disp_index + 1] = nxt
    return pd.DataFrame(rows)


# The real ASTER 6H displacement verified live on 2026-08-22: body 84% of range,
# +9% move, gap 0.6810 -> 0.6910 (1.47%).
ASTER_PRIOR = _candle(0.6650, 0.6810, 0.6600, 0.6750)
ASTER_DISP  = _candle(0.6750, 0.7440, 0.6710, 0.7360)
ASTER_NEXT  = _candle(0.7360, 0.7480, 0.6910, 0.7040)


def _aster_df():
    return _bars(15, ASTER_PRIOR, ASTER_DISP, ASTER_NEXT, baseline=0.665)


def test_default_behaviour_is_unchanged():
    """No floor passed -> identical to before this parameter existed."""
    found, direction, lo, hi, _ = detect_displacement_fvg(_aster_df(), require_c3_direction=False)
    assert found is True and direction == "bullish"
    assert lo == pytest.approx(0.6810)
    assert hi == pytest.approx(0.6910)


def test_a_genuinely_big_candle_still_passes_a_floor():
    """ASTER's body is 0.0610 — comfortably past any sane ATR-based floor."""
    body = abs(ASTER_DISP["close"] - ASTER_DISP["open"])
    found, _, _, _, _ = detect_displacement_fvg(_aster_df(), min_body_abs=body * 0.5, require_c3_direction=False)
    assert found is True, "a 9% displacement was rejected by a floor half its own body"


def test_floor_above_the_body_rejects_it():
    """The whole point: a bar too small in absolute terms must not arm a zone."""
    body = abs(ASTER_DISP["close"] - ASTER_DISP["open"])
    found, direction, lo, hi, _ = detect_displacement_fvg(_aster_df(),
                                                          min_body_abs=body * 1.5)
    assert found is False
    assert (direction, lo, hi) == (None, None, None)


def test_tiny_candle_passes_ratio_gate_but_fails_size_gate():
    """The exact hole this closes. This bar's body is 60% of its range — it sails past
    min_body_pct — but it is minuscule in absolute terms. Scale-free ratios cannot tell
    the difference; min_body_abs can."""
    # Must still clear the swing by 0.15%, so the bar cannot be arbitrarily small —
    # but it can be far smaller than a real momentum bar while scoring 75% on ratio.
    prior = _candle(99.00, 99.00, 98.98, 98.99)
    disp  = _candle(98.99, 99.25, 98.97, 99.20)   # body 0.21, range 0.28 -> 75%
    nxt   = _candle(99.20, 99.30, 99.05, 99.28)   # C3.low 99.05 > C1.high 99.00
    df = _bars(15, prior, disp, nxt, baseline=99.00)

    found_no_floor, direction, lo, hi, _ = detect_displacement_fvg(df)
    assert found_no_floor is True, "setup should qualify on ratio alone (that IS the bug)"
    assert direction == "bullish"
    assert (lo, hi) == pytest.approx((99.00, 99.05))

    # 0.6 x ATR on a normally-ranging instrument would sit well above a 0.21 body.
    found_floored, _, _, _, _ = detect_displacement_fvg(df, min_body_abs=0.30)
    assert found_floored is False, "tiny bar still armed a zone despite the absolute floor"


def test_range_atr_returns_zero_rather_than_nan_on_short_series():
    """Callers pass range_atr() straight into min_body_abs. A NaN would silently make
    every comparison False and disable detection, so it must degrade to 'no floor'."""
    short = pd.DataFrame([_candle(10, 11, 9, 10) for _ in range(3)])
    assert range_atr(short) == 0.0
    assert range_atr(pd.DataFrame()) == 0.0


def test_range_atr_computes_mean_high_low_range():
    df = pd.DataFrame([_candle(10, 12, 8, 11) for _ in range(20)])   # range 4 every bar
    assert range_atr(df) == pytest.approx(4.0)

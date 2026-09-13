"""detect_displacement_fvg — the standard 3-candle Fair Value Gap.

DEFINITION (bullish): candles C1, C2, C3 where C1.high < C3.low. The gap is
[C1.high, C3.low]. C2 is the displacement candle, and it necessarily TRADES THROUGH
that range — a single candle spans its own low to its own high, and that violent
traversal is precisely what creates the imbalance: price moved so fast that the
neighbouring candles left no overlapping trade. The gap counts as unfilled until price
RETURNS to it later. Bearish is the mirror (C1.low > C3.high).

REGRESSION GUARD (2026-08-19): a previous "fix" added `C2.low >= C1.high` (and the
bearish mirror) on the theory that a displacement candle wicking into its own gap
invalidated it. That is NOT the definition, and it silently disabled detection almost
everywhere — in 24/7 crypto there is essentially never a true price gap between
consecutive candles, so C2.low is nearly always at or below C1.high. Proof it mattered:
BTC's real +5.75% 6h displacement on 2026-08-19 left a $3,521 gap and was REJECTED
because C2's low sat $72 under C1's high. Those real candles are pinned below.
"""
import pandas as pd
import pytest

from bot.indicators import detect_displacement_fvg


def _candle(open_, high, low, close):
    return {"open": open_, "high": high, "low": low, "close": close}


def _bars(disp_index, prior, disp, nxt, n=20, baseline=100.0):
    """Flat baseline candles with the 3-candle pattern inserted. Everything after `nxt`
    stays flat so no later pattern wins the newest-first scan."""
    rows = [_candle(baseline, baseline, baseline, baseline) for _ in range(n)]
    rows[disp_index - 1] = prior
    rows[disp_index]     = disp
    rows[disp_index + 1] = nxt
    return pd.DataFrame(rows)


# ─────────────── the real BTC spike that exposed the regression ───────────────
def test_real_btc_2026_08_19_displacement_is_detected():
    """+5.75% 6h candle leaving a $3,521 gap. C2.low is $72 BELOW C1.high — normal,
    and must not disqualify it."""
    prior = _candle(64186, 64489, 64112, 64459)
    disp  = _candle(64459, 69698, 64417, 68168)
    nxt   = _candle(68168, 70022, 68010, 69300)
    df = _bars(15, prior, disp, nxt, baseline=64300.0)
    found, direction, lo, hi, broken = detect_displacement_fvg(df)
    assert found is True, "textbook FVG rejected — the C2.low>=C1.high regression is back"
    assert direction == "bullish"
    assert lo == pytest.approx(64489)
    assert hi == pytest.approx(68010)


def test_real_ada_2026_08_06_displacement_is_also_a_valid_fvg():
    """The trade that triggered the bad 'fix'. Body/range 57%, real gap 0.19289-0.19935.
    C2.low (0.19006) below C1.high (0.19289) is normal, not disqualifying."""
    prior = _candle(0.18815, 0.19289, 0.18760, 0.19207)
    disp  = _candle(0.19220, 0.21146, 0.19006, 0.20437)
    nxt   = _candle(0.20443, 0.20740, 0.19935, 0.20093)
    df = _bars(15, prior, disp, nxt, baseline=0.1900)
    found, direction, lo, hi, _ = detect_displacement_fvg(df)
    assert found is True
    assert direction == "bullish"
    assert (lo, hi) == (pytest.approx(0.19289), pytest.approx(0.19935))


# ───────────────────────────── core definition ─────────────────────────────
def test_bullish_gap_requires_c1_high_below_c3_low():
    prior = _candle(100.0, 100.9, 99.8, 100.3)
    disp  = _candle(100.5, 103.5, 100.4, 103.0)     # body 2.5 / range 3.1 = 81%
    nxt   = _candle(102.0, 102.5, 101.2, 102.2)
    found, direction, lo, hi, _ = detect_displacement_fvg(_bars(15, prior, disp, nxt))
    assert found is True and direction == "bullish"
    assert (lo, hi) == (pytest.approx(100.9), pytest.approx(101.2))


def test_no_gap_when_c1_and_c3_overlap():
    """C3 trades back down into C1's range — the imbalance was filled immediately."""
    prior = _candle(100.0, 100.9, 99.8, 100.3)
    disp  = _candle(100.5, 103.5, 100.4, 103.0)
    nxt   = _candle(102.0, 102.5, 100.5, 101.0)     # low 100.5 < prior high 100.9
    found, *_ = detect_displacement_fvg(_bars(15, prior, disp, nxt))
    assert found is False


def test_bearish_gap_is_the_mirror():
    prior = _candle(101.0, 101.2, 100.0, 100.5)
    disp  = _candle(100.2, 100.4, 97.5, 98.0)       # body 2.2 / range 2.9 = 76%
    nxt   = _candle(99.0, 99.5, 98.6, 99.0)
    found, direction, lo, hi, _ = detect_displacement_fvg(_bars(15, prior, disp, nxt))
    assert found is True and direction == "bearish"
    assert (lo, hi) == (pytest.approx(99.5), pytest.approx(100.0))


def test_weak_middle_candle_is_not_displacement():
    """A doji-ish middle candle fails the body/range floor even with a gap present."""
    prior = _candle(100.0, 100.9, 99.8, 100.3)
    disp  = _candle(101.0, 103.5, 100.4, 101.2)     # body 0.2 / range 3.1 = 6%
    nxt   = _candle(102.0, 102.5, 101.2, 102.2)
    found, *_ = detect_displacement_fvg(_bars(15, prior, disp, nxt))
    assert found is False


def test_too_little_data_is_safe():
    assert detect_displacement_fvg(pd.DataFrame([_candle(1, 1, 1, 1)])) == (False, None, None, None, None)

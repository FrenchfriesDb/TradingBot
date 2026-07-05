import pandas as pd
import pytest

from test_bot import (
    compute_pools, detect_sweep, compute_stop_target, compute_rr, size_position,
)


def _candles(highs, lows):
    return pd.DataFrame({"high": highs, "low": lows})


def test_compute_pools_uses_lookback_window():
    # 30 candles, but lookback=24 should ignore the oldest 6
    highs = [100] * 6 + [200] * 24
    lows  = [50]  * 6 + [150] * 24
    df = _candles(highs, lows)
    pool_high, pool_low = compute_pools(df, lookback=24)
    assert pool_high == 200
    assert pool_low == 150


def test_detect_sweep_short_on_wick_above_and_close_back_in():
    result = detect_sweep(candle_high=105, candle_low=99, candle_close=98,
                           pool_high=100, pool_low=90)
    assert result == "SHORT"


def test_detect_sweep_long_on_wick_below_and_close_back_in():
    result = detect_sweep(candle_high=95, candle_low=85, candle_close=92,
                           pool_high=100, pool_low=90)
    assert result == "LONG"


def test_detect_sweep_none_when_no_wick_past_pool():
    result = detect_sweep(candle_high=99, candle_low=91, candle_close=95,
                           pool_high=100, pool_low=90)
    assert result is None


def test_detect_sweep_none_when_wick_past_but_closes_outside():
    # Wicks above pool_high but closes above it too — a breakout, not a reversal
    result = detect_sweep(candle_high=105, candle_low=101, candle_close=103,
                           pool_high=100, pool_low=90)
    assert result is None


def test_compute_stop_target_short():
    sl, tp = compute_stop_target("SHORT", candle_high=105, candle_low=99,
                                  pool_high=100, pool_low=90, sl_buffer_pct=0.0005)
    assert sl == pytest.approx(105 * 1.0005)
    assert tp == 90


def test_compute_stop_target_long():
    sl, tp = compute_stop_target("LONG", candle_high=95, candle_low=85,
                                  pool_high=100, pool_low=90, sl_buffer_pct=0.0005)
    assert sl == pytest.approx(85 * 0.9995)
    assert tp == 100


def test_compute_rr_basic():
    rr = compute_rr(entry=100, sl=90, tp=130)
    assert rr == pytest.approx(3.0)


def test_compute_rr_zero_risk_returns_zero():
    assert compute_rr(entry=100, sl=100, tp=130) == 0.0


def test_size_position_risks_exact_percent_of_balance():
    qty = size_position(balance=10_000, risk_pct=0.01, entry=100, sl=95)
    # risk_per_unit = 5, risk_dollars = 100 -> qty = 20
    assert qty == pytest.approx(20.0)


def test_size_position_zero_when_risk_non_positive():
    assert size_position(balance=10_000, risk_pct=0.01, entry=100, sl=100) == 0.0

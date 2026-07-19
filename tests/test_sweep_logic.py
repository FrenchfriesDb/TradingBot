import pandas as pd
import pytest

from test_bot import (
    compute_pools, detect_sweep, compute_stop_target, compute_rr, size_position,
    PaperTrader,
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
    # entry=40 (not 100) keeps notional ($800) well under the 10% notional cap
    # ($1,000 on a $10k balance) — isolates the risk-based formula from capping,
    # which has its own dedicated test below.
    qty = size_position(balance=10_000, risk_pct=0.01, entry=40, sl=35)
    # risk_per_unit = 5, risk_dollars = 100 -> qty = 20
    assert qty == pytest.approx(20.0)


def test_size_position_zero_when_risk_non_positive():
    assert size_position(balance=10_000, risk_pct=0.01, entry=100, sl=100) == 0.0


def test_size_position_caps_notional():
    # Tight stop on an expensive coin: uncapped risk-sizing would demand ~5x the
    # account in notional ($24.6k on $5k). The cap must hold it to 10% of balance.
    qty = size_position(balance=5_000, risk_pct=0.01, entry=60_000, sl=59_880)
    assert qty * 60_000 <= 5_000 * 0.10 + 1e-6
    assert qty == pytest.approx(5_000 * 0.10 / 60_000, rel=1e-3)


def test_daily_cap_full_room_on_first_trade():
    pt = PaperTrader(balance=5_000)
    assert pt.check_daily_cap(notional_needed=150, daily_cap_dollars=200) == 200


def test_daily_cap_shrinks_after_recording():
    pt = PaperTrader(balance=5_000)
    pt.record_notional(120)
    assert pt.check_daily_cap(notional_needed=150, daily_cap_dollars=200) == pytest.approx(80)


def test_daily_cap_not_released_when_a_trade_closes():
    # record_notional tracks $ OPENED today, not concurrent exposure — a closed
    # trade must NOT free up room, matching binance_bot.py's PaperTrader semantics.
    pt = PaperTrader(balance=5_000)
    pt.record_notional(200)   # one trade opened for the full daily cap
    assert pt.check_daily_cap(notional_needed=10, daily_cap_dollars=200) == 0


def test_daily_cap_resets_on_a_new_utc_day():
    pt = PaperTrader(balance=5_000)
    pt.record_notional(200)
    assert pt.check_daily_cap(notional_needed=10, daily_cap_dollars=200) == 0
    # Simulate the clock rolling over to a new UTC day.
    import datetime as _dt
    pt.daily_date = _dt.date(2000, 1, 1)
    assert pt.check_daily_cap(notional_needed=10, daily_cap_dollars=200) == 200

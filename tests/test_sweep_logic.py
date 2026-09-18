import pandas as pd
import pytest

from datetime import datetime, timedelta, timezone

from test_bot import (
    compute_pools, detect_sweep, compute_stop_target, compute_rr, size_position,
    PaperTrader, is_trade_stale,
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


def test_compute_stop_target_short_falls_back_to_buffer_without_atr():
    # atr_value=0 -> stop uses the fixed-buffer floor beyond the wick
    sl, tp = compute_stop_target("SHORT", candle_high=105, candle_low=99,
                                  pool_high=100, pool_low=90, sl_buffer_pct=0.0005)
    assert sl == pytest.approx(105 + 105 * 0.0005)
    assert tp == 90


def test_compute_stop_target_long_falls_back_to_buffer_without_atr():
    sl, tp = compute_stop_target("LONG", candle_high=95, candle_low=85,
                                  pool_high=100, pool_low=90, sl_buffer_pct=0.0005)
    assert sl == pytest.approx(85 - 85 * 0.0005)
    assert tp == 100


def test_compute_stop_target_uses_atr_buffer_when_wider():
    # ATR buffer (2.0) beats the fixed buffer (~0.05) -> stop sits 2.0 beyond the wick
    sl, tp = compute_stop_target("SHORT", candle_high=105, candle_low=99,
                                  pool_high=100, pool_low=90, sl_buffer_pct=0.0005,
                                  atr_value=2.0)
    assert sl == pytest.approx(105 + 2.0)   # STOP_ATR_MULT defaults to 1.0
    # and a long places it below the low by the same ATR buffer
    sl_l, _ = compute_stop_target("LONG", candle_high=95, candle_low=85,
                                   pool_high=100, pool_low=90, atr_value=2.0)
    assert sl_l == pytest.approx(85 - 2.0)


def test_atr_computes_average_true_range():
    from test_bot import atr
    # simple candles [ts,o,h,l,c,v]; ranges 2 each -> ATR 2.0
    candles = [[0,10,11,9,10,0],[1,10,12,10,11,0],[2,11,13,11,12,0],[3,12,14,12,13,0]]
    assert atr(candles, length=3) == pytest.approx(2.0)
    assert atr([], length=14) == 0.0   # empty -> 0, caller uses buffer floor


def test_resample_ohlcv_1h_to_4h():
    from test_bot import resample_ohlcv
    HOUR = 3600 * 1000
    # 8 hourly candles aligned to epoch -> two 4h buckets
    candles = [[i * HOUR, 10, 10 + i, 5, 10 + i, 1] for i in range(8)]
    out = resample_ohlcv(candles, 4 * 3600)
    assert len(out) == 2
    # bucket 1 = hours 0-3: high=max(10..13)=13, low=5, close=13, vol=4
    assert out[0][2] == 13 and out[0][3] == 5 and out[0][4] == 13 and out[0][5] == 4
    # bucket 2 = hours 4-7: high=17, close=17
    assert out[1][2] == 17 and out[1][4] == 17


def test_trend_direction_up_down_and_insufficient():
    from test_bot import trend_direction
    def candles(closes):  # [ts,o,h,l,c,v]
        return [[i, c, c, c, c, 0] for i, c in enumerate(closes)]
    # rising series -> last close above its EMA -> UP
    assert trend_direction(candles([float(i) for i in range(60)]), ema_len=50) == "UP"
    # falling series -> last close below its EMA -> DOWN
    assert trend_direction(candles([float(60 - i) for i in range(60)]), ema_len=50) == "DOWN"
    # too little data -> None (no filter)
    assert trend_direction(candles([1.0, 2.0, 3.0]), ema_len=50) is None


def test_trend_filter_blocks_counter_trend_only():
    # the rule the bot applies: block SHORT in UP, block LONG in DOWN; allow the rest
    def blocked(trend, direction):
        return (trend == "UP" and direction == "SHORT") or (trend == "DOWN" and direction == "LONG")
    assert blocked("UP", "SHORT") is True      # fading an uptrend — blocked
    assert blocked("DOWN", "LONG") is True     # catching a falling knife — blocked
    assert blocked("UP", "LONG") is False      # buy-the-dip with trend — allowed
    assert blocked("DOWN", "SHORT") is False   # sell-the-rip with trend — allowed
    assert blocked(None, "SHORT") is False     # no trend data — allowed


def test_compute_rr_basic():
    rr = compute_rr(entry=100, sl=90, tp=130)
    assert rr == pytest.approx(3.0)


def test_compute_rr_zero_risk_returns_zero():
    assert compute_rr(entry=100, sl=100, tp=130) == 0.0


def test_size_position_risks_exact_percent_of_balance():
    # A wide stop (entry=40, sl=10 -> 30 wide) keeps risk-based notional ($133) under
    # the per-trade notional cap regardless of MAX_MARGIN_PCT, isolating the risk-based
    # formula from capping (capping has its own dedicated test below).
    qty = size_position(balance=10_000, risk_pct=0.01, entry=40, sl=10)
    # risk_per_unit = 30, risk_dollars = 100 -> qty = 3.3333
    assert qty == pytest.approx(100 / 30, rel=1e-4)
    # and the realized loss at the stop is exactly 1% of the account
    assert qty * (40 - 10) == pytest.approx(100.0, rel=1e-4)


def test_size_position_zero_when_risk_non_positive():
    assert size_position(balance=10_000, risk_pct=0.01, entry=100, sl=100) == 0.0


def test_size_position_caps_notional():
    # Tight stop on an expensive coin: uncapped risk-sizing would demand ~5x the
    # account in notional ($24.6k on $5k). The cap must hold notional to
    # MAX_MARGIN_PCT of balance in margin at TEST_LEVERAGE (tracks the constants
    # so sizing policy changes don't break the test).
    from test_bot import MAX_MARGIN_PCT, TEST_LEVERAGE
    max_notional = 5_000 * MAX_MARGIN_PCT * TEST_LEVERAGE
    qty = size_position(balance=5_000, risk_pct=0.01, entry=60_000, sl=59_880)
    assert qty * 60_000 <= max_notional + 1e-6
    assert qty == pytest.approx(max_notional / 60_000, rel=1e-3)


# The daily cap counts RISK, not deployed capital, as of 2026-09-18. Denominating it in
# notional silently undid risk-based sizing: a tighter stop bought a bigger position,
# hit the notional ceiling sooner, and so ended up risking LESS — $4 behind a 0.8% stop
# against a $50 budget. See tests/test_daily_risk_budget.py. The three properties below
# are unchanged and are the ones worth keeping: a full budget on a fresh day, no release
# when a trade closes, and a reset on the day roll.
DAILY_PCT = 0.04          # 4% of a $5,000 balance = a $200 budget


def test_daily_risk_budget_full_room_on_first_trade():
    pt = PaperTrader(balance=5_000)
    assert pt.risk_remaining(5_000, DAILY_PCT) == 200


def test_daily_risk_budget_shrinks_after_recording():
    pt = PaperTrader(balance=5_000)
    pt.record_risk(120)
    assert pt.risk_remaining(5_000, DAILY_PCT) == pytest.approx(80)


def test_daily_risk_budget_not_released_when_a_trade_closes():
    # record_risk tracks risk COMMITTED today, not concurrent exposure — a closed trade
    # must NOT free up room, or a losing day can keep re-spending the same budget.
    pt = PaperTrader(balance=5_000)
    pt.record_risk(200)       # one trade taking the whole day's budget
    assert pt.risk_remaining(5_000, DAILY_PCT) == 0


def test_daily_risk_budget_resets_on_a_new_day():
    pt = PaperTrader(balance=5_000)
    pt.record_risk(200)
    assert pt.risk_remaining(5_000, DAILY_PCT) == 0
    import datetime as _dt
    pt.daily_date = _dt.date(2000, 1, 1)      # roll the clock to a new day
    assert pt.risk_remaining(5_000, DAILY_PCT) == 200


def test_the_budget_tracks_the_account_rather_than_a_fixed_dollar_amount():
    """After a drawdown the day's allowance shrinks with the balance."""
    pt = PaperTrader(balance=5_000)
    assert pt.risk_remaining(2_500, DAILY_PCT) == 100


# ─────────────────────────── is_trade_stale ───────────────────────────
# REAL INCIDENT 2026-08-09: test_bot.py had NO time-based exit anywhere (only
# sl_hit/tp_hit checks) — an ADA LONG sat open 48.9 hours doing nothing before the
# user noticed. binance_bot.py already has this exact concept (STALE_TRADE_HOURS).
def test_is_trade_stale_false_before_the_threshold():
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    entry = now - timedelta(hours=3, minutes=59)
    assert is_trade_stale(entry, now, stale_hours=4) is False


def test_is_trade_stale_true_at_the_threshold():
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    entry = now - timedelta(hours=4)
    assert is_trade_stale(entry, now, stale_hours=4) is True


def test_is_trade_stale_true_well_past_the_threshold():
    # the real incident: a position open 48.9 hours against a 4h threshold
    now = datetime(2026, 8, 9, 15, 28, tzinfo=timezone.utc)
    entry = now - timedelta(hours=48, minutes=54)
    assert is_trade_stale(entry, now, stale_hours=4) is True


def test_is_trade_stale_false_with_no_entry_time():
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    assert is_trade_stale(None, now, stale_hours=4) is False

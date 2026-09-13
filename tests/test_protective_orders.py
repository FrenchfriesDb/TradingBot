"""Regression target: BTCUSD never had a stop-loss since its first fill on 2026-07-22.
Root cause — bot/strategy.py's bracket-entry and _ensure_protection code both submit a
stop_loss leg as {"stop_price": ...} with no limit_price. Alpaca accepts that for EQUITIES
but REJECTS it for CRYPTO with 422 "invalid order type for crypto order" (confirmed live:
a bare {"type":"stop", "stop_price":...} submission for BTC/USD failed with that exact
error). For the bracket-entry path this exception is silently caught and falls back to a
naked market order with zero protection — meaning every crypto entry has likely never
gotten a real stop-loss. build_protective_leg() is the crypto-aware fix: crypto gets a
stop_limit leg (stop_price + a nearby limit_price so it still fills promptly); equities
keep the existing bare stop_price leg unchanged."""
import pytest

from bot.indicators import build_protective_leg


def test_equity_leg_is_unchanged_bare_stop():
    leg = build_protective_leg(205.78, is_crypto=False, is_long=False)
    assert leg == {"stop_price": "205.78"}


def test_crypto_short_close_buy_stop_gets_limit_above():
    # covering a SHORT = BUY stop; limit must sit ABOVE stop so it still fills as
    # price keeps rising past the trigger (a limit price below the stop would never fill).
    leg = build_protective_leg(342.76, is_crypto=True, is_long=False, buffer_pct=0.005)
    assert leg["stop_price"] == "342.76"
    assert float(leg["limit_price"]) == pytest.approx(342.76 * 1.005, abs=1e-6)


def test_crypto_long_close_sell_stop_gets_limit_below():
    # closing a LONG = SELL stop; limit must sit BELOW stop so it still fills as
    # price keeps falling past the trigger.
    leg = build_protective_leg(63542.65, is_crypto=True, is_long=True, buffer_pct=0.005)
    assert leg["stop_price"] == "63542.65"
    assert float(leg["limit_price"]) == pytest.approx(63542.65 * 0.995, abs=1e-6)


def test_crypto_sub_dollar_price_keeps_precision():
    # POL-style sub-$1 prices must not get rounded away to 2dp (0.0757 -> 0.08 would be
    # a wildly wrong stop for a penny-priced coin).
    leg = build_protective_leg(0.075725, is_crypto=True, is_long=True, buffer_pct=0.005)
    assert leg["stop_price"] == "0.075725"
    assert float(leg["limit_price"]) == pytest.approx(0.075725 * 0.995, abs=1e-9)


def test_default_buffer_is_half_a_percent():
    leg = build_protective_leg(100.0, is_crypto=True, is_long=True)
    assert float(leg["limit_price"]) == pytest.approx(99.5, abs=1e-6)

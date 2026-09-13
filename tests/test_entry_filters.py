"""Entry-quality filters for the crypto bot's continuation setups (wedge/chase/BOS):

- has_displacement: only fire when a real momentum candle just printed, so the bot
  stops entering weak breakouts into chop that then bleed sideways for 6h.
- blocked_by_overhead: don't go long directly under a near resistance pool (or short
  right above near support) — no room to run to target.

Both pure functions so the geometry is provable without live data."""
import pytest

from bot.indicators import has_displacement, blocked_by_overhead


# ─────────────────────────────── has_displacement ────────────────────────────
# candles are (open, high, low, close), oldest first.
def test_strong_bull_body_is_long_displacement():
    # body 4.5 of range 5.5 = 82% > 50%, closes up -> displacement for a long
    candles = [(100, 105, 99.5, 104.5)]
    assert has_displacement(candles, is_long=True, min_body_frac=0.5) is True


def test_doji_is_not_displacement():
    # body 0.1 of range 4 = 2.5% -> indecision, not momentum
    candles = [(100, 102, 98, 100.1)]
    assert has_displacement(candles, is_long=True, min_body_frac=0.5) is False


def test_wrong_direction_body_is_not_long_displacement():
    # strong body but it's a DOWN candle -> not a long displacement
    candles = [(104.5, 105, 99.5, 100)]
    assert has_displacement(candles, is_long=True, min_body_frac=0.5) is False


def test_strong_down_body_is_short_displacement():
    candles = [(104.5, 105, 99.5, 100)]
    assert has_displacement(candles, is_long=False, min_body_frac=0.5) is True


def test_small_body_below_absolute_floor_is_not_displacement():
    # 80% body frac but only $0.4 move; min_body_abs=1.0 (e.g. 0.6x ATR) rejects it
    candles = [(100, 100.5, 100.0, 100.4)]
    assert has_displacement(candles, is_long=True, min_body_frac=0.5, min_body_abs=1.0) is False


def test_displacement_found_within_lookback_not_only_last_bar():
    candles = [
        (100, 105, 99.5, 104.5),   # strong (this one qualifies)
        (104.5, 104.8, 104.2, 104.4),  # weak
        (104.4, 104.6, 104.1, 104.3),  # weak
    ]
    assert has_displacement(candles, is_long=True, min_body_frac=0.5, lookback=3) is True


def test_no_displacement_when_all_recent_bars_weak():
    candles = [
        (100, 100.5, 99.5, 100.1),
        (100.1, 100.6, 99.6, 100.2),
        (100.2, 100.7, 99.7, 100.3),
    ]
    assert has_displacement(candles, is_long=True, min_body_frac=0.5, lookback=3) is False


# ────────────────────────────── blocked_by_overhead ──────────────────────────
def test_long_blocked_when_resistance_pool_too_close():
    # nearest overhead pool only $0.5 above entry, need $1 of room -> blocked
    assert blocked_by_overhead(entry=100, is_long=True, opposing_pool=100.5, min_room=1.0) is True


def test_long_ok_when_resistance_pool_has_room():
    assert blocked_by_overhead(entry=100, is_long=True, opposing_pool=103, min_room=1.0) is False


def test_long_not_blocked_when_no_overhead_pool():
    assert blocked_by_overhead(entry=100, is_long=True, opposing_pool=None, min_room=1.0) is False


def test_long_ignores_pool_below_entry():
    # a pool BELOW entry isn't overhead resistance for a long
    assert blocked_by_overhead(entry=100, is_long=True, opposing_pool=99, min_room=1.0) is False


def test_short_blocked_when_support_pool_too_close():
    assert blocked_by_overhead(entry=100, is_long=False, opposing_pool=99.5, min_room=1.0) is True


def test_short_ok_when_support_pool_has_room():
    assert blocked_by_overhead(entry=100, is_long=False, opposing_pool=97, min_room=1.0) is False

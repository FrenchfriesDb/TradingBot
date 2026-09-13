"""Unit tests for structural stop/target math (Lever: stops outside noise, TP at real
liquidity). Pure functions — no live data — so the R:R geometry is provably correct."""
import pandas as pd
import pytest

from bot.indicators import (
    structural_stop_price, structural_take_profit, crypto_zone_stop_level,
    find_supply_zone, find_demand_zone,
)


# ─────────────────────────── structural_stop_price ───────────────────────────
def test_stop_uses_swing_when_wider_than_noise_floor():
    # long: swing low $1.99 below entry, noise floor 1.5*ATR(1.0)=1.5 -> swing wins
    sl = structural_stop_price(entry=207.59, swing=205.60, atr_ref=1.0,
                               is_long=True, min_atr_mult=1.5, liq_cap_dist=None)
    assert sl == pytest.approx(205.60, abs=1e-6)


def test_stop_widens_a_too_tight_swing_to_the_noise_floor():
    # long: swing only $0.49 away (inside noise) -> widened to 1.5*ATR=1.5 -> 206.09
    sl = structural_stop_price(entry=207.59, swing=207.10, atr_ref=1.0,
                               is_long=True, min_atr_mult=1.5, liq_cap_dist=None)
    assert sl == pytest.approx(206.09, abs=1e-6)


def test_stop_is_capped_at_the_liquidation_distance():
    # long: swing $17.59 away but liq cap is $5 -> stop no further than 202.59
    sl = structural_stop_price(entry=207.59, swing=190.0, atr_ref=1.0,
                               is_long=True, min_atr_mult=1.5, liq_cap_dist=5.0)
    assert sl == pytest.approx(202.59, abs=1e-6)


def test_short_stop_sits_above_entry():
    sl = structural_stop_price(entry=207.59, swing=209.60, atr_ref=1.0,
                               is_long=False, min_atr_mult=1.5, liq_cap_dist=None)
    assert sl == pytest.approx(209.60, abs=1e-6)


def test_stop_without_a_swing_falls_back_to_noise_floor():
    sl = structural_stop_price(entry=207.59, swing=None, atr_ref=1.0,
                               is_long=True, min_atr_mult=1.5, liq_cap_dist=None)
    assert sl == pytest.approx(206.09, abs=1e-6)


# ────────────────────────── structural_take_profit ───────────────────────────
def test_tp_pins_to_liquidity_pool_within_rr_band():
    # pool exactly 2R away, band [2,4] -> TP sits ON the pool
    tp = structural_take_profit(entry=207.59, stop_dist=2.0, pool=211.59,
                                is_long=True, min_rr=2.0, max_rr=4.0)
    assert tp == pytest.approx(211.59, abs=1e-6)


def test_tp_caps_at_max_rr_when_pool_too_far_intraday():
    # pool 7.5R away -> capped to 4R = entry + 4*2 = 215.59 (reachable), not the far pool
    tp = structural_take_profit(entry=207.59, stop_dist=2.0, pool=222.59,
                                is_long=True, min_rr=2.0, max_rr=4.0)
    assert tp == pytest.approx(215.59, abs=1e-6)


def test_tp_ignores_pool_closer_than_min_rr():
    # pool only 0.7R away (in the noise) -> ignore it, use the 2R floor -> 211.59
    tp = structural_take_profit(entry=207.59, stop_dist=2.0, pool=209.0,
                                is_long=True, min_rr=2.0, max_rr=4.0)
    assert tp == pytest.approx(211.59, abs=1e-6)


def test_tp_falls_back_to_min_rr_without_a_pool():
    tp = structural_take_profit(entry=207.59, stop_dist=2.0, pool=None,
                                is_long=True, min_rr=2.0, max_rr=4.0)
    assert tp == pytest.approx(211.59, abs=1e-6)


def test_short_tp_pins_below_entry():
    tp = structural_take_profit(entry=207.59, stop_dist=2.0, pool=203.59,
                                is_long=False, min_rr=2.0, max_rr=4.0)
    assert tp == pytest.approx(203.59, abs=1e-6)


# ──────────────────────────── crypto_zone_stop_level ──────────────────────────
# Combines the FVG/OB zone edge (+ATR breathing room) with the swing-guard into a
# single candidate stop LEVEL — mirrors binance_bot.py's zone+swing logic (previously
# duplicated across its sniper-arm and retest code paths). This is the "swing" input
# later floored against 1H-ATR noise by structural_stop_price.
def test_zone_level_long_uses_zone_edge_minus_breathing_room():
    # zone_low=100, breathing=1.5 -> zone_sl=98.5; swing (96) is FARTHER -> swing wins
    lvl = crypto_zone_stop_level(fill_price=101, is_long=True, zone_edge=100,
                                 breathing_room=1.5, swing_extreme=96)
    assert lvl == pytest.approx(96, abs=1e-6)


def test_zone_level_long_prefers_zone_when_swing_is_tighter():
    # swing (99, closer than zone_sl=98.5) never TIGHTENS the stop — zone_sl wins
    lvl = crypto_zone_stop_level(fill_price=101, is_long=True, zone_edge=100,
                                 breathing_room=1.5, swing_extreme=99)
    assert lvl == pytest.approx(98.5, abs=1e-6)


def test_zone_level_long_falls_back_when_entry_already_through_zone():
    # a chase/momentum entry at 98.3 — BELOW zone_edge(100) - breathing_room(1.5) = 98.5,
    # so the normal zone_sl (98.5) would sit AT/ABOVE entry (nonsensical stop) ->
    # fall back to entry - breathing = 98.3 - 1.5 = 96.8
    lvl = crypto_zone_stop_level(fill_price=98.3, is_long=True, zone_edge=100,
                                 breathing_room=1.5, swing_extreme=None)
    assert lvl == pytest.approx(96.8, abs=1e-6)


def test_zone_level_short_uses_zone_edge_plus_breathing_room():
    lvl = crypto_zone_stop_level(fill_price=99, is_long=False, zone_edge=100,
                                 breathing_room=1.5, swing_extreme=104)
    assert lvl == pytest.approx(104, abs=1e-6)


def test_zone_level_without_swing_uses_zone_only():
    lvl = crypto_zone_stop_level(fill_price=101, is_long=True, zone_edge=100,
                                 breathing_room=1.5, swing_extreme=None)
    assert lvl == pytest.approx(98.5, abs=1e-6)


# ─────────────── integration: zone level + HTF floor (the real port) ──────────
def test_avax_example_stop_widens_outside_1h_noise():
    # Real 7/23 AVAX trade: entry 6.44, OLD stop 6.3836 (0.88% away) via 5m ATR alone.
    # With a 1H ATR of ~0.08 (typical AVAX volatility) and a 1.5x floor, the stop
    # should widen to sit outside a single 1H bar's normal range.
    zone_lvl = crypto_zone_stop_level(fill_price=6.44, is_long=True, zone_edge=6.46,
                                      breathing_room=0.03, swing_extreme=6.40)
    sl = structural_stop_price(entry=6.44, swing=zone_lvl, atr_ref=0.08,
                               is_long=True, min_atr_mult=1.5, liq_cap_dist=None)
    assert sl < 6.44 - 1.5 * 0.08 + 1e-9   # at least the noise floor
    assert sl <= 6.40                       # never tighter than real structure


# ─────────────── find_supply_zone / find_demand_zone: zone recency ────────────
# REAL INCIDENT 2026-08-02: an AVAX SHORT was armed off a 'bearish_ob' candle from
# 2026-06-29 — 34.2 days (137 six-hour bars) before the entry — verified by replaying
# find_supply_zone against real Coinbase 6h history for that exact trade. The zone was
# picked purely because it was still "unmitigated" (price hadn't revisited it) and sat
# within 12% of price; nothing in find_supply_zone/find_demand_zone ever checked HOW
# LONG AGO the zone's candle pattern actually formed. This is the same mechanism behind
# the earlier SOL and ADA "fresh by bookkeeping, stale by real price action" incidents:
# bars_in_entry_wait only measures time since the zone was ARMED, never the age of the
# price event that created it — so a month-old, never-revisited OB/FVG can be selected
# today and look exactly as "fresh" as one from an hour ago. Fix: bound the scan window
# by the candle's own age, not just its unmitigated-ness.
def _flat_candle(price=100.0):
    return {"open": price, "high": price, "low": price, "close": price}


def _bars_with_bearish_ob(n, ob_index, breakdown_index, ob_open=105.0, ob_high=109.0):
    # Bearish OB: a bullish candle (body/range >= 0.40) later broken below its open
    # (wick only). Filler sits at 106 (inside the 104-109 OB range, above ob_low=104)
    # so every OTHER candle's close stays >= ob_low — otherwise the flat filler itself
    # would trivially satisfy the higher-priority 'bearish_breaker' pattern (needs any
    # later close < ob_low) and mask the OB candidate this test is targeting.
    # find_supply_zone reads this as c1 in its 3-candle window (c1=i-2, c3=i), so the
    # pattern is "found" at loop index ob_index + 2.
    rows = [_flat_candle(106.0) for _ in range(n)]
    ob_low = ob_open - 1.0
    rows[ob_index] = {"open": ob_open, "high": ob_high, "low": ob_low, "close": ob_high - 1.0}
    rows[breakdown_index] = {"open": ob_open - 0.5, "high": ob_open + 1.0,
                              "low": ob_open - 2.0, "close": ob_open - 0.5}
    return pd.DataFrame(rows)


def _bars_with_bullish_ob(n, ob_index, breakout_index, ob_open=95.0, ob_low=91.0):
    # Bullish OB: a bearish candle later broken above its open (wick only). Filler sits
    # at 93 (inside the 91-96 OB range, below ob_high=96) so it can't trivially satisfy
    # the higher-priority 'bullish_breaker' pattern (needs any later close > ob_high).
    rows = [_flat_candle(93.0) for _ in range(n)]
    ob_high = ob_open + 1.0
    rows[ob_index] = {"open": ob_open, "high": ob_high, "low": ob_low, "close": ob_low + 1.0}
    rows[breakout_index] = {"open": ob_open - 1.0, "high": ob_open + 0.5,
                             "low": ob_open - 2.0, "close": ob_open - 0.5}
    return pd.DataFrame(rows)


def test_find_supply_zone_finds_recent_unmitigated_ob():
    n = 50
    # ob candle at loop-index 42 (c1=40) -> pattern found at i=42, age = (n-1)-42 = 7 bars
    df = _bars_with_bearish_ob(n, ob_index=40, breakdown_index=46)
    found, z_lo, z_hi, z_type = find_supply_zone(df, current_price=100.0)
    assert found is True
    assert z_type == "bearish_ob"
    assert z_lo == pytest.approx(105.0)


def test_find_supply_zone_ignores_ob_older_than_default_max_age():
    # Same pattern, shifted so age = (n-1)-15 = 34 bars — matches the real AVAX incident
    # (34.2 days = ~34 six-hour bars). Default max_age_bars=30 must exclude it.
    n = 50
    df = _bars_with_bearish_ob(n, ob_index=13, breakdown_index=17)
    found, z_lo, z_hi, z_type = find_supply_zone(df, current_price=100.0)
    assert found is False


def test_find_supply_zone_respects_explicit_max_age_bars():
    # The same 34-bar-old zone IS found when the caller explicitly widens the window.
    n = 50
    df = _bars_with_bearish_ob(n, ob_index=13, breakdown_index=17)
    found, z_lo, z_hi, z_type = find_supply_zone(df, current_price=100.0, max_age_bars=40)
    assert found is True
    assert z_type == "bearish_ob"


def test_find_demand_zone_finds_recent_unmitigated_ob():
    n = 50
    df = _bars_with_bullish_ob(n, ob_index=40, breakout_index=46)
    found, z_lo, z_hi, z_type = find_demand_zone(df, current_price=100.0)
    assert found is True
    assert z_type == "bullish_ob"
    assert z_hi == pytest.approx(95.0)


def test_find_demand_zone_ignores_ob_older_than_default_max_age():
    n = 50
    df = _bars_with_bullish_ob(n, ob_index=13, breakout_index=17)
    found, z_lo, z_hi, z_type = find_demand_zone(df, current_price=100.0)
    assert found is False

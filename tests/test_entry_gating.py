"""Entry-gating fixes from the REAL 2026-08-11 AVAX SHORT incident.

The trade: entry $6.231, stored zone $6.24-$6.454, stop $6.4724 (3.9% away, $79 risk),
in a market whose entire prior 4h range was $6.155-$6.249 (~1.5%). Four separate
defects combined to produce it — one helper each, all pure so the gating is provable:

  A. carried_zone_age      — bars_in_entry_wait measured time since the last RE-ARM, not
                             how long the zone had existed. No expired-zone memory existed
                             and SymbolState.reset() calls __init__(), zeroing the counter,
                             so an expired zone fell to IDLE, got re-derived identically
                             from slow-moving 6h data, and re-armed at 0 — forever. The
                             STALE_ZONE_BARS gate could therefore never actually fire.
                             (Live proof: counter read 3 while the zone had sat ~20 bars.)
  B. drop_forming_candle   — the zone edge came from a still-FORMING HTF candle and was
                             snapshotted at arm time. Re-running the finder at entry gave
                             ($6.326, $6.454) vs the stored ($6.24, $6.454): against live
                             data price sat 1.5% BELOW the zone and would never have filled.
  C. price_in_entry_zone   — the old symmetric ±0.15% tolerance let a SHORT fill BELOW the
                             zone (selling supply at a discount). $6.231 cleared the old
                             threshold of $6.2307 by $0.0003 — 0.005% of price.
  D. cap_qty_for_risk      — the main entry path sized by MARGIN (balance × fraction), so a
                             wide structural stop silently scaled real risk up: $79 on a
                             trade meant to risk single digits. The 10-second sniper path
                             already used fixed-risk sizing; the main path did not.
"""
import pandas as pd
import pytest

from bot.indicators import (
    carried_zone_age, drop_forming_candle, price_in_entry_zone, cap_qty_for_risk,
)


# ───────────────────── A. carried_zone_age ─────────────────────
def test_age_carries_forward_when_the_same_zone_rearms():
    # identical zone re-armed after expiring at 12 bars -> must NOT restart at 0
    assert carried_zone_age(6.24, 6.454, 6.24, 6.454, prev_age=12) == 12


def test_age_carries_forward_within_tolerance():
    # HTF zones wobble a hair between recomputes; a ~0.05% drift is the SAME zone
    assert carried_zone_age(6.2412, 6.4535, 6.24, 6.454, prev_age=9) == 9


def test_age_resets_for_a_genuinely_different_zone():
    # a real new zone (levels well outside tolerance) legitimately starts fresh
    assert carried_zone_age(6.80, 7.10, 6.24, 6.454, prev_age=12) == 0


def test_age_starts_at_zero_with_no_previous_zone():
    assert carried_zone_age(6.24, 6.454, None, None, prev_age=0) == 0


def test_real_avax_rearm_would_have_been_rejected_as_stale():
    # The live incident: counter read 3, but the zone had actually sat ~20 bars.
    # Carrying the age forward makes it exceed STALE_ZONE_BARS (6) so the existing
    # freshness gate finally fires instead of being silently bypassed.
    STALE_ZONE_BARS = 6
    age = carried_zone_age(6.24, 6.454, 6.24, 6.454, prev_age=20)
    assert age == 20
    assert age > STALE_ZONE_BARS


# ───────────────────── B. drop_forming_candle ─────────────────────
def test_drop_forming_candle_removes_only_the_newest_bar():
    df = pd.DataFrame({"high": [1.0, 2.0, 3.0], "low": [0.5, 1.5, 2.5]})
    out = drop_forming_candle(df)
    assert len(out) == 2
    assert out["high"].tolist() == [1.0, 2.0]


def test_drop_forming_candle_is_safe_on_tiny_frames():
    assert len(drop_forming_candle(pd.DataFrame({"high": [1.0], "low": [0.5]}))) == 1
    assert len(drop_forming_candle(pd.DataFrame({"high": [], "low": []}))) == 0


def test_forming_candle_is_what_moved_the_avax_zone_edge():
    # The stored edge (6.24) came from a bar still in progress; by entry time that
    # same bar had printed a higher high, moving the edge to 6.326. Excluding the
    # unclosed bar makes the zone stable between recomputes.
    closed_highs = [6.24, 6.20, 6.22]
    forming_high = 6.326
    df = pd.DataFrame({"high": closed_highs + [forming_high],
                       "low":  [6.1, 6.1, 6.1, 6.1]})
    assert drop_forming_candle(df)["high"].max() == 6.24


# ───────────────────── C. price_in_entry_zone ─────────────────────
def test_short_must_actually_reach_the_supply_zone():
    # THE REAL INCIDENT: price 6.231 sat BELOW zone_lo 6.24 -> must be rejected.
    assert price_in_entry_zone(6.231, 6.24, 6.454, is_long=False) is False


def test_short_fills_inside_the_zone():
    assert price_in_entry_zone(6.30, 6.24, 6.454, is_long=False) is True
    assert price_in_entry_zone(6.24, 6.24, 6.454, is_long=False) is True   # at the edge


def test_short_may_overshoot_above_the_zone_within_tolerance():
    # overshooting INTO/through supply is still selling at a premium — allowed
    assert price_in_entry_zone(6.458, 6.24, 6.454, is_long=False) is True


def test_long_must_actually_reach_the_demand_zone():
    # mirror: a LONG filling ABOVE the zone is buying demand at a premium
    assert price_in_entry_zone(6.50, 6.24, 6.454, is_long=True) is False


def test_long_fills_inside_the_zone_and_may_overshoot_below():
    assert price_in_entry_zone(6.30, 6.24, 6.454, is_long=True) is True
    assert price_in_entry_zone(6.236, 6.24, 6.454, is_long=True) is True   # slight undershoot


def test_zone_with_missing_levels_is_never_enterable():
    assert price_in_entry_zone(6.30, None, 6.454, is_long=False) is False
    assert price_in_entry_zone(6.30, 6.24, None, is_long=True) is False


# ───────────────────── D. cap_qty_for_risk ─────────────────────
def test_qty_is_capped_so_a_stop_out_cannot_exceed_the_risk_budget():
    # real AVAX numbers: 327.3 units x $0.2414 stop distance = $79 risk
    risk_per_unit = 6.472428571428571 - 6.231
    capped = cap_qty_for_risk(327.297833, risk_per_unit, max_risk_dollars=8.0)
    assert capped * risk_per_unit == pytest.approx(8.0, rel=1e-6)
    assert capped < 327.297833


def test_qty_untouched_when_already_inside_the_risk_budget():
    # a tight stop that already risks less than the cap must not be scaled UP
    assert cap_qty_for_risk(10.0, 0.10, max_risk_dollars=8.0) == 10.0


def test_qty_zero_when_risk_per_unit_is_degenerate():
    assert cap_qty_for_risk(10.0, 0.0, max_risk_dollars=8.0) == 0.0
    assert cap_qty_for_risk(10.0, -1.0, max_risk_dollars=8.0) == 0.0

"""The sniper must risk no more than MAX_RISK_DOLLARS at the price it ACTUALLY fills.

THE BUG (fixed 2026-09-01). The 10-second sniper sizes itself when the zone is ARMED:

    qty = MAX_RISK_DOLLARS / |arm_price - arm_sl|

which risks exactly the cap *at that price*. It then waits — often 10+ minutes — for
price to tap the zone, and fills somewhere else, while `sniper_sl` stays fixed. At fire
time qty was re-derived as `sniper_margin * LEVERAGE / fill`, preserving MARGIN rather
than RISK, so any drift away from the stop silently inflated the loss. The true risk was
already being computed one line later — and only printed.

REAL FILLS THIS PRODUCED (both LONG, both stale-timeout exits):
    ADA 2026-08-30  entry 0.20203  sl 0.19957  qty 13,195.24  ->  risked $32.45  (+62%)
    POL 2026-08-31  entry 0.09206  sl 0.089618 qty  9,436.71  ->  risked $23.04  (+15%)
"""
import pytest

from bot.indicators import cap_qty_for_risk

MAX_RISK = 20.0


def _risk(qty, fill, sl):
    return abs(fill - sl) * qty


def test_real_ada_20260830_would_have_been_capped():
    fill, sl, qty = 0.20203, 0.19957, 13195.23503
    assert _risk(qty, fill, sl) == pytest.approx(32.46, abs=0.05), "fixture drifted"
    capped = cap_qty_for_risk(qty, abs(fill - sl), MAX_RISK)
    assert capped < qty
    assert _risk(capped, fill, sl) == pytest.approx(MAX_RISK, abs=0.01)


def test_real_pol_20260831_would_have_been_capped():
    fill, sl, qty = 0.09206, 0.089618, 9436.7092
    assert _risk(qty, fill, sl) == pytest.approx(23.04, abs=0.05), "fixture drifted"
    capped = cap_qty_for_risk(qty, abs(fill - sl), MAX_RISK)
    assert _risk(capped, fill, sl) == pytest.approx(MAX_RISK, abs=0.01)


def test_fill_closer_to_the_stop_is_left_alone():
    """Drift TOWARD the stop lowers risk below the cap. The cap must never scale UP —
    it is a ceiling, not a target."""
    arm_sl, arm_price = 0.089618, 0.0917373
    qty = MAX_RISK / abs(arm_price - arm_sl)          # exactly $20 at arm
    fill = 0.0910                                      # filled nearer the stop
    assert _risk(qty, fill, arm_sl) < MAX_RISK
    capped = cap_qty_for_risk(qty, abs(fill - arm_sl), MAX_RISK)
    assert capped == qty, "cap scaled qty UP — it must only ever shrink"


def test_fill_exactly_at_arm_price_is_unchanged():
    arm_sl, arm_price = 0.089618, 0.0917373
    qty = MAX_RISK / abs(arm_price - arm_sl)
    capped = cap_qty_for_risk(qty, abs(arm_price - arm_sl), MAX_RISK)
    assert capped == pytest.approx(qty)


def test_zero_risk_distance_returns_zero_not_infinity():
    """A fill sitting exactly on the armed stop would divide by zero."""
    assert cap_qty_for_risk(1000.0, 0.0, MAX_RISK) == 0.0

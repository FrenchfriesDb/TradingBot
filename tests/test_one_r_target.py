"""The stock bot targets 1R, and that is a measured choice — pin it so it can't drift back.

WHY (2026-10-01). MIN_TP_RR/MAX_TP_RR were 2.0/4.0 on the stated reasoning that "1:2 keeps
TP reachable intraday on 15m entries". Re-running tools/measure_tap_displacement.py with
the LIVE 1.5x-1H-ATR stop (it had been using the raw zone height, which puts the 2R target
far too close and flatters every win rate) measured the opposite over 8,289 taps:

    gate   passes   win@1R   exp@1R  total@1R   win@2R   exp@2R  total@2R
    0.0x     8289      49%   -0.03R     -249R      17%   -0.49R    -4062R
    1.0x      820      57%   +0.14R     +115R      24%   -0.28R     -230R

The same entries are profitable at 1R and losing at 2R. The edge was never missing; the
target was unreachable in the time the bot allows (EOD flatten), which is the same wall
tools/measure_hold_time.py hit independently at ~18%.
"""
import os

import pytest

from bot import indicators
from bot import strategy


def test_target_band_is_one_r():
    assert strategy.MIN_TP_RR == 1.0
    assert strategy.MAX_TP_RR == 1.0


def test_ai_floor_tracks_the_target_band():
    """If the bot targets 1R the AI must not be told to demand 2R, and the no-AI fallback
    must not plant a target the session cannot reach."""
    assert strategy.MIN_AI_RR == strategy.MIN_TP_RR


def test_all_three_are_env_overridable():
    """The 2.0 that was wrong here was a hardcoded literal for months. Any number this
    load-bearing has to be sweepable without an edit — see TAKER_FEE_RATE."""
    src = open("bot/strategy.py", encoding="utf-8").read()
    for name in ("MIN_TP_RR", "MAX_TP_RR", "MIN_AI_RR"):
        assert f'os.getenv("{name}"' in src, name


@pytest.mark.parametrize("pool_rr", [0.2, 0.9, 1.0, 2.5, 7.0, None])
def test_target_lands_exactly_at_one_r_whatever_structure_says(pool_rr):
    """min == max makes the liquidity-pool pinning a no-op BY DESIGN: a pool past 1R is
    capped to 1R, a nearer one or none defaults to 1R. Deliberate — raising MAX_TP_RR to
    let structure choose again reinstates the unreachable target."""
    entry, risk = 100.0, 2.0
    pool = entry + pool_rr * risk if pool_rr is not None else None
    tp = indicators.structural_take_profit(
        entry, risk, pool, True, strategy.MIN_TP_RR, strategy.MAX_TP_RR)
    assert tp == pytest.approx(entry + risk), f"pool at {pool_rr}R moved the target"


def test_short_side_is_symmetric():
    entry, risk = 100.0, 2.0
    tp = indicators.structural_take_profit(
        entry, risk, entry - 5 * risk, False, strategy.MIN_TP_RR, strategy.MAX_TP_RR)
    assert tp == pytest.approx(entry - risk)


def test_the_tap_gate_is_untouched_by_this_change():
    """1R is the TARGET decision. The entry gate stays at the value its own sweep picked,
    and that sweep's ranking is what makes 1.0x the total-R peak AT 1R (+115R)."""
    assert strategy.TAP_DISPLACEMENT_ATR_MULT == 1.0
    assert strategy.DISPLACEMENT_ATR_MULT == 1.8

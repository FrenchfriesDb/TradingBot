"""Three gates that stop a flat market from manufacturing a setup out of noise.

All three fixtures below are the REAL numbers from the live POL/USD long opened
2026-09-04 01:14 UTC, which the operator flagged by eye with "NO MOMENTUM candle
whatsoever". They were right, and each gate here catches a different reason why:

  price      0.09413
  5m ATR     0.000159   (0.169% of price -- a flat tape)
  6H ATR     0.002593   (2.755% -- clears the 2.0% tradeability floor, which is
                         why the bot considered POL live enough to trade at all)

  1. the bar that armed it had body 0.000170 == 1.07x the 5m ATR. The old floor was
     0.6x ATR, i.e. "60% of an AVERAGE candle" -- so a perfectly ordinary bar cleared
     the momentum test. Displacement has to mean OUTLIER, not average.
  2. the gap it left was 0.09426..0.09427 -- one tick, 0.011% of price. Nothing in
     the codebase set a minimum gap width, so a zone that is a LINE was armed.
  3. the target was 0.10721, 13.3% away, against a 6h hold limit and a 2.755% 6H ATR
     -- roughly 4.8 average 6H candles of travel inside the span of one. It reported
     R:R 1:9.4 and could only ever end in the stale-timeout. The PREVIOUS POL trade
     carried the identical 0.10721 target and did exactly that, for -$12.08.
"""
import pytest

from bot.indicators import (
    detect_displacement_fvg,
    displacement_min_body,
    reachable_target,
)

# ── the live POL trade, as recorded in crypto_state.json ──
POL_PRICE   = 0.09413
POL_ATR_5M  = 0.000159
POL_ATR_6H  = 0.002593
POL_BODY    = 0.000170     # the bar that passed the old 0.6x gate
POL_GAP_LO  = 0.09426
POL_GAP_HI  = 0.09427
POL_ENTRY   = 0.09465
POL_STOP    = 0.09331714285714286
POL_TARGET  = 0.10721


# ─────────────────────────────────────────────────────────────────────
# 1. displacement floor
# ─────────────────────────────────────────────────────────────────────

def test_old_gate_accepted_a_perfectly_average_bar():
    """Documents the defect. 0.6x ATR is below 1.0x ATR, so an average bar passes."""
    assert POL_BODY >= 0.6 * POL_ATR_5M
    assert POL_BODY / POL_ATR_5M == pytest.approx(1.07, abs=0.01)


def test_real_pol_bar_is_rejected_by_the_new_floor():
    floor = displacement_min_body(POL_ATR_5M, POL_PRICE)
    assert POL_BODY < floor, "the bar the operator could not see must not qualify"


def test_floor_is_the_larger_of_the_two_terms():
    """ATR term scales with the market; the percentage term is the backstop that
    stops a dead tape from lowering the bar to nothing."""
    # flat tape -> percentage term wins
    assert displacement_min_body(atr=0.000159, price=0.09413, atr_mult=1.8,
                                 min_pct=0.0015) == pytest.approx(0.0002862)
    # busy tape -> ATR term wins
    assert displacement_min_body(atr=0.0010, price=0.09413, atr_mult=1.8,
                                 min_pct=0.0015) == pytest.approx(0.0018)


def test_floor_selects_an_outlier_not_an_average_bar():
    """Whatever the regime, a qualifying body must exceed one full ATR."""
    for atr, price in ((0.000159, 0.09413), (60.0, 60000.0), (0.4, 25.0)):
        assert displacement_min_body(atr, price) > atr


def test_btc_scale_floor_stays_reachable():
    """The percentage backstop must not make large caps untradeable. BTC at $60k
    with a 0.1% 5m ATR should need ~0.18% of price, not something absurd."""
    floor = displacement_min_body(atr=60.0, price=60000.0)
    assert floor == pytest.approx(108.0)
    assert floor / 60000.0 < 0.0025


def test_zero_atr_falls_back_to_the_percentage_floor():
    """range_atr() returns 0.0 on a short or NaN series; that must not disable the gate."""
    assert displacement_min_body(atr=0.0, price=0.09413) == pytest.approx(0.0001412, abs=1e-7)


# ─────────────────────────────────────────────────────────────────────
# 2. minimum gap width
# ─────────────────────────────────────────────────────────────────────

def _bars(n, prior, disp, nxt):
    """n filler bars then the 3-candle sequence. n must keep the total >= lookback (20),
    or detect_displacement_fvg short-circuits to False before examining anything."""
    import pandas as pd
    base = [{"open": 100.0, "high": 100.4, "low": 99.6, "close": 100.0,
             "timestamp": i, "volume": 1.0} for i in range(n)]
    return pd.DataFrame(base + [prior, disp, nxt])


def _c(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c, "timestamp": 0, "volume": 1.0}


def test_one_tick_gap_is_rejected():
    prior = _c(100.0, 100.10, 99.9, 100.0)
    disp  = _c(100.0, 101.60, 99.9, 101.50)      # big body, real displacement
    nxt   = _c(101.4, 101.60, 100.11, 101.5)     # C3.low only 0.01 above C1.high
    found, *_ = detect_displacement_fvg(_bars(20, prior, disp, nxt))
    assert found, "sanity: the sequence is a valid FVG with no width floor"

    found, *_ = detect_displacement_fvg(_bars(20, prior, disp, nxt), min_gap_abs=0.15)
    assert not found, "a 0.01-wide gap is a line, not a zone"


def test_wide_gap_still_accepted():
    prior = _c(100.0, 100.10, 99.9, 100.0)
    disp  = _c(100.0, 101.60, 99.9, 101.50)
    nxt   = _c(101.4, 101.60, 100.90, 101.5)     # C3.low 0.80 above C1.high
    found, direction, lo, hi, _ = detect_displacement_fvg(
        _bars(20, prior, disp, nxt), min_gap_abs=0.15)
    assert found and direction == "bullish"
    assert hi - lo == pytest.approx(0.80)


def test_real_pol_gap_would_be_rejected():
    """0.09426..0.09427 against a 0.15%-of-price floor."""
    width = POL_GAP_HI - POL_GAP_LO
    floor = 0.0015 * POL_PRICE
    assert width == pytest.approx(0.00001)
    assert width < floor


def test_gap_floor_defaults_to_off():
    """Untouched callers keep prior behaviour."""
    prior = _c(100.0, 100.10, 99.9, 100.0)
    disp  = _c(100.0, 101.60, 99.9, 101.50)
    nxt   = _c(101.4, 101.60, 100.11, 101.5)
    assert detect_displacement_fvg(_bars(20, prior, disp, nxt))[0] is True


# ─────────────────────────────────────────────────────────────────────
# 3. target reachability
# ─────────────────────────────────────────────────────────────────────

def test_real_pol_target_is_pulled_back_to_something_reachable():
    tgt, rr, ok = reachable_target(POL_ENTRY, POL_STOP, POL_TARGET, POL_ATR_6H)
    assert ok, "the trade is still takeable -- only the fantasy target is trimmed"
    assert tgt < POL_TARGET
    assert tgt == pytest.approx(POL_ENTRY + 1.5 * POL_ATR_6H)
    # 1:9.4 on paper becomes a real ~1:2.9
    assert rr == pytest.approx(2.92, abs=0.05)


def test_untouched_when_the_target_is_already_reachable():
    entry, stop = 100.0, 99.0
    tgt, rr, ok = reachable_target(entry, stop, 102.0, htf_atr=4.0)
    assert ok and tgt == 102.0 and rr == pytest.approx(2.0)


def test_rejected_when_clamping_would_break_the_rr_floor():
    """If the reachable distance can't pay 1:2 against the stop, there is no trade."""
    entry, stop = 100.0, 99.0
    tgt, rr, ok = reachable_target(entry, stop, 130.0, htf_atr=1.0, min_rr=2.0)
    assert not ok, "reachable distance 1.5 vs a 1.0 stop is only 1:1.5"
    assert rr == pytest.approx(1.5)


def test_short_side_mirrors():
    entry, stop = 100.0, 101.0
    tgt, rr, ok = reachable_target(entry, stop, 70.0, htf_atr=4.0)
    assert ok
    assert tgt == pytest.approx(94.0)
    assert rr == pytest.approx(6.0)


def test_unknown_htf_atr_leaves_the_target_alone():
    """A data hiccup must not silently veto every setup -- fail OPEN here, because the
    other two gates already carry the size requirement."""
    tgt, rr, ok = reachable_target(100.0, 99.0, 130.0, htf_atr=0.0)
    assert ok and tgt == 130.0


def test_zero_stop_distance_is_refused_not_divided_by():
    tgt, rr, ok = reachable_target(100.0, 100.0, 130.0, htf_atr=4.0)
    assert not ok and rr == 0.0

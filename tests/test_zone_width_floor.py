"""A zone too thin to trade is not a zone.

REAL INCIDENT 2026-09-22. The stock bot armed AAPL and sat in ENTRY_WAIT on it all
morning without ever filling:

    06:31:06 [AAPL] 📊 Trend zone: daily BULLISH → [bullish_fvg] 338.49–338.53
                    → LONG on pullback
    07:16:03 [AAPL] state=ENTRY_WAIT price=339.7250 ... [AMD 338.49–338.53 ⏳waiting]
    07:47:04 [AAPL] state=ENTRY_WAIT price=340.9450 ... [AMD 338.49–338.53 ⏳waiting]

That zone is FOUR CENTS wide on a $338 stock — 0.012%. Every other symbol that
morning was 0.198%–2.319%:

    QQQ  737.89-740.72  0.379%      NVDA 225.50-226.73  0.536%
    SPY  771.36-772.89  0.198%      TSLA 370.38-372.63  0.593%
    META 722.38-739.55  2.319%      AAPL 338.49-338.53  0.012%  <-

The gap is real — c1.high 338.49 < c3.low 338.53 is a genuine 4H imbalance. It is just
far too thin to trade: price crosses it inside a single tick, so the "tap" either never
registers or registers on noise, and a structural stop placed against it sits inside the
spread.

ROOT CAUSE. The codebase already knows this. bot/indicators.py:949, on
detect_displacement_fvg:

    "leave a gap at least min_gap_abs wide. A one-tick gap is a line, not a zone"

detect_displacement_fvg, find_bullish_fvg and find_bearish_fvg all enforce it.
find_supply_zone and find_demand_zone — which produce the AMD and trend-follow zones,
i.e. most of what the bots actually arm — never did. Same shape as the fvg_tf bug: the
gate exists and covers some of the paths.

The floor therefore lives INSIDE these two functions rather than at the call sites.
There are eight live call sites across the two bots and not one of them passed a width
argument; a parameter defaulting to "no floor" would have been forgotten in exactly the
same way.
"""
import pandas as pd
import pytest

from bot.indicators import find_demand_zone, find_supply_zone

FLOOR = 0.0015          # MIN_FVG_PCT, the same 0.15% the sibling detectors use
PRICE = 342.33


def _rising_with_gap(gap_lo, gap_hi):
    """Exactly ONE bullish FVG (c1.high=gap_lo -> c3.low=gap_hi), never filled.

    The later bars deliberately all share the SAME low, just above the gap: any bar whose
    low rises above the high of the bar two before it would open a SECOND gap, and an
    earlier draft of this fixture did exactly that — the helper, not the code, was what
    failed three of these tests."""
    rows = [
        (gap_lo - 1.0, gap_lo,       gap_lo - 1.5, gap_lo - 0.3),   # c1  high = gap_lo
        (gap_lo - 0.3, gap_hi + 1.0, gap_lo - 0.4, gap_lo + 0.01),  # c2  wicks over c3
        (gap_hi + 0.02, gap_hi + 0.7, gap_hi,      gap_hi + 0.6),   # c3  low  = gap_hi
    ]
    hi = gap_hi + 0.9
    for _ in range(8):
        rows.append((gap_hi + 0.2, hi, gap_hi + 0.01, hi - 0.2))
        hi += 0.40
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def _falling_with_gap(gap_lo, gap_hi):
    """Mirror: exactly one bearish FVG (c1.low=gap_hi -> c3.high=gap_lo), never filled."""
    rows = [
        (gap_hi + 1.0, gap_hi + 1.5, gap_hi,       gap_hi + 0.3),   # c1  low  = gap_hi
        (gap_hi + 0.3, gap_hi + 0.4, gap_lo - 1.0, gap_hi - 0.01),  # c2  wicks under c3
        (gap_lo - 0.02, gap_lo,      gap_lo - 0.7, gap_lo - 0.6),   # c3  high = gap_lo
    ]
    lo = gap_lo - 0.9
    for _ in range(8):
        rows.append((gap_lo - 0.2, gap_lo - 0.01, lo, lo + 0.2))
        lo -= 0.40
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def _assert_single_gap(df, bullish=True):
    """The fixture is only meaningful if it contains the one gap it claims to."""
    if bullish:
        n = sum(1 for i in range(2, len(df)) if df.iloc[i-2]['high'] < df.iloc[i]['low'])
    else:
        n = sum(1 for i in range(2, len(df)) if df.iloc[i-2]['low'] > df.iloc[i]['high'])
    assert n == 1, f"fixture has {n} gaps, not 1 — the test would prove nothing"


def _width_pct(lo, hi, price=PRICE):
    return (hi - lo) / price * 100


# ── the incident ──────────────────────────────────────────────────────────────

def test_aapls_four_cent_zone_is_refused():
    """The exact numbers from the log: 338.49 -> 338.53 on a $342 stock."""
    df = _rising_with_gap(338.49, 338.53)
    _assert_single_gap(df)
    found, lo, hi, kind = find_demand_zone(df, PRICE)
    assert not found, f"armed a {_width_pct(lo, hi):.4f}%-wide {kind} zone: {lo}-{hi}"


def test_the_mirror_case_is_refused_for_shorts():
    df = _falling_with_gap(346.00, 346.04)
    _assert_single_gap(df, bullish=False)
    found, lo, hi, kind = find_supply_zone(df, 342.0)
    assert not found, f"armed a {(hi-lo)/342.0*100:.4f}%-wide {kind} zone: {lo}-{hi}"


# ── the floor holds in both directions ───────────────────────────────────────

@pytest.mark.parametrize("gap,expect_found", [
    (0.04, False),   # AAPL's 0.012% — far under
    (0.30, False),   # 0.088% — still under
    (0.51, False),   # 0.149% — just under
    (0.60, True),    # 0.175% — over
    (2.00, True),    # 0.584% — comfortably over, like NVDA/TSLA
])
def test_only_gaps_at_or_above_the_floor_survive(gap, expect_found):
    df = _rising_with_gap(338.49, 338.49 + gap)
    _assert_single_gap(df)
    found, lo, hi, _k = find_demand_zone(df, PRICE)
    assert found is expect_found, (
        f"gap {gap} = {gap/PRICE*100:.3f}% of price, floor is {FLOOR*100:.2f}%; "
        f"got found={found}" + (f" zone {lo}-{hi}" if found else ""))


def test_every_zone_that_survives_clears_the_floor():
    for gap in (0.6, 1.0, 2.0, 5.0):
        found, lo, hi, _ = find_demand_zone(_rising_with_gap(338.49, 338.49 + gap), PRICE)
        if found:
            assert (hi - lo) >= FLOOR * PRICE - 1e-9, (gap, lo, hi)


# ── the widths seen live that must NOT be affected ───────────────────────────

@pytest.mark.parametrize("name,lo,hi,price", [
    ("QQQ",  737.89, 740.72, 746.85),
    ("SPY",  771.36, 772.89, 774.32),
    ("META", 722.38, 739.55, 740.48),
    ("NVDA", 225.50, 226.73, 229.49),
    ("TSLA", 370.38, 372.63, 379.56),
])
def test_the_other_symbols_zones_that_morning_still_qualify(name, lo, hi, price):
    """Calibration check: the floor must reject only AAPL's, not the whole watchlist."""
    assert (hi - lo) >= FLOOR * price, (
        f"{name}'s real zone {lo}-{hi} ({(hi-lo)/price*100:.3f}%) would be rejected")


# ── the floor is on by default, and overridable ──────────────────────────────

def test_the_floor_is_on_without_any_caller_asking():
    """Eight live call sites pass no width argument. If the default were 'no floor',
    this bug would simply persist at all eight."""
    df = _rising_with_gap(338.49, 338.53)
    assert not find_demand_zone(df, PRICE)[0]


def test_it_can_be_switched_off_explicitly():
    df = _rising_with_gap(338.49, 338.53)
    found, lo, hi, _ = find_demand_zone(df, PRICE, min_width_pct=0.0)
    assert found and (hi - lo) == pytest.approx(0.04)


def test_a_wider_floor_rejects_more():
    df = _rising_with_gap(338.49, 340.49)          # 0.58% wide
    assert find_demand_zone(df, PRICE)[0]
    assert not find_demand_zone(df, PRICE, min_width_pct=0.02)[0]


def test_an_unusable_price_does_not_crash_or_silently_disable_the_floor():
    df = _rising_with_gap(338.49, 338.53)
    for bad in (0.0, -5.0, float("nan")):
        found, _lo, _hi, _k = find_demand_zone(df, bad)
        assert found is False, f"price={bad!r} let a 4-cent zone through"

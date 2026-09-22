"""A tap is something that happened during the bar, not something true at poll time.

2026-09-22. SPY's armed demand zone was 771.36-772.89. Price traded INSIDE it for 31
minutes, 11:36-12:43 ET. The bot never entered, and never even evaluated an entry,
because the test at bot/strategy.py:1755 was:

    in_fvg = self.fvg_low[symbol] <= current_price <= self.fvg_high[symbol]

— the last price, sampled once every 15 minutes. A dip that starts and ends between two
polls is invisible. Measured on the day's 1-minute bars:

    SPY   771.36-772.89   31 min inside
    META  722.38-739.55   36 min inside
    NVDA  225.50-226.73    1 min inside   <- a 15-min poll will essentially never see it
    QQQ / TSLA / AAPL      0 min          <- price genuinely never came back

(That day the bot was also asleep for 3h38m, which is why SPY specifically was missed.
The sampling hole is the separate, permanent half of the problem.)

THE DANGER IN FIXING IT. Everything downstream — stop, risk_amt, take-profit, R:R — is
computed from current_price, and the order is a MARKET order. So "the bar touched the
zone, therefore buy now" fills outside the zone the setup armed. That is a real,
already-diagnosed bug on the crypto side:

    ASTER/USD LONG armed demand at 0.7190-0.7588 and filled at 0.7906 — 4.19% above the
    top of its own zone, on a price that never traded down to it.

So detection widens, and a directional chase guard keeps the fill honest. The guard is
NOT symmetric, and that is the part worth reading twice:

    LONG  (demand zone BELOW price): price back ABOVE the zone is the expected
          tap-and-reject — allow it, up to a bounded chase. Price BELOW the zone low
          means the demand FAILED and the stop is about to be hit. Never enter.
    SHORT (supply zone ABOVE price): mirror image.
"""
import pytest

from bot.indicators import zone_tapped, tap_chase_ok


def _bar(lo, hi):
    return {"low": lo, "high": hi}


# ── detection: did price trade into the zone during the bar? ──────────────────

def test_a_wick_into_the_zone_counts_even_though_the_close_is_outside():
    """The SPY case in miniature: the bar dipped in and came back out."""
    bars = [_bar(772.00, 774.50)]          # low 772.00 is inside 771.36-772.89
    assert zone_tapped(bars, 771.36, 772.89)


def test_a_bar_entirely_above_the_zone_is_not_a_tap():
    assert not zone_tapped([_bar(773.50, 775.00)], 771.36, 772.89)


def test_a_bar_entirely_below_the_zone_is_not_a_tap():
    assert not zone_tapped([_bar(768.00, 770.00)], 771.36, 772.89)


def test_a_bar_straddling_the_whole_zone_is_a_tap():
    assert zone_tapped([_bar(765.00, 780.00)], 771.36, 772.89)


def test_touching_an_edge_exactly_counts():
    assert zone_tapped([_bar(772.89, 775.0)], 771.36, 772.89)
    assert zone_tapped([_bar(768.0, 771.36)], 771.36, 772.89)


def test_it_looks_back_far_enough_to_cover_the_poll_interval():
    """The bot polls every 15 min on 15-min bars, so one bar can be the forming one.
    Two bars always span the gap since the previous poll."""
    bars = [_bar(772.00, 774.00), _bar(773.50, 775.00)]   # older bar tapped, newer did not
    assert zone_tapped(bars, 771.36, 772.89, lookback=2)
    assert not zone_tapped(bars, 771.36, 772.89, lookback=1)


def test_the_live_price_case_still_registers():
    """Nothing that used to be a tap stops being one."""
    assert zone_tapped([_bar(772.00, 772.50)], 771.36, 772.89)


@pytest.mark.parametrize("bad", [None, [], [{"low": None, "high": 5}], [{}]])
def test_unreadable_bars_are_not_a_tap(bad):
    assert zone_tapped(bad, 771.36, 772.89) is False


def test_an_unreadable_zone_is_not_a_tap():
    assert zone_tapped([_bar(1, 2)], None, 772.89) is False
    assert zone_tapped([_bar(1, 2)], 772.89, 771.36) is False       # inverted


# ── the chase guard: is the fill still honest? ───────────────────────────────

ATR = 1.00


def test_inside_the_zone_is_always_fine():
    for is_long in (True, False):
        ok, _why = tap_chase_ok(772.00, 771.36, 772.89, ATR, is_long=is_long)
        assert ok


def test_a_long_may_chase_a_little_above_the_zone():
    ok, _ = tap_chase_ok(773.20, 771.36, 772.89, ATR, is_long=True)   # 0.31 over, < 0.5 ATR
    assert ok


def test_a_long_may_not_chase_far_above_the_zone():
    ok, why = tap_chase_ok(774.00, 771.36, 772.89, ATR, is_long=True)  # 1.11 over, > 0.5 ATR
    assert not ok and "ran" in why.lower()


def test_a_long_NEVER_enters_below_its_own_demand_zone():
    """The asymmetry. Below the zone low the demand has failed and the structural stop
    sits just under — entering is buying into a live invalidation."""
    ok, why = tap_chase_ok(771.00, 771.36, 772.89, ATR, is_long=True)   # only 0.36 below
    assert not ok, "entered a long underneath the zone that was supposed to hold it"
    assert "fail" in why.lower() or "below" in why.lower()


def test_the_long_rejection_below_is_not_a_distance_question():
    """Even one tick below, and even with a huge ATR that would permit the distance."""
    ok, _ = tap_chase_ok(771.35, 771.36, 772.89, atr=100.0, is_long=True)
    assert not ok


def test_a_short_may_chase_a_little_below_its_supply_zone():
    ok, _ = tap_chase_ok(770.90, 771.36, 772.89, ATR, is_long=False)
    assert ok


def test_a_short_NEVER_enters_above_its_own_supply_zone():
    ok, why = tap_chase_ok(773.00, 771.36, 772.89, ATR, is_long=False)
    assert not ok
    assert "fail" in why.lower() or "above" in why.lower()


def test_the_chase_budget_scales_with_atr():
    """A quiet stock gets a tighter leash than a volatile one."""
    assert not tap_chase_ok(773.60, 771.36, 772.89, atr=0.20, is_long=True)[0]
    assert tap_chase_ok(773.60, 771.36, 772.89, atr=5.00, is_long=True)[0]


def test_a_dead_atr_tightens_the_gate_rather_than_opening_it():
    """Fail closed: an unreadable ATR must not become an unlimited chase budget."""
    for bad_atr in (0.0, None, float("nan"), -1.0):
        ok, _ = tap_chase_ok(774.00, 771.36, 772.89, bad_atr, is_long=True)
        assert not ok, f"atr={bad_atr!r} allowed a 1.11 chase"
        # still inside the zone must remain fine
        assert tap_chase_ok(772.00, 771.36, 772.89, bad_atr, is_long=True)[0]


def test_the_multiplier_is_configurable():
    assert not tap_chase_ok(774.00, 771.36, 772.89, ATR, is_long=True)[0]
    assert tap_chase_ok(774.00, 771.36, 772.89, ATR, is_long=True, max_atr_mult=2.0)[0]


@pytest.mark.parametrize("bad_price", [None, 0.0, -1.0, float("nan")])
def test_an_unusable_price_is_refused(bad_price):
    ok, _ = tap_chase_ok(bad_price, 771.36, 772.89, ATR, is_long=True)
    assert not ok

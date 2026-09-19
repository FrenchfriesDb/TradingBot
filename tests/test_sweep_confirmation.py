"""A sweep is worth trading when the level is REJECTED, then the move actually starts.

The operator, after test_bot shorted ADA into a rally and missed the move itself:
"there should have been an indecison candle then a momentum candle for the sweep".

That is the classic two-bar reversal and it encodes something real. A sweep on its own
says only that a level was touched — price took the liquidity and may simply keep going,
which is exactly what ADA did. The pattern that distinguishes a reversal from a
continuation is:

    1. INDECISION at the level — a doji, a spinning top, a long rejection wick. Sellers
       and buyers fighting; the move that carried price into the level has stalled.
    2. MOMENTUM away from it — a decisive body in the trade's direction. The fight
       resolved, and it resolved our way.

Without (1) there was no rejection, just a level being passed through. Without (2) there
is a stall but no evidence anyone has taken the other side yet, and entering there is
guessing at the turn rather than trading it.

ORDER MATTERS, AND SO DOES RECENCY. The momentum bar has to be the most recent one. If
price stalled, pushed, and then chopped for three more bars, the setup has gone stale and
entering is the "enters after the big move" complaint from the crypto bot all over again.
"""
import pytest

from bot.indicators import sweep_confirmation


def _c(o, c, h=None, l=None):
    return {"open": o, "close": c,
            "high": h if h is not None else max(o, c),
            "low": l if l is not None else min(o, c)}


# a doji: tiny body, wicks both sides
DOJI = _c(100.0, 100.05, h=100.9, l=99.1)
# a long lower wick rejecting a swept low
HAMMER = _c(100.0, 100.3, h=100.4, l=98.8)
# decisive bullish body
PUSH_UP = _c(100.0, 101.2, h=101.3, l=99.95)
PUSH_DOWN = _c(101.2, 100.0, h=101.25, l=99.9)
# a body too small to be momentum — and small enough that it IS indecision
NUDGE_UP = _c(100.0, 100.1, h=100.6, l=99.5)
# neither: body 40% of range, above the indecision ceiling, below the momentum floor
MEDIUM_UP = _c(100.0, 100.4, h=100.5, l=99.5)


# ── the pattern the operator described ────────────────────────────────────────

def test_indecision_then_momentum_confirms_a_long():
    ok, why = sweep_confirmation([DOJI, PUSH_UP], is_long=True)
    assert ok is True, why


def test_indecision_then_momentum_confirms_a_short():
    ok, why = sweep_confirmation([DOJI, PUSH_DOWN], is_long=False)
    assert ok is True, why


def test_a_rejection_wick_counts_as_the_indecision():
    """A hammer at a swept low IS the fight — it does not have to be a textbook doji."""
    assert sweep_confirmation([HAMMER, PUSH_UP], is_long=True)[0] is True


# ── what it must refuse ───────────────────────────────────────────────────────

def test_momentum_with_no_indecision_before_it_is_refused():
    """Price ran straight through the level. Nothing was rejected — this is the
    continuation that shorted ADA into a rally."""
    ok, why = sweep_confirmation([PUSH_UP, PUSH_UP], is_long=True)
    assert ok is False
    assert "indecision" in why.lower()


def test_indecision_with_no_momentum_yet_is_refused():
    """The fight has not resolved. Entering here is guessing at the turn."""
    ok, why = sweep_confirmation([DOJI, NUDGE_UP], is_long=True)
    assert ok is False
    assert "momentum" in why.lower()


def test_momentum_the_wrong_way_is_refused():
    """Indecision, then the move goes AGAINST the trade. That is the other side winning."""
    assert sweep_confirmation([DOJI, PUSH_DOWN], is_long=True)[0] is False
    assert sweep_confirmation([DOJI, PUSH_UP], is_long=False)[0] is False


def test_the_momentum_bar_must_be_the_most_recent_one():
    """Stalled, pushed, then chopped for three bars — the setup is stale and entering
    now is the "enters after the big move" complaint all over again."""
    stale = [DOJI, PUSH_UP, NUDGE_UP, NUDGE_UP, NUDGE_UP]
    ok, why = sweep_confirmation(stale, is_long=True)
    assert ok is False, why


def test_a_gap_of_chop_between_the_two_is_tolerated_only_briefly():
    """One quiet bar between the rejection and the push is normal. Push the rejection
    far enough back and it is a different setup that merely contains a doji behind it.

    The filler bars have to be MEDIUM, not quiet: a small body with wicks both sides is
    itself indecision, so a string of those is still "price fighting at the level" and
    the pattern genuinely does still hold."""
    assert sweep_confirmation([DOJI, MEDIUM_UP, PUSH_UP], is_long=True)[0] is True
    assert sweep_confirmation(
        [DOJI, MEDIUM_UP, MEDIUM_UP, MEDIUM_UP, MEDIUM_UP, PUSH_UP], is_long=True)[0] is False


def test_a_run_of_quiet_bars_still_counts_as_the_fight():
    """Deliberate: small-bodied bars with wicks both sides ARE indecision. A stall that
    lasts several bars and then resolves is the pattern, not an evasion of it."""
    assert sweep_confirmation(
        [NUDGE_UP, NUDGE_UP, NUDGE_UP, PUSH_UP], is_long=True)[0] is True


# ── size floor, so "momentum" is not a rounding error ─────────────────────────

def test_the_momentum_bar_must_clear_an_absolute_size_floor():
    """Same floor the displacement gates use — pass ~1.8x ATR. A decisive-looking body
    on a dead chart is still noise."""
    assert sweep_confirmation([DOJI, PUSH_UP], is_long=True, min_body_abs=0.5)[0] is True
    assert sweep_confirmation([DOJI, PUSH_UP], is_long=True, min_body_abs=5.0)[0] is False


# ── degenerate input ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("bars", [[], None, [DOJI], "nope"])
def test_too_little_data_fails_closed(bars):
    """Cannot see the pattern -> do not claim it. Same rule as every other gate today."""
    assert sweep_confirmation(bars, is_long=True)[0] is False


def test_malformed_bars_fail_closed_rather_than_raising():
    assert sweep_confirmation([{"open": 1}, {"open": 2}], is_long=True)[0] is False


def test_a_zero_range_bar_is_not_momentum():
    flat = _c(100.0, 100.0, h=100.0, l=100.0)
    assert sweep_confirmation([DOJI, flat], is_long=True)[0] is False


def test_the_reason_says_which_half_was_missing():
    """A refusal has to be diagnosable from the log — "skipped" cost an hour on ADA."""
    _, no_ind = sweep_confirmation([PUSH_UP, PUSH_UP], is_long=True)
    _, no_mom = sweep_confirmation([DOJI, NUDGE_UP], is_long=True)
    assert no_ind != no_mom
    assert len(no_ind) > 15 and len(no_mom) > 15

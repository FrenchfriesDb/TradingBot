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


# A balanced doji — wicks both sides, neither dominant. Genuine indecision, but it
# records no REJECTION, so it confirms neither direction. Kept deliberately, because
# distinguishing it from the directional shapes below is the point of this gate.
DOJI = _c(100.0, 100.05, h=100.9, l=99.1)
# Dragonfly: tiny body, long LOWER wick — buyers defended a swept low. Confirms a LONG.
DRAGONFLY = _c(100.0, 100.05, h=100.15, l=99.0)
# Gravestone: tiny body, long UPPER wick — sellers slammed a swept high. Confirms a SHORT.
GRAVESTONE_BAR = _c(100.0, 99.95, h=101.0, l=99.85)
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
    ok, why = sweep_confirmation([DRAGONFLY, PUSH_UP], is_long=True)
    assert ok is True, why


def test_indecision_then_momentum_confirms_a_short():
    ok, why = sweep_confirmation([GRAVESTONE_BAR, PUSH_DOWN], is_long=False)
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
    assert "rejection" in why.lower()


def test_indecision_with_no_momentum_yet_is_refused():
    """The fight has not resolved. Entering here is guessing at the turn."""
    ok, why = sweep_confirmation([DRAGONFLY, NUDGE_UP], is_long=True)
    assert ok is False
    assert "momentum" in why.lower()


def test_momentum_the_wrong_way_is_refused():
    """Indecision, then the move goes AGAINST the trade. That is the other side winning."""
    assert sweep_confirmation([DRAGONFLY, PUSH_DOWN], is_long=True)[0] is False
    assert sweep_confirmation([GRAVESTONE_BAR, PUSH_UP], is_long=False)[0] is False


def test_the_momentum_bar_must_be_the_most_recent_one():
    """Stalled, pushed, then chopped for three bars — the setup is stale and entering
    now is the "enters after the big move" complaint all over again."""
    stale = [DRAGONFLY, PUSH_UP, NUDGE_UP, NUDGE_UP, NUDGE_UP]
    ok, why = sweep_confirmation(stale, is_long=True)
    assert ok is False, why


def test_a_gap_of_chop_between_the_two_is_tolerated_only_briefly():
    """One quiet bar between the rejection and the push is normal. Push the rejection
    far enough back and it is a different setup that merely contains a doji behind it.

    The filler bars have to be MEDIUM, not quiet: a small body with wicks both sides is
    itself indecision, so a string of those is still "price fighting at the level" and
    the pattern genuinely does still hold."""
    assert sweep_confirmation([DRAGONFLY, MEDIUM_UP, PUSH_UP], is_long=True)[0] is True
    assert sweep_confirmation(
        [DRAGONFLY, MEDIUM_UP, MEDIUM_UP, MEDIUM_UP, MEDIUM_UP, PUSH_UP], is_long=True)[0] is False


def test_a_run_of_quiet_bars_still_counts_as_the_fight():
    """Deliberate: small-bodied bars with wicks both sides ARE indecision. A stall that
    lasts several bars and then resolves is the pattern, not an evasion of it."""
    quiet = _c(100.0, 100.05, h=100.12, l=99.4)     # small body, lower wick dominant
    assert sweep_confirmation([quiet, quiet, quiet, PUSH_UP], is_long=True)[0] is True


# ── size floor, so "momentum" is not a rounding error ─────────────────────────

def test_the_momentum_bar_must_clear_an_absolute_size_floor():
    """Same floor the displacement gates use — pass ~1.8x ATR. A decisive-looking body
    on a dead chart is still noise."""
    assert sweep_confirmation([DRAGONFLY, PUSH_UP], is_long=True, min_body_abs=0.5)[0] is True
    assert sweep_confirmation([DRAGONFLY, PUSH_UP], is_long=True, min_body_abs=5.0)[0] is False


# ── degenerate input ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("bars", [[], None, [DRAGONFLY], "nope"])
def test_too_little_data_fails_closed(bars):
    """Cannot see the pattern -> do not claim it. Same rule as every other gate today."""
    assert sweep_confirmation(bars, is_long=True)[0] is False


def test_malformed_bars_fail_closed_rather_than_raising():
    assert sweep_confirmation([{"open": 1}, {"open": 2}], is_long=True)[0] is False


def test_a_zero_range_bar_is_not_momentum():
    flat = _c(100.0, 100.0, h=100.0, l=100.0)
    assert sweep_confirmation([DRAGONFLY, flat], is_long=True)[0] is False


def test_the_reason_says_which_half_was_missing():
    """A refusal has to be diagnosable from the log — "skipped" cost an hour on ADA."""
    _, no_ind = sweep_confirmation([PUSH_UP, PUSH_UP], is_long=True)
    _, no_mom = sweep_confirmation([DRAGONFLY, NUDGE_UP], is_long=True)
    assert no_ind != no_mom
    assert len(no_ind) > 15 and len(no_mom) > 15


# ── the rejection wick has to be on the SELLING side ──────────────────────────
#
# The operator, on the ADA entry: "the BOT should have waited for a gravestone, a candle
# with a small body and a long upper wick, it shows that price was continuing to be
# pushing up but then A MASSIVE wave of sellers stepped in and pushed the MOMENTUM
# candle all the way down."
#
# The first version of this gate took ANY small body as indecision, direction-blind. That
# is too loose in a way that matters: a hammer at a swept high — tiny body, long LOWER
# wick — is buyers defending, and reading it as "indecision before a short" gets the
# story backwards. What confirms a short is a bar that pushed UP and was slammed back:
# the upper wick is where the rejection is recorded.
#
# So the indecision bar must now carry its wick on the trade's rejection side: upper for
# a short, lower for a long, and that wick has to dominate the opposite one rather than
# merely exist.
#
# The live 03:30 bar that preceded the first good short entry is exactly this shape:
#   open 0.23160  high 0.23327  low 0.23127  close 0.23186
#   body 13% of range, upper wick 70%, lower wick 17%  -> a gravestone

GRAVESTONE = _c(0.23160, 0.23186, h=0.23327, l=0.23127)   # verbatim, the live 03:30 bar
PUSH_DOWN_HARD = _c(0.23189, 0.23098, h=0.23216, l=0.23084)  # the live 03:45 bar


def test_the_live_gravestone_then_selloff_confirms_a_short():
    """The two bars that should have been the entry, taken from real ADA data."""
    ok, why = sweep_confirmation([GRAVESTONE, PUSH_DOWN_HARD], is_long=False)
    assert ok is True, why


def test_a_hammer_at_a_swept_high_no_longer_counts_for_a_short():
    """Tiny body, long LOWER wick — buyers defending. Reading that as indecision before
    a short is the story backwards, and the old gate accepted it."""
    ok, why = sweep_confirmation([HAMMER, PUSH_DOWN], is_long=False)
    assert ok is False
    assert "wick" in why.lower() or "rejection" in why.lower()


def test_a_gravestone_at_a_swept_low_does_not_count_for_a_long():
    """Mirror: sellers slamming the top is not a reason to buy it."""
    assert sweep_confirmation([GRAVESTONE, PUSH_UP], is_long=True)[0] is False


def test_a_hammer_still_confirms_a_long():
    """Unchanged, and the reason the rule is directional rather than stricter overall."""
    assert sweep_confirmation([HAMMER, PUSH_UP], is_long=True)[0] is True


def test_a_balanced_doji_satisfies_neither_side():
    """Wicks both sides and no dominance is genuine indecision, but it records no
    rejection — nobody was pushed back. Not the pattern being described."""
    balanced = _c(100.0, 100.02, h=100.9, l=99.1)
    assert sweep_confirmation([balanced, PUSH_DOWN], is_long=False)[0] is False
    assert sweep_confirmation([balanced, PUSH_UP], is_long=True)[0] is False


def test_the_wick_must_dominate_not_merely_exist():
    """Upper wick present but no bigger than the lower one is not a rejection."""
    both = _c(100.0, 100.05, h=100.55, l=99.5)     # ~48% up, ~48% down
    assert sweep_confirmation([both, PUSH_DOWN], is_long=False)[0] is False


def test_the_reason_names_the_missing_wick_so_the_log_explains_itself():
    _, why = sweep_confirmation([HAMMER, PUSH_DOWN], is_long=False)
    assert len(why) > 20 and ("upper" in why.lower() or "rejection" in why.lower())

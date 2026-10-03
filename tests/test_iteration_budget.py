"""One pass must not outrun its own interval, and a short pass must not starve the same
symbols every time.

WHY (2026-10-02). A per-FETCH deadline existed (DATA_FETCH_TIMEOUT=120s); a per-ITERATION
one did not. 14 symbols x several fetches each meant a bad network minute could run far
past the loop interval — one real pass took 1591s (26.5 min) on a 15-minute loop, and the
bot completed 16 of ~26 possible passes that session.

That bound is a PRECONDITION of the 15M -> 5M cadence change, not a nicety: at 5M the same
overrun costs five passes instead of two, so shortening the interval without it would make
the lateness it is meant to fix WORSE.

ROTATION is the other half. Always starting at index 0 means a slow tape sacrifices the
tail of the watchlist every single pass — the 2026-09-21 incident where the bot "saw 3 of
its 8 symbols all session". With 14 symbols the last ones would simply never be evaluated.
"""
import pytest

from bot import strategy


class FakeStrat:
    """Only the two attributes _iteration_budget_sec actually reads."""
    def __init__(self, sleeptime):
        self.sleeptime = sleeptime

    _iteration_budget_sec = strategy.DebbieLaSMC._iteration_budget_sec


@pytest.mark.parametrize("sleeptime,expected", [
    ("5M", 300 * 0.8), ("15M", 900 * 0.8), ("1H", 3600 * 0.8), ("30S", 30 * 0.8),
])
def test_budget_is_derived_from_the_live_interval(sleeptime, expected):
    """Derived, never hardcoded — changing cadence must not leave a stale budget."""
    assert FakeStrat(sleeptime)._iteration_budget_sec() == pytest.approx(expected)


@pytest.mark.parametrize("bad", ["", "abc", None, "M", "15X15"])
def test_an_unparseable_interval_means_UNBOUNDED_not_zero(bad):
    """A budget we cannot compute must not silently truncate the watchlist to nothing."""
    assert FakeStrat(bad)._iteration_budget_sec() == 0.0


def test_the_cadence_is_five_minutes_and_overridable():
    assert strategy.LOOP_INTERVAL == "5M"
    src = open("bot/strategy.py", encoding="utf-8").read()
    assert 'os.getenv("LOOP_INTERVAL"' in src
    assert 'os.getenv("ITERATION_BUDGET_FRAC"' in src


def test_budget_leaves_room_inside_the_interval():
    """Must finish before the next pass is due, or the bot queues behind itself."""
    b = FakeStrat("5M")._iteration_budget_sec()
    assert 0 < b < 300


# ── rotation ─────────────────────────────────────────────────────────────────────────
SYMS = ["AAPL", "QQQ", "SPY", "NVDA", "TSLA", "GOOGL", "META",
        "MSFT", "AMZN", "AMD", "PLTR", "NFLX", "BE", "AMG"]


def _order(symbols, iter_count):
    n = len(symbols)
    shift = (iter_count % n) if n else 0
    return symbols[shift:] + symbols[:shift]


def test_rotation_starts_somewhere_new_each_pass():
    firsts = [_order(SYMS, i)[0] for i in range(len(SYMS))]
    assert len(set(firsts)) == len(SYMS), "some symbol never leads a pass"


def test_rotation_is_a_permutation_every_time():
    for i in range(0, 40):
        assert sorted(_order(SYMS, i)) == sorted(SYMS)


def test_every_symbol_gets_evaluated_even_if_only_half_a_pass_completes():
    """The real starvation case: if only the first 7 of 14 ever run, the back half must
    still be reached within a few passes rather than never."""
    seen = set()
    for i in range(len(SYMS)):
        seen.update(_order(SYMS, i)[:7])
    assert seen == set(SYMS), f"never evaluated: {set(SYMS) - seen}"


def test_an_empty_watchlist_does_not_crash_the_rotation():
    assert _order([], 3) == []

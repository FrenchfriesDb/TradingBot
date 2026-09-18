"""Waiting for the open must survive the Mac going to sleep.

WHAT HAPPENED (2026-09-18). The stock bot was started at 15:42 the previous afternoon,
logged "Sleeping until the market opens", and was still asleep at 07:15 — forty-five
minutes after the open, having traded nothing.

lumibot's Broker._await_market_to_open is one shot:

    def _await_market_to_open(self, timedelta=None, strategy=None):
        \"\"\"Executes infinite loop until market opens\"\"\"      # <- there is no loop
        isOpen = self.is_market_open()
        if not isOpen:
            time_to_open = self.get_time_to_open()
            ...
            self.sleep(sleeptime)                                # <- one time.sleep()

It computed 14h48m once and called time.sleep(53280). On macOS time.sleep is measured
against a clock that does NOT advance while the system is asleep, and `pmset -g log`
shows this machine slept 5.28 hours in that window ("Entering Sleep state ... Using
Bat" — caffeinate -i -s only holds sleep off on AC power). So the wait was due to
expire at 06:30 + 5h17m = 11:46, against a 13:00 close: about one hour of a six and a
half hour session, and only if the lid stayed open from then on.

The bug is not the sleeping laptop. It is computing a deadline once and trusting a
timer to still mean something hours later. Anything that stops a monotonic clock —
sleep, suspend, a VM pause — breaks it, and so does the opposite error: waking early
and charging into a market that has not opened.

So the wait re-checks. Sleep in bounded chunks, ask the broker again each time, and
let the broker's own is_market_open() be the authority on whether it is open — never
an arithmetic prediction made hours ago.
"""
import pytest

from bot.market_clock import await_market_open


class _Clock:
    """Records what it was asked to sleep, and can simulate the machine suspending."""

    def __init__(self, opens_after_checks=0, freeze_at=None, freeze_for=0.0):
        self.checks = 0
        self.opens_after_checks = opens_after_checks
        self.slept = []
        self.elapsed = 0.0
        self._freeze_at = freeze_at        # after this many sleeps, time stops advancing
        self._freeze_for = freeze_for

    def is_open(self):
        self.checks += 1
        return self.checks > self.opens_after_checks

    def time_to_open(self):
        # A deliberately stale, over-long estimate — exactly what the real bug relied on
        return 53280.0

    def sleep(self, seconds):
        self.slept.append(seconds)
        if self._freeze_at is not None and len(self.slept) >= self._freeze_at:
            return          # machine suspended: the timer did not actually advance
        self.elapsed += seconds


def test_an_already_open_market_is_not_slept_on_at_all():
    c = _Clock(opens_after_checks=0)
    assert await_market_open(c.is_open, c.time_to_open, c.sleep) is True
    assert c.slept == []


def test_it_rechecks_instead_of_trusting_one_long_timer():
    """The heart of it: time_to_open says 14.8 hours every single time, but the market
    actually opens after three checks. A one-shot sleep would still be asleep."""
    c = _Clock(opens_after_checks=3)
    assert await_market_open(c.is_open, c.time_to_open, c.sleep, max_chunk=60) is True
    assert len(c.slept) == 3
    assert all(s <= 60 for s in c.slept), c.slept


def test_no_single_sleep_can_outlive_a_suspend():
    """53280s was the actual value. Nothing may sleep on it in one go."""
    c = _Clock(opens_after_checks=5)
    await_market_open(c.is_open, c.time_to_open, c.sleep, max_chunk=60)
    assert max(c.slept) <= 60, f"slept {max(c.slept)}s in one call — a suspend eats it"


def test_a_suspended_machine_still_opens_on_time():
    """Simulates the real failure: from the second sleep onward the clock stops
    advancing, as macOS does. The wait must still end when the market opens, because
    it asks the broker rather than counting down."""
    c = _Clock(opens_after_checks=4, freeze_at=2)
    assert await_market_open(c.is_open, c.time_to_open, c.sleep, max_chunk=60) is True
    assert c.elapsed < 53280, "it must not have needed the full predicted wait"


def test_it_gives_up_rather_than_waiting_for_ever():
    """A market that never reports open (a holiday, a broker outage, a bad calendar)
    must not pin the bot in a silent loop until someone notices days later."""
    c = _Clock(opens_after_checks=10**9)
    assert await_market_open(c.is_open, c.time_to_open, c.sleep,
                             max_chunk=60, max_wait=300) is False
    assert sum(c.slept) <= 300 + 60


def test_the_chunk_never_overshoots_a_near_open():
    """With 20s to go it should sleep 20, not a full minute — waking late into an open
    market is the same class of error, just smaller."""
    c = _Clock(opens_after_checks=1)
    c.time_to_open = lambda: 20.0
    await_market_open(c.is_open, c.time_to_open, c.sleep, max_chunk=60)
    assert c.slept == [20.0]


def test_a_negative_or_zero_estimate_still_sleeps_a_little():
    """get_time_to_open() can return <= 0 while is_market_open() still says closed
    (pre-open auction, a calendar edge). Sleeping 0 there would spin the CPU hot."""
    c = _Clock(opens_after_checks=2)
    c.time_to_open = lambda: -5.0
    await_market_open(c.is_open, c.time_to_open, c.sleep, max_chunk=60)
    assert all(s > 0 for s in c.slept), c.slept


def test_a_broker_that_raises_is_retried_rather_than_crashing_the_bot():
    """is_market_open() hits the network. A blip during the overnight wait must not
    take the strategy down before the session it was waiting for."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("no route to host")
        return calls["n"] > 4

    slept = []
    assert await_market_open(flaky, lambda: 600.0, slept.append, max_chunk=30) is True
    assert slept, "should have waited through the errors rather than returning at once"


def test_an_estimate_that_raises_falls_back_to_the_chunk():
    def bad_estimate():
        raise ValueError("calendar unavailable")
    c = _Clock(opens_after_checks=2)
    await_market_open(c.is_open, bad_estimate, c.sleep, max_chunk=45)
    assert c.slept == [45, 45]

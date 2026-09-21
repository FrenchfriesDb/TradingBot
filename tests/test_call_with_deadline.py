"""No call inside a trading loop may block for eight hours.

WHAT HAPPENED (2026-09-21). The stock bot woke correctly for the open, processed AAPL,
QQQ and SPY in twenty seconds — SPY even armed a zone, "STEP 2: Bullish OB/FVG locked |
766.80-766.93" — and then:

    06:44:11  Getting historical prices for NVDA, 200 bars of 4 hours (48000 minute bars)
    06:44:12  Mac enters Sleep                     <- one second later
       ...8h16m...
    15:00:04  the call returns, two hours after the close

The machine slept one second into the request. The TCP connection died with it, and
lumibot's get_historical_prices() takes no timeout, so the strategy blocked on a socket
that was never coming back. It saw 3 of its 8 symbols all session and the one setup it
found was never followed up. `lsof` showed the corpse: a CLOSE_WAIT socket.

That is the whole reason this bot has four entry-ish lines in its entire history. It is
not being filtered out by strategy gates; it barely gets to run.

WHAT THIS CANNOT DO. A thread blocked on a dead socket does not stop existing because we
stopped waiting for it — Python cannot kill it. So this ABANDONS the call rather than
cancelling it, exactly as the AI confirmation path in binance_bot already does
(`_executor.shutdown(wait=False)`), and the trading loop moves to the next symbol. The
leaked thread finishes or it does not; either way one symbol is lost instead of a session.
"""
import time

import pytest

from bot.market_clock import call_with_deadline


def test_a_fast_call_returns_its_value():
    value, outcome = call_with_deadline(lambda: 42, timeout=5)
    assert value == 42 and outcome == "ok"


def test_a_slow_call_is_abandoned_at_the_deadline():
    """The NVDA case, compressed: the call never returns, and we stop waiting."""
    started = time.time()
    value, outcome = call_with_deadline(lambda: time.sleep(30), timeout=0.2)
    assert outcome == "timeout"
    assert value is None
    assert time.time() - started < 5, "it waited for the call instead of the deadline"


def test_the_caller_can_supply_what_a_timeout_returns():
    value, outcome = call_with_deadline(lambda: time.sleep(30), timeout=0.2, default="skip")
    assert value == "skip" and outcome == "timeout"


def test_an_exception_is_reported_as_an_error_not_a_timeout():
    """A 404 and a dead socket need different handling; collapsing them hides which
    happened, and the eight-hour stall was only diagnosable because it was distinct."""
    def boom():
        raise ConnectionError("no route to host")
    value, outcome = call_with_deadline(boom, timeout=5)
    assert outcome == "error" and value is None


def test_a_falsy_result_is_still_a_success():
    """None and [] are legitimate answers from a data fetch — 'no bars' is not 'failed'."""
    for empty in (None, [], 0, False):
        value, outcome = call_with_deadline(lambda: empty, timeout=5)
        assert outcome == "ok", f"{empty!r} was misread as a failure"
        assert value == empty


def test_arguments_are_passed_through():
    value, outcome = call_with_deadline(lambda: sum([1, 2, 3]), timeout=5)
    assert value == 6 and outcome == "ok"


@pytest.mark.parametrize("bad_timeout", [0, -1])
def test_a_non_positive_deadline_still_runs_the_call_once(bad_timeout):
    """Config that disables the timeout must not disable the fetch."""
    value, outcome = call_with_deadline(lambda: 7, timeout=bad_timeout)
    assert value == 7 and outcome == "ok"


def test_abandoning_does_not_block_on_the_stuck_thread():
    """The point of shutdown(wait=False): two abandoned calls in a row must each cost
    the deadline, not the full runtime of the thread left behind."""
    started = time.time()
    for _ in range(2):
        call_with_deadline(lambda: time.sleep(20), timeout=0.2)
    assert time.time() - started < 5, "the second call waited on the first leaked thread"

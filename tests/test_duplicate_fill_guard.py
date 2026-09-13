"""Regression tests for the bracket-fallback duplicate-fill guard.

REAL INCIDENT 2026-08-14: two MSFT entries landed at the IDENTICAL price $494.77,
minutes apart, both closing near -3R (-$65.49 and -$61.99). The bracket POST raised
AFTER Alpaca had already accepted the order, and the fallback treated the exception as
"nothing happened" and fired a second, naked market order.

The rule these pin: re-send ONLY when the broker is provably clean. Fail closed.
"""
from bot.indicators import should_resend_entry_after_error as should_resend


def test_clean_broker_is_the_only_case_that_resends():
    # verified, no order, no position -> the entry genuinely never landed
    assert should_resend(broker_has_live_order=False, broker_has_position=False,
                         verification_ok=True) is True


def test_live_order_blocks_resend():
    # the POST raised but Alpaca has the order — this is the MSFT case
    assert should_resend(broker_has_live_order=True, broker_has_position=False,
                         verification_ok=True) is False


def test_open_position_blocks_resend():
    # order already filled into a position before the exception surfaced
    assert should_resend(broker_has_live_order=False, broker_has_position=True,
                         verification_ok=True) is False


def test_failed_verification_blocks_resend():
    # cannot prove the broker is clean -> assume it landed. A missed entry costs one
    # setup; a duplicate costs an unintended doubled position.
    assert should_resend(broker_has_live_order=False, broker_has_position=False,
                         verification_ok=False) is False


def test_failed_verification_blocks_even_when_broker_looks_busy():
    assert should_resend(broker_has_live_order=True, broker_has_position=True,
                         verification_ok=False) is False


def test_the_exact_msft_20260814_scenario():
    """Bracket POST timed out; the order was already live at Alpaca."""
    assert should_resend(broker_has_live_order=True, broker_has_position=False,
                         verification_ok=True) is False, (
        "would have re-sent and produced the second MSFT fill at 494.77")

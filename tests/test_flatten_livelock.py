"""A rejected flatten must NOT reset state — or the two guards livelock.

WHY (2026-10-03). GOOGL was entered 2026-08-18 and survived until 15:54 on 08-20. The
bot tried to flatten it four times and reported success every time. From the log:

    08:59:00  leftover guard -> cancel OCO -> submit market close
    08:59:01  ERROR "insufficient qty available for order (requested: 8, available: 0)"
                     existing_qty 8, held_for_orders 8
    08:59:01  "flatten close: cancelled OCO + submitted market close (8.0 shares)"
    09:01:54  "Re-attached protection — position was NAKED, posted GTC OCO"
    09:25 / 10:13 / 10:52 ... identical, all day

THE LIVELOCK: _flatten_one cancelled the OCO and submitted the close in the same breath.
Alpaca releases `held_for_orders` asynchronously, so the close was rejected. The result was
never checked, the success line printed anyway, and _reset() wiped the symbol's state — so
the bot FORGOT it held 8 shares. _ensure_protection then found a "naked" position and
re-posted the GTC OCO, which re-reserved the shares and blocked the next flatten.

Each guard is correct alone. Together they spin. Two fixes, both pinned here: wait for the
cancel to settle, and never reset on a close the broker refused.
"""
import io

import pytest

SRC = io.open("bot/strategy.py", encoding="utf-8").read()


def _flatten_body():
    i = SRC.index("def _flatten_one")
    j = SRC.index("def _has_open_orders")
    assert i < j, "_flatten_one should precede _has_open_orders"
    return SRC[i:j]


def test_the_close_result_is_checked():
    body = _flatten_body()
    assert "submitted = self.submit_order(order)" in body, (
        "submit_order's result is discarded again — a rejected close cannot be detected")


def test_a_rejected_close_returns_without_resetting():
    """The precise defect: _reset ran unconditionally, so the bot forgot the position."""
    body = _flatten_body()
    i = body.index("if submitted is None:")
    j = body.index("self._reset(symbol)")
    assert i < j, "the None-check must come BEFORE _reset"
    assert "return" in body[i:j], "a rejected close must return, not fall through to _reset"


def test_it_waits_for_the_cancel_to_settle():
    body = _flatten_body()
    assert "_has_open_orders" in body, (
        "no wait for held_for_orders to clear — the close will race the cancel again")


def test_the_wait_is_bounded():
    """A flatten that blocks forever is worse than one that races."""
    body = _flatten_body()
    assert "for _ in range(" in body, "the settle-wait must be bounded"


def test_has_open_orders_fails_OPEN():
    """If the broker read fails we must still ATTEMPT the close — not closing is worse."""
    i = SRC.index("def _has_open_orders")
    body = SRC[i:i + 1400]
    k = body.index("except Exception:")
    assert "return False" in body[k:k + 120], (
        "_has_open_orders must return False (proceed) when it cannot read the book")


def test_the_rejection_is_logged_loudly():
    body = _flatten_body()
    assert "REJECTED" in body and "STILL OPEN" in body, (
        "a failed flatten must say so — the old code printed success over an error line")

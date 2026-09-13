"""Unit tests for match_last_round_trip — pairs a symbol's most recent closing fill with
its true entry using ONLY the order sequence (no external is_long/bias trusted).

Regression target: a real corrupted Stock Ledger row (SPY, 2026-07-23) where a stale
self.bias("SHORT") made the OLD entry/exit matcher pick the sell-leg of one real trade
as "entry" and the buy-leg of a LATER, unrelated real trade as "exit" — fabricating a
round trip that never happened (-$916.90) out of two real ones (-$47.94 and -$185.42)."""
import pytest

from bot.indicators import match_last_round_trip


def _order(side, price, filled_at, status="filled", qty=10):
    return {"side": side, "status": status, "filled_avg_price": price,
            "filled_at": filled_at, "filled_qty": qty, "id": f"{side}-{filled_at}"}


def test_pairs_most_recent_exit_with_immediately_preceding_opposite_side_entry():
    orders = [
        _order("sell", 728.93, "2026-06-26T13:33:00Z"),
        _order("buy",  731.11, "2026-06-24T19:13:45Z"),
    ]
    entry, exit_ = match_last_round_trip(orders)
    assert entry["filled_avg_price"] == 731.11
    assert exit_["filled_avg_price"] == 728.93


def test_does_not_merge_across_two_separate_round_trips():
    # The real SPY regression: two independent round trips. Must return ONLY the more
    # recent one (748.03 -> 744.17), never splicing trip A's sell with trip B's buy.
    orders = [
        _order("sell", 744.17,      "2026-07-02T15:53:15Z"),
        _order("buy",  748.032916,  "2026-07-02T14:41:40Z"),
        _order("sell", 728.930909,  "2026-06-26T13:33:00Z"),
        _order("buy",  731.11,      "2026-06-24T19:13:45Z"),
    ]
    entry, exit_ = match_last_round_trip(orders)
    assert entry["filled_avg_price"] == 748.032916
    assert exit_["filled_avg_price"] == 744.17


def test_ignores_non_filled_orders():
    orders = [
        _order("sell", 744.17, "2026-07-02T15:53:15Z"),
        _order("sell", None,   None, status="canceled"),
        _order("buy",  748.03, "2026-07-02T14:41:40Z"),
        _order("buy",  None,   None, status="canceled"),
    ]
    entry, exit_ = match_last_round_trip(orders)
    assert entry["filled_avg_price"] == 748.03
    assert exit_["filled_avg_price"] == 744.17


def test_returns_none_when_fewer_than_two_filled_orders():
    orders = [_order("sell", 744.17, "2026-07-02T15:53:15Z")]
    assert match_last_round_trip(orders) == (None, None)


def test_returns_none_when_no_opposite_side_exists():
    orders = [
        _order("buy", 748.03, "2026-07-02T14:41:40Z"),
        _order("buy", 731.11, "2026-06-24T19:13:45Z"),
    ]
    assert match_last_round_trip(orders) == (None, None)


def test_sorts_internally_even_if_input_is_not_time_ordered():
    # Alpaca's raw payload order (by submission, not fill time) can't be trusted directly.
    orders = [
        _order("buy",  731.11,     "2026-06-24T19:13:45Z"),
        _order("sell", 744.17,     "2026-07-02T15:53:15Z"),
        _order("buy",  748.032916, "2026-07-02T14:41:40Z"),
        _order("sell", 728.930909, "2026-06-26T13:33:00Z"),
    ]
    entry, exit_ = match_last_round_trip(orders)
    assert entry["filled_avg_price"] == 748.032916
    assert exit_["filled_avg_price"] == 744.17


def test_infers_short_round_trip_correctly():
    orders = [
        _order("buy",  385.34,     "2026-07-14T14:35:45Z"),   # cover
        _order("sell", 387.656539, "2026-07-02T14:13:43Z"),   # short entry
    ]
    entry, exit_ = match_last_round_trip(orders)
    assert entry["side"] == "sell"
    assert exit_["side"] == "buy"

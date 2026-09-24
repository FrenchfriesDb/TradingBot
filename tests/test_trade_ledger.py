"""A trade must survive the network call that records it.

Eight closed trades never reached the Crypto Ledger tab — connection resets, no route to
host, and one iCloud EDEADLK reading google_credentials.json. log_trade() is fail-soft,
which is correct (a Sheets outage must not crash the trading loop), but it was also
fire-and-forget: no retry, no queue. Those eight are gone, and the journal shows a P&L
curve with holes and no sign that anything is missing — worse than an outage, because it
still looks complete.
"""
import json
import os

import pytest

from bot.trade_ledger import (append_row, read_rows, pending_rows, flush_pending,
                              ledger_path)


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "crypto.jsonl")


def _row(i, sent=False):
    r = {"ticker": f"SYM{i}", "pnl": i * 1.5, "side": "LONG"}
    if sent:
        r["sent"] = True
    return r


# ── durability ───────────────────────────────────────────────────────────────

def test_a_row_is_on_disk_before_anything_is_sent(path):
    """The whole point: the trade exists locally even if Sheets never succeeds."""
    append_row(path, _row(1))
    assert read_rows(path), "the trade must be durable before the network is involved"
    flush_pending(path, send=lambda r: False)
    assert read_rows(path), "a failed send must not remove it either"
    assert len(pending_rows(path)) == 1


def test_append_then_read_round_trips(path):
    assert append_row(path, _row(1))
    assert read_rows(path) == [{"ticker": "SYM1", "pnl": 1.5, "side": "LONG"}]


def test_rows_accumulate_in_order(path):
    for i in range(4):
        append_row(path, _row(i))
    assert [r["ticker"] for r in read_rows(path)] == ["SYM0", "SYM1", "SYM2", "SYM3"]


def test_a_corrupt_line_does_not_destroy_the_rest(path):
    append_row(path, _row(1))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
    append_row(path, _row(2))
    got = [r["ticker"] for r in read_rows(path)]
    assert got == ["SYM1", "SYM2"], "one bad tail must not hide the other trades"


def test_unserialisable_values_do_not_silently_drop_the_trade(path):
    """default=str means a datetime or Decimal still records rather than vanishing."""
    import datetime as dt
    assert append_row(path, {"ticker": "X", "when": dt.datetime(2026, 9, 24, 12, 0)})
    assert read_rows(path)[0]["when"].startswith("2026-09-24")


def test_reading_a_missing_file_is_empty_not_an_error(tmp_path):
    assert read_rows(str(tmp_path / "nope.jsonl")) == []


# ── the retry ────────────────────────────────────────────────────────────────

def test_a_failed_send_leaves_the_row_pending(path):
    append_row(path, _row(1))
    sent, still = flush_pending(path, send=lambda r: False)
    assert (sent, still) == (0, 1)
    assert len(pending_rows(path)) == 1


def test_a_later_flush_recovers_it(path):
    append_row(path, _row(1))
    flush_pending(path, send=lambda r: False)
    sent, still = flush_pending(path, send=lambda r: True)
    assert (sent, still) == (1, 0)
    assert pending_rows(path) == []


def test_an_exception_from_send_is_a_failure_not_a_loss(path):
    """A Sheets outage raising must never consume the trade it failed to record."""
    def boom(r):
        raise ConnectionResetError(54, "Connection reset by peer")
    append_row(path, _row(1))
    sent, still = flush_pending(path, send=boom)
    assert (sent, still) == (0, 1)
    assert len(pending_rows(path)) == 1


def test_already_sent_rows_are_not_resent(path):
    append_row(path, _row(1, sent=True))
    append_row(path, _row(2))
    seen = []
    flush_pending(path, send=lambda r: seen.append(r["ticker"]) or True)
    assert seen == ["SYM2"], "a re-flush must not duplicate rows in the journal"


def test_a_partial_failure_keeps_exactly_the_failures_pending(path):
    for i in range(5):
        append_row(path, _row(i))
    flush_pending(path, send=lambda r: r["ticker"] in ("SYM1", "SYM3"))
    assert sorted(r["ticker"] for r in pending_rows(path)) == ["SYM0", "SYM2", "SYM4"]


def test_the_limit_bounds_one_flush_without_losing_the_remainder(path):
    for i in range(10):
        append_row(path, _row(i))
    sent, still = flush_pending(path, send=lambda r: True, limit=3)
    assert (sent, still) == (3, 7)
    assert flush_pending(path, send=lambda r: True, limit=100) == (7, 0)


def test_flushing_an_empty_ledger_is_a_no_op(path):
    assert flush_pending(path, send=lambda r: True) == (0, 0)


def test_the_file_is_still_valid_jsonl_after_a_flush(path):
    for i in range(3):
        append_row(path, _row(i))
    flush_pending(path, send=lambda r: r["ticker"] != "SYM1")
    with open(path, encoding="utf-8") as fh:
        rows = [json.loads(l) for l in fh if l.strip()]
    assert len(rows) == 3
    assert [r.get("sent", False) for r in rows] == [True, False, True]


# ── where it lives ───────────────────────────────────────────────────────────

def test_the_ledger_is_NOT_stored_inside_the_icloud_synced_repo():
    """Putting the recovery file next to the thing it recovers from would be circular —
    the repo is on an iCloud Desktop, which is what produced the EDEADLK."""
    p = ledger_path("crypto")
    assert "Desktop" not in p and "TradingBot" not in p
    assert "Application Support" in p


def test_one_file_per_account():
    assert ledger_path("crypto") != ledger_path("stock")

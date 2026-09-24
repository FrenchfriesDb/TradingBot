"""The manual close button, passed dashboard -> bot through a file.

The dashboard only reads the bots' state files and holds no exchange credentials, which
is worth keeping. So the button cannot place an order; it records a request, and the
bot's own 10-second watcher — the process that already owns the position and its stops —
acts on it.

The dangerous failure here is not "the button did nothing". It is a request that fires
LATER, against a position the operator never meant to close.
"""
import json

import pytest

from bot.close_requests import (request_close, pending_closes, clear_close,
                                purge_expired, DEFAULT_TTL)

NOW = 1_000_000.0


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "close_requests.json")


# ── the happy path ───────────────────────────────────────────────────────────

def test_a_request_is_visible_to_the_bot(path):
    assert request_close(path, "DOGE/USD", now=NOW)
    assert pending_closes(path, now=NOW) == ["DOGE/USD"]


def test_several_symbols_are_independent(path):
    request_close(path, "DOGE/USD", now=NOW)
    request_close(path, "ASTER/USD", now=NOW)
    assert pending_closes(path, now=NOW) == ["ASTER/USD", "DOGE/USD"]
    clear_close(path, "DOGE/USD")
    assert pending_closes(path, now=NOW) == ["ASTER/USD"]


def test_no_file_means_nothing_pending(tmp_path):
    assert pending_closes(str(tmp_path / "nope.json")) == []


# ── it must not fire twice ───────────────────────────────────────────────────

def test_honouring_a_request_consumes_it(path):
    """Otherwise the bot closes, sees the request still there, and tries again — against
    whatever position happens to exist next."""
    request_close(path, "DOGE/USD", now=NOW)
    clear_close(path, "DOGE/USD")
    assert pending_closes(path, now=NOW) == []


def test_clearing_something_that_was_never_requested_is_harmless(path):
    assert clear_close(path, "NOPE/USD") is True


# ── it must not fire LATE ────────────────────────────────────────────────────

def test_a_stale_request_is_ignored(path):
    """A file left by a crashed process must not shut a brand-new position hours later.
    The bot could be flat, re-enter, and be closed instantly by an old click."""
    request_close(path, "DOGE/USD", now=NOW)
    assert pending_closes(path, now=NOW + DEFAULT_TTL + 1) == []


def test_a_request_is_still_live_just_inside_the_window(path):
    request_close(path, "DOGE/USD", now=NOW)
    assert pending_closes(path, now=NOW + DEFAULT_TTL - 1) == ["DOGE/USD"]


def test_small_clock_skew_does_not_discard_a_fresh_request(path):
    """Sleep/wake moves the clock; a request from 'the future' is fresh, not invalid."""
    request_close(path, "DOGE/USD", now=NOW + 30)
    assert pending_closes(path, now=NOW) == ["DOGE/USD"]


def test_purging_drops_only_the_expired_ones(path):
    request_close(path, "OLD/USD", now=NOW)
    request_close(path, "NEW/USD", now=NOW + DEFAULT_TTL)
    assert purge_expired(path, now=NOW + DEFAULT_TTL + 1) == 1
    assert pending_closes(path, now=NOW + DEFAULT_TTL + 1) == ["NEW/USD"]


# ── corrupt input must never close a position ────────────────────────────────

def test_a_corrupt_file_closes_nothing(path):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert pending_closes(path, now=NOW) == []


def test_a_non_dict_file_closes_nothing(path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(["DOGE/USD"], fh)
    assert pending_closes(path, now=NOW) == []


def test_an_unreadable_timestamp_is_skipped_not_treated_as_now(path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"DOGE/USD": "whenever", "ASTER/USD": NOW}, fh)
    assert pending_closes(path, now=NOW) == ["ASTER/USD"]


@pytest.mark.parametrize("bad", ["", None])
def test_an_empty_symbol_is_refused(path, bad):
    assert request_close(path, bad, now=NOW) is False
    assert pending_closes(path, now=NOW) == []


def test_the_file_stays_valid_json_across_operations(path):
    request_close(path, "A/USD", now=NOW)
    request_close(path, "B/USD", now=NOW)
    clear_close(path, "A/USD")
    with open(path, encoding="utf-8") as fh:
        assert list(json.load(fh)) == ["B/USD"]

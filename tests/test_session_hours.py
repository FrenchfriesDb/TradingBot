"""Regression target: NVDA (85 sh, entered 10:19 AM ET) rode into the weekend
completely open. Root cause — the EOD flatten only ran INSIDE the live trading loop
(on_trading_iteration's EOD_FLATTEN_MIN check); before_starting_trading's startup
reconcile never asked "is the market even open right now?" So restarting the bot at
any time outside the regular session (exactly what happens on every normal restart —
including the 2:53 PM PDT / 5:53 PM ET restart that produced this bug) silently skips
the flatten. is_regular_session() lets startup catch what the live loop missed."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.indicators import is_regular_session, is_leftover_position

_ET = ZoneInfo("America/New_York")


def test_midday_weekday_is_open():
    assert is_regular_session(weekday=1, hour=10, minute=0) is True   # Tue 10:00am


def test_before_open_is_closed():
    assert is_regular_session(weekday=1, hour=9, minute=29) is False  # Tue 9:29am


def test_at_open_boundary_is_open():
    assert is_regular_session(weekday=1, hour=9, minute=30) is True   # Tue 9:30am


def test_at_close_boundary_is_closed():
    assert is_regular_session(weekday=1, hour=16, minute=0) is False  # Tue 4:00pm


def test_just_before_close_is_open():
    assert is_regular_session(weekday=1, hour=15, minute=59) is True  # Tue 3:59pm


def test_weekend_is_closed():
    assert is_regular_session(weekday=5, hour=12, minute=0) is False  # Sat noon
    assert is_regular_session(weekday=6, hour=12, minute=0) is False  # Sun noon


def test_the_actual_bug_scenario_friday_evening_restart_is_closed():
    # 2026-07-24 was a Friday (weekday=4); the bot restarted 5:53 PM ET holding NVDA.
    assert is_regular_session(weekday=4, hour=17, minute=53) is False


# ── is_leftover_position: the Fri→Mon "held all weekend" case the safety net missed ──
# is_regular_session catches a restart OUTSIDE hours, but if the bot is restarted DURING
# Monday's session still holding Friday's NVDA, it looks like a fresh mid-session position.
# is_leftover_position flags it by comparing the REAL entry DATE (ET) to today.
def test_friday_entry_seen_monday_is_leftover():
    now   = datetime(2026, 7, 27, 10, 0, tzinfo=_ET)      # Monday 10:00am ET
    entry = "2026-07-24T14:19:39+00:00"                   # Fri 10:19am ET (real NVDA fill)
    assert is_leftover_position(entry, now) is True


def test_same_day_entry_is_not_leftover():
    now   = datetime(2026, 7, 24, 15, 0, tzinfo=_ET)      # Fri 3:00pm ET
    entry = "2026-07-24T14:19:39+00:00"                   # Fri 10:19am ET — same session
    assert is_leftover_position(entry, now) is False


def test_overnight_entry_yesterday_is_leftover():
    now   = datetime(2026, 7, 24, 9, 45, tzinfo=_ET)      # Fri 9:45am ET (just after open)
    entry = "2026-07-23T19:30:00+00:00"                   # Thu 3:30pm ET
    assert is_leftover_position(entry, now) is True


def test_missing_entry_is_not_leftover():
    assert is_leftover_position(None, datetime(2026, 7, 24, 15, 0, tzinfo=_ET)) is False


def test_unparseable_entry_is_not_leftover():
    assert is_leftover_position("garbage", datetime(2026, 7, 24, 15, 0, tzinfo=_ET)) is False


def test_naive_utc_entry_string_still_compares_correctly():
    # Python isoformat() sometimes emits naive strings — treat as UTC, don't crash.
    now   = datetime(2026, 7, 27, 10, 0, tzinfo=_ET)      # Monday
    assert is_leftover_position("2026-07-24T14:19:39", now) is True


# ── needs_eod_catchup_flatten: the missed-window safety net ─────────────────────────
# REAL INCIDENT 2026-07-28: NVDA + GOOGL held open 5.5h past the 4pm close while
# tradingbot.py ran continuously (no restart involved this time — the process was
# alive through the whole close). Root cause: EOD_FLATTEN_MIN=15 sits EXACTLY equal
# to the 15-min execution interval, and _minutes_to_close() jumps straight from a
# real number to None the instant "now >= close" — a single iteration that overruns
# (slow API calls across 8 symbols, network lag) can skip the entire pre-close window
# and land past the close with ZERO catch-up, silently disabling the guard all night.
# needs_eod_catchup_flatten() is an explicit "did today's EOD flatten actually happen
# yet?" check with a generous post-close grace window, independent of loop timing.
from bot.indicators import needs_eod_catchup_flatten


def test_just_past_close_not_yet_flattened_needs_catchup():
    now = datetime(2026, 7, 28, 16, 5, tzinfo=_ET)   # 4:05pm ET, 5 min past close
    assert needs_eod_catchup_flatten(now, already_flattened_today=False) is True


def test_past_close_but_already_flattened_today_skips():
    now = datetime(2026, 7, 28, 16, 5, tzinfo=_ET)
    assert needs_eod_catchup_flatten(now, already_flattened_today=True) is False


def test_well_past_grace_window_does_not_fire_late_at_night():
    now = datetime(2026, 7, 28, 21, 39, tzinfo=_ET)  # 9:39pm ET — the actual incident time
    assert needs_eod_catchup_flatten(now, already_flattened_today=False) is False


def test_before_close_no_catchup_needed():
    now = datetime(2026, 7, 28, 15, 0, tzinfo=_ET)   # 3:00pm ET — still trading
    assert needs_eod_catchup_flatten(now, already_flattened_today=False) is False


def test_weekend_never_needs_catchup():
    now = datetime(2026, 7, 25, 16, 5, tzinfo=_ET)   # Saturday
    assert needs_eod_catchup_flatten(now, already_flattened_today=False) is False


def test_exactly_at_close_needs_catchup():
    now = datetime(2026, 7, 28, 16, 0, tzinfo=_ET)   # exactly 4:00pm
    assert needs_eod_catchup_flatten(now, already_flattened_today=False) is True


def test_exactly_at_grace_boundary_still_fires():
    now = datetime(2026, 7, 28, 16, 45, tzinfo=_ET)  # 45 min past close, the boundary
    assert needs_eod_catchup_flatten(now, already_flattened_today=False) is True


def test_one_minute_past_grace_boundary_stops_firing():
    now = datetime(2026, 7, 28, 16, 46, tzinfo=_ET)  # 46 min past close
    assert needs_eod_catchup_flatten(now, already_flattened_today=False) is False

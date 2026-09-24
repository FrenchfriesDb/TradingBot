"""The alarm that should have fired on 2026-09-22.

The stock bot went silent for 3h38m of a 6.5-hour session — lid closed, machine asleep.
SPY sat inside its armed zone for 31 minutes inside that window. Nothing noticed. Every
entry-quality fix in this repo is worth nothing while the process is down.

The two ways a watchdog dies:
  * it cries wolf overnight and gets muted within a week
  * it fires every tick, so a 3-hour outage becomes 36 notifications
Both are pinned below.
"""
import pytest

from bot.watchdog import (silence_verdict, is_active_now,
                          CRYPTO_MAX_SILENCE, STOCK_MAX_SILENCE)

NOW = 1_000_000.0
M = 60.0


# ── the real incident ────────────────────────────────────────────────────────

def test_the_2026_09_22_gap_would_have_fired():
    """3h38m of silence during the session."""
    alarm, why = silence_verdict(NOW, NOW - 218 * M, STOCK_MAX_SILENCE, active=True)
    assert alarm and "SILENT for 218m" in why


def test_a_normal_15m_loop_is_quiet():
    alarm, _ = silence_verdict(NOW, NOW - 14 * M, STOCK_MAX_SILENCE, active=True)
    assert not alarm


def test_a_crypto_bot_missing_three_cycles_fires():
    alarm, why = silence_verdict(NOW, NOW - 16 * M, CRYPTO_MAX_SILENCE, active=True)
    assert alarm and "SILENT" in why


# ── it must not cry wolf overnight ───────────────────────────────────────────

def test_the_stock_bots_normal_overnight_silence_is_NOT_an_alarm():
    """Its real gap is 14h49m, initializing -> next open. A naive mtime check fires
    every night and gets muted within a week."""
    alarm, why = silence_verdict(NOW, NOW - 889 * M, STOCK_MAX_SILENCE, active=False)
    assert not alarm and "outside its active window" in why


def test_a_dead_stock_bot_out_of_hours_is_also_not_an_alarm():
    alarm, _ = silence_verdict(NOW, NOW - 889 * M, STOCK_MAX_SILENCE,
                               active=False, alive=False)
    assert not alarm


def test_crypto_is_active_around_the_clock():
    for hour in (0, 3, 9, 14, 23):
        assert is_active_now("crypto", 2, hour, 0, lambda *a: False)


def test_stock_activity_follows_the_same_session_helper_the_bot_uses():
    """Injected, so the watchdog and the bot can never disagree about the open."""
    assert is_active_now("stock", 2, 10, 0, lambda w, h, m: True)
    assert not is_active_now("stock", 2, 21, 0, lambda w, h, m: False)


# ── a dead process is immediate ──────────────────────────────────────────────

def test_a_dead_process_in_hours_fires_without_waiting_out_the_silence():
    """Waiting 15 more quiet minutes adds to an outage already in progress."""
    alarm, why = silence_verdict(NOW, NOW - 1 * M, STOCK_MAX_SILENCE,
                                 active=True, alive=False)
    assert alarm and "NOT RUNNING" in why


def test_a_missing_log_file_fires():
    alarm, why = silence_verdict(NOW, None, CRYPTO_MAX_SILENCE, active=True)
    assert alarm and "missing" in why


# ── it must not repeat every tick ────────────────────────────────────────────

def test_a_second_check_inside_the_repeat_window_stays_quiet():
    alarm, why = silence_verdict(NOW, NOW - 200 * M, STOCK_MAX_SILENCE, active=True,
                                 last_alarm_ts=NOW - 5 * M)
    assert not alarm and "already alarmed" in why


def test_it_speaks_again_once_the_repeat_window_passes():
    alarm, _ = silence_verdict(NOW, NOW - 200 * M, STOCK_MAX_SILENCE, active=True,
                               last_alarm_ts=NOW - 31 * M)
    assert alarm


def test_a_three_hour_outage_checked_every_5_minutes_alarms_a_handful_of_times():
    """36 checks must not be 36 notifications."""
    last, fired = None, 0
    for i in range(36):
        t = NOW + i * 5 * M
        alarm, _ = silence_verdict(t, NOW - 30 * M, CRYPTO_MAX_SILENCE,
                                   active=True, last_alarm_ts=last)
        if alarm:
            fired += 1
            last = t
    assert 1 <= fired <= 7, fired


def test_the_repeat_mute_applies_to_a_dead_process_too():
    alarm, _ = silence_verdict(NOW, NOW - 1 * M, CRYPTO_MAX_SILENCE, active=True,
                               alive=False, last_alarm_ts=NOW - 2 * M)
    assert not alarm


# ── junk ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["x", object()])
def test_an_unreadable_last_alarm_does_not_suppress_a_real_alarm(bad):
    """Fail LOUD here: a corrupt state file must not silence the watchdog."""
    alarm, _ = silence_verdict(NOW, NOW - 200 * M, STOCK_MAX_SILENCE, active=True,
                               last_alarm_ts=bad)
    assert alarm


def test_a_log_mtime_in_the_future_is_treated_as_fresh():
    """Clock skew after a sleep/wake must not read as a 200-minute outage."""
    alarm, _ = silence_verdict(NOW, NOW + 5 * M, CRYPTO_MAX_SILENCE, active=True)
    assert not alarm

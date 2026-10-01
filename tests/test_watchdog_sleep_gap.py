"""A sleep gap leaves no trace in the logs — detect it from our OWN heartbeat.

WHY (2026-10-01). On 2026-09-29 the machine slept 77 minutes mid-session (`pmset`:
"Entering Sleep state ... 4654 secs", on battery, lid shut). binance_bot was frozen
throughout and missed a BTC sweep the operator watched happen on the chart. ZERO alarms.

silence_verdict structurally cannot see it:
  • the watchdog is launchd StartInterval=300, so it is asleep too;
  • on wake the bot resumes within seconds, so the log mtime is fresh again;
  • silence_verdict only compares `now` to that mtime, and BOTH jumped forward together.

The gap is visible in exactly one place: the distance between consecutive watchdog runs.
No pmset needed — the absence of our own heartbeat is the evidence.

This is the opposite failure from the one fixed in tests/test_watchdog_both_log_paths.py,
which ALARMED on a healthy bot. Same component, two inverted blind spots, neither fixable
by tuning thresholds.
"""
import pytest

from bot.watchdog import coverage_gap_verdict, CRYPTO_MAX_SILENCE, STOCK_MAX_SILENCE

MIN = 60.0
NOW = 2_000_000.0


def test_the_real_77_minute_sleep_now_alarms():
    """The exact 2026-09-29 shape: previous run 77 minutes ago, crypto always active."""
    alarm, why = coverage_gap_verdict(NOW, NOW - 77 * MIN, CRYPTO_MAX_SILENCE, active=True)
    assert alarm is True
    assert "77m" in why and "NOT WATCHED" in why


def test_a_normal_five_minute_cadence_is_silent():
    alarm, why = coverage_gap_verdict(NOW, NOW - 5 * MIN, CRYPTO_MAX_SILENCE, active=True)
    assert alarm is False and why is None


def test_launchd_firing_late_is_not_an_outage():
    """Slack over the schedule: a couple of minutes of drift under load is normal."""
    alarm, _ = coverage_gap_verdict(NOW, NOW - 9 * MIN, CRYPTO_MAX_SILENCE, active=True)
    assert alarm is False


def test_first_ever_run_cannot_alarm():
    alarm, why = coverage_gap_verdict(NOW, None, CRYPTO_MAX_SILENCE, active=True)
    assert alarm is False and why is None


def test_overnight_sleep_does_not_wake_the_stock_bot_alarm():
    """The cry-wolf rule this file was built around: a laptop shut overnight must not
    alarm for a bot that is supposed to be silent."""
    alarm, why = coverage_gap_verdict(NOW, NOW - 600 * MIN, STOCK_MAX_SILENCE, active=False)
    assert alarm is False and why is None


def test_one_sleep_produces_one_notification():
    """repeat_after muting — a long gap must not re-alarm on every 5-minute tick."""
    alarm, why = coverage_gap_verdict(NOW, NOW - 77 * MIN, CRYPTO_MAX_SILENCE, active=True,
                                      last_alarm_ts=NOW - 60)
    assert alarm is False
    assert why is not None and "already alarmed" in why


def test_a_corrupt_heartbeat_does_not_crash_or_fabricate():
    for bad in ("not-a-number", object(), float("nan")):
        alarm, _ = coverage_gap_verdict(NOW, bad, CRYPTO_MAX_SILENCE, active=True)
        assert alarm is False


@pytest.mark.parametrize("mins,expect", [(10, False), (20, False), (26, True), (120, True)])
def test_stock_threshold_tracks_its_own_silence_limit(mins, expect):
    """The gap bar is the bot's own max_silence, not a second hardcoded number."""
    alarm, _ = coverage_gap_verdict(NOW, NOW - mins * MIN, STOCK_MAX_SILENCE, active=True)
    assert alarm is expect

"""The watchdog must stat BOTH log locations, or it alarms on healthy bots.

WHY (2026-10-01). A bot writes to two places and which one is live depends on how it was
launched:

  <repo>/logs/<bot>.log        tee_stdout_to() from inside Python — works under ANY launch
  ~/Library/Logs/debbiela/...  the launchd plist's stdout redirect — only under launchd

The watchdog statted only the launchd path. binance_bot and stock_bot, relaunched by hand
in a terminal on 2026-09-30, therefore read as `SILENT for 1534m` for 25 hours while both
were trading perfectly. That is a FALSE ALARM — the exact inverse of the outage this
component exists to catch — and it is also what produced a flatly wrong "both bots have
been dead for 25 hours" diagnosis.

Note the pair of opposite blind spots found a day apart: this one alarms on a healthy bot,
and the sleep gap (see the 2026-09-29 incident) stays silent through a real 77-minute
outage because `now` and the log mtime freeze together. Neither is fixed by tuning
thresholds.
"""
import pytest

from bot.watchdog import newest_log, silence_verdict

MIN = 60.0
NOW = 1_000_000.0


def test_picks_the_fresher_of_the_two():
    src, mt = newest_log([("launchd", NOW - 9000), ("BARE", NOW - 60)])
    assert (src, mt) == ("BARE", NOW - 60)


def test_picks_launchd_when_it_is_the_fresher():
    src, mt = newest_log([("launchd", NOW - 30), ("BARE", NOW - 9000)])
    assert (src, mt) == ("launchd", NOW - 30)


def test_a_missing_path_is_skipped_not_treated_as_zero():
    """A bot that has never run bare has no repo log; that must not read as 1970."""
    src, mt = newest_log([("launchd", NOW - 120), ("BARE", None)])
    assert (src, mt) == ("launchd", NOW - 120)
    src, mt = newest_log([("launchd", None), ("BARE", NOW - 120)])
    assert (src, mt) == ("BARE", NOW - 120)


def test_no_logs_at_all_returns_none():
    assert newest_log([("launchd", None), ("BARE", None)]) == (None, None)
    assert newest_log([]) == (None, None)


def test_the_real_false_alarm_does_not_fire_any_more():
    """The exact 2026-09-30 shape: launchd log 25h stale, repo log 1 minute old."""
    src, mt = newest_log([("launchd", NOW - 1534 * MIN), ("BARE", NOW - 1 * MIN)])
    alarm, why = silence_verdict(NOW, mt, 15 * MIN, active=True, alive=True,
                                 source=None if src == "launchd" else src)
    assert alarm is False, f"still alarming on a healthy bare-launched bot: {why}"
    assert "BARE" in why, "operator cannot tell it is unsupervised"


def test_a_genuine_outage_still_alarms_with_both_paths_stale():
    src, mt = newest_log([("launchd", NOW - 90 * MIN), ("BARE", NOW - 95 * MIN)])
    alarm, why = silence_verdict(NOW, mt, 15 * MIN, active=True, alive=True,
                                 source=None if src == "launchd" else src)
    assert alarm is True
    assert "SILENT" in why


def test_supervised_bot_reason_carries_no_source_tag():
    """Normal operation must stay quiet in the log, not gain noise."""
    src, mt = newest_log([("launchd", NOW - 60), ("BARE", NOW - 9000)])
    _, why = silence_verdict(NOW, mt, 15 * MIN, active=True, alive=True,
                             source=None if src == "launchd" else src)
    assert "[" not in why


def test_a_dead_process_still_alarms_even_with_a_fresh_repo_log():
    """pgrep says gone; a stale-but-fresher file must not rescue it."""
    src, mt = newest_log([("launchd", NOW - 9000), ("BARE", NOW - 60)])
    alarm, why = silence_verdict(NOW, mt, 15 * MIN, active=True, alive=False,
                                 source=None if src == "launchd" else src)
    assert alarm is True and "NOT RUNNING" in why

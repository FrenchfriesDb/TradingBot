"""Alarm when a bot stops talking.

On 2026-09-22 the stock bot went silent for 3h38m of a 6.5-hour session — the lid was
closed and the machine slept. SPY spent 31 minutes inside its armed zone during that
window. Nothing noticed. On 2026-09-23 all three bots crash-looped under launchd for
hours; `launchctl list` showed `pid=-` and `last exit = 0`, which reads like a clean
exit, and the only real evidence was `runs = 23` buried in `launchctl print`.

Every entry-quality fix in this repo is worth nothing while a process is down, so this
watches the one signal that cannot lie: the log file's mtime.

TWO THINGS IT MUST NOT DO.

1. Cry wolf overnight. The stock bot is SUPPOSED to be silent when the market is shut —
   its normal gap is 14h49m, from "Strategy is initializing" to the next open. A naive
   mtime check would fire every single night and be muted within a week. So each bot
   declares when it should be talking, and silence outside that window is not an alarm.

2. Repeat every tick. An outage lasting three hours must not produce 36 notifications.
   Each bot re-alarms at most once per `repeat_after` seconds.

The decisions live here as pure functions so they can be tested against fixed clocks
instead of by waiting three hours for a real outage.
"""

def newest_log(entries):
    """(label, mtime) of the freshest log, or (None, None) if none exist.

    A bot writes to TWO places and they disagree depending on how it was launched
    (found the hard way on 2026-10-01):

      • <repo>/logs/<bot>.log         — written by tee_stdout_to() from inside Python, so
                                        it works under ANY launch, bare or supervised.
      • ~/Library/Logs/debbiela/...   — the launchd plist's stdout redirect, written ONLY
                                        while launchd owns the process.

    This watchdog used to stat the launchd path alone. A bot relaunched by hand in a
    terminal therefore read as `SILENT for 1534m` forever while trading perfectly — a
    FALSE ALARM, the exact opposite of the outage this file was written to catch, and it
    also led to a flatly wrong "both bots have been dead for 25 hours" diagnosis.

    `entries` is [(label, mtime_or_None), ...]; ties keep the first listed.
    """
    best_label, best_mtime = None, None
    for label, mtime in entries:
        if mtime is None:
            continue
        if best_mtime is None or mtime > best_mtime:
            best_label, best_mtime = label, mtime
    return best_label, best_mtime


CRYPTO_MAX_SILENCE = 15 * 60      # 5m loop, so three missed cycles
STOCK_MAX_SILENCE  = 25 * 60      # 15m loop, so ~1.5 missed cycles
REPEAT_AFTER       = 30 * 60


def is_active_now(kind, weekday, hour, minute, is_regular_session):
    """Should this bot be producing output right now?

    kind is "crypto" (24/7) or "stock" (regular session only). is_regular_session is
    injected — the same helper the stock bot itself uses — so the watchdog and the bot
    can never disagree about when the market is open.
    """
    if kind == "crypto":
        return True
    if kind == "stock":
        return bool(is_regular_session(weekday, hour, minute))
    return True


def silence_verdict(now_ts, log_mtime, max_silence, active,
                    last_alarm_ts=None, repeat_after=REPEAT_AFTER, alive=True,
                    source=None):
    """(should_alarm, reason). Pure: no clock, no filesystem, no notifications.

    A dead process during its active window alarms IMMEDIATELY — waiting out the silence
    window would add 15 quiet minutes to an outage already in progress.

    `source` labels WHICH log supplied the mtime (see newest_log). It is surfaced in the
    reason because a bot whose freshest log is the repo one is running BARE — healthy, but
    unsupervised and with no launchd auto-restart. That is worth seeing, not hiding.
    """
    if not active:
        return False, "outside its active window"
    if not alive:
        silent = None if log_mtime is None else max(0.0, now_ts - log_mtime)
        if _muted(now_ts, last_alarm_ts, repeat_after):
            return False, "process down (already alarmed)"
        extra = "" if silent is None else f", log {silent/60:.0f}m old"
        return True, f"process is NOT RUNNING{extra}"
    if log_mtime is None:
        if _muted(now_ts, last_alarm_ts, repeat_after):
            return False, "no log (already alarmed)"
        return True, "log file missing"
    silent = now_ts - log_mtime
    where = f" [{source}]" if source else ""
    if silent < max_silence:
        return False, f"last wrote {silent/60:.1f}m ago{where}"
    if _muted(now_ts, last_alarm_ts, repeat_after):
        return False, f"silent {silent/60:.0f}m (already alarmed){where}"
    return True, f"SILENT for {silent/60:.0f}m (limit {max_silence/60:.0f}m){where}"


def coverage_gap_verdict(now_ts, last_run_ts, max_silence, active,
                         last_alarm_ts=None, repeat_after=REPEAT_AFTER,
                         expected_interval=300.0):
    """(should_alarm, reason) for a stretch where NOBODY WAS WATCHING.

    THE SLEEP BLIND SPOT, and why silence_verdict cannot see it. On 2026-09-29 the machine
    slept 77 minutes mid-session ("Entering Sleep state ... 4654 secs", on battery, lid
    shut). The bot was frozen for all of it and missed a BTC sweep the operator watched
    happen on the chart. Zero alarms fired, because:

      • the watchdog is launchd StartInterval=300, so it does not run while asleep either;
      • on wake the BOT resumes within seconds, so log mtime is fresh again;
      • silence_verdict only ever compares `now` to that mtime. Both clocks jumped forward
        together, so the gap is invisible — it leaves no trace in the only signal we read.

    The gap IS visible in one place: the distance between consecutive watchdog runs. We
    are supposed to run every `expected_interval`; if the previous run was 77 minutes ago,
    nothing was observed for 77 minutes, whatever the logs now say. No pmset needed — the
    absence of our own heartbeat is the evidence.

    Deliberately reported as a COVERAGE gap, not a bot outage, because that is what is
    actually known: during it the machine was asleep or this watchdog was not running, and
    in either case the bots were not being watched and (if the machine slept) not trading.

    `active` gates it the same way silence_verdict does, so a laptop shut overnight does
    not alarm for the stock bot. Crypto is always active and will alarm — correct: a 24/7
    bot frozen for 77 minutes is a real outage, and the repeat_after mute keeps one sleep
    to one notification.
    """
    if not active or last_run_ts is None:
        return False, None
    try:
        gap = now_ts - float(last_run_ts)
    except (TypeError, ValueError):
        return False, None
    # NaN compares False against everything, so an unguarded NaN heartbeat falls straight
    # through the threshold check below and manufactures an alarm out of nothing.
    if gap != gap:
        return False, None
    # Allow generous slack over the schedule: launchd fires late under load, and a couple
    # of minutes of drift is not an outage.
    if gap <= max(max_silence, expected_interval * 2):
        return False, None
    if _muted(now_ts, last_alarm_ts, repeat_after):
        return False, f"coverage gap {gap/60:.0f}m (already alarmed)"
    return True, (f"NOT WATCHED for {gap/60:.0f}m — machine asleep or watchdog down; "
                  f"the bots were frozen or unobserved for that window")


def _muted(now_ts, last_alarm_ts, repeat_after):
    if last_alarm_ts is None:
        return False
    try:
        return (now_ts - float(last_alarm_ts)) < repeat_after
    except (TypeError, ValueError):
        return False

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


def _muted(now_ts, last_alarm_ts, repeat_after):
    if last_alarm_ts is None:
        return False
    try:
        return (now_ts - float(last_alarm_ts)) < repeat_after
    except (TypeError, ValueError):
        return False

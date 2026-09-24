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
                    last_alarm_ts=None, repeat_after=REPEAT_AFTER, alive=True):
    """(should_alarm, reason). Pure: no clock, no filesystem, no notifications.

    A dead process during its active window alarms IMMEDIATELY — waiting out the silence
    window would add 15 quiet minutes to an outage already in progress.
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
    if silent < max_silence:
        return False, f"last wrote {silent/60:.1f}m ago"
    if _muted(now_ts, last_alarm_ts, repeat_after):
        return False, f"silent {silent/60:.0f}m (already alarmed)"
    return True, f"SILENT for {silent/60:.0f}m (limit {max_silence/60:.0f}m)"


def _muted(now_ts, last_alarm_ts, repeat_after):
    if last_alarm_ts is None:
        return False
    try:
        return (now_ts - float(last_alarm_ts)) < repeat_after
    except (TypeError, ValueError):
        return False

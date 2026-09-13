"""Zero-dependency (stdlib `os` only) single-instance PID lock. Deliberately kept
separate from bot/indicators.py, which imports pandas at module level — binance_bot.py
avoids importing pandas until its background lazy-loader thread finishes (a fresh-boot
macOS Gatekeeper scan can block that for ~16 minutes), so the duplicate-process check
needs to run BEFORE that, at the very top of the script, with no heavy imports at all.
bot/indicators.py re-exports these two names for callers that already pay the pandas
import cost anyway (test_bot.py, bot/strategy.py) and for the test suite."""
import os


def should_refuse_duplicate_start(lock_pid, my_pid, other_pid_is_alive):
    """Pure decision at the core of a single-instance PID lock: given what's currently
    in the lock file (None if missing/corrupt), this process's own PID, and whether the
    locked PID is still alive, should THIS process refuse to start? True only when a
    DIFFERENT, currently-alive process holds the lock — a missing/corrupt/stale/
    self-owned lock always allows startup (and the caller reclaims it)."""
    if lock_pid is None or lock_pid == my_pid:
        return False
    return bool(other_pid_is_alive)


def acquire_single_instance_lock(lock_path):
    """Refuse to start if another LIVE process already holds this lock file; otherwise
    write our own PID and proceed. Returns True if the lock was acquired (safe to run),
    False if a real duplicate is already running (caller should exit immediately).
    Real incident: duplicate tradingbot.py instances raced writing strategy_state.json,
    zeroing out tracked entry_price for NVDA/GOOGL and silently losing a real ledger row
    — this has recurred four separate times across binance_bot.py, test_bot.py, and
    tradingbot.py in one session, always because a restart happened without confirming
    the old process had actually died first."""
    lock_pid = None
    try:
        with open(lock_path) as f:
            lock_pid = int(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        lock_pid = None
    my_pid = os.getpid()
    other_alive = False
    if lock_pid is not None and lock_pid != my_pid:
        try:
            os.kill(lock_pid, 0)   # signal 0: check liveness without actually signaling
            other_alive = True
        except OSError:
            other_alive = False
    if should_refuse_duplicate_start(lock_pid, my_pid, other_alive):
        return False
    with open(lock_path, "w") as f:
        f.write(str(my_pid))
    return True

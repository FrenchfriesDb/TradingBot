"""Regression target: duplicate processes have silently run FOUR separate times this
session (binance_bot.py twice, test_bot.py once, tradingbot.py twice more) — each time
because a restart happened without confirming the OLD process actually died first. Real
damage: two tradingbot.py instances raced writing strategy_state.json, corrupting
NVDA/GOOGL's tracked entry_price to 0 and silently losing a real +$64.48 ledger row.

should_refuse_duplicate_start() is the pure decision at the core of a PID-lock: given
what's currently in the lock file (or None if missing/corrupt) and whether that PID is
still alive, should THIS process refuse to start? The actual file I/O and os.kill(pid, 0)
liveness check are thin, untested wiring around this — this is the part worth getting
exactly right, since a wrong answer either lets duplicates back in (no protection) or
permanently blocks legitimate restarts (staler-than-stale lock)."""
import pytest

from bot.indicators import should_refuse_duplicate_start


def test_no_lock_file_proceeds():
    assert should_refuse_duplicate_start(lock_pid=None, my_pid=123, other_pid_is_alive=False) is False


def test_stale_lock_dead_pid_proceeds():
    # a lock file exists (PID 999) but that process is no longer running -> reclaim it
    assert should_refuse_duplicate_start(lock_pid=999, my_pid=123, other_pid_is_alive=False) is False


def test_live_other_pid_refuses():
    assert should_refuse_duplicate_start(lock_pid=999, my_pid=123, other_pid_is_alive=True) is True


def test_lock_already_ours_proceeds():
    # our own PID is somehow already in the lock file (e.g. a prior clean-exit that
    # didn't remove it, or a re-entrant call) -> never block ourselves
    assert should_refuse_duplicate_start(lock_pid=123, my_pid=123, other_pid_is_alive=True) is False


def test_corrupt_lock_treated_as_missing():
    # caller passes None for unparseable/corrupt lock contents, same as no file
    assert should_refuse_duplicate_start(lock_pid=None, my_pid=123, other_pid_is_alive=True) is False

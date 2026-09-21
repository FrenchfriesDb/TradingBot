"""A balance the broker never gave us must not become a row in the equity curve.

REAL CRASH 2026-09-21, from the stock bot's own log:

    15:03:43 | ERROR | 🌐 Network unreachable (DNS/connection failure) — Alpaca calls
                       are failing.
    15:03:47 | An error occurred during the on_trading_iteration lifecycle method:
                       unsupported operand type(s) for -: 'NoneType' and 'float'
      File "bot/strategy.py", line 2212, in _log_daily_snapshot_if_new_day
        daily_return_pct = ((balance - self._daily_open_balance) / ...
    TypeError: unsupported operand type(s) for -: 'NoneType' and 'float'
    Warning: Error shutting down scheduler: cannot join current thread

get_portfolio_value() returned None mid-outage and the snapshot did arithmetic on it,
taking down the whole trading iteration.

The crash was the visible half. The quiet half is worse: had _daily_open_balance ALSO
been None — a fresh process, which is exactly the state a crash-restart leaves you in —
the `if self._daily_open_balance` guard would have short-circuited to 0.0% instead, and
the function would have sailed on to write balance=None into the Stock Macro tab and
stamp _sheet_log_date = today. That tab gets ONE row per day and the Quant Desk equity
curve is drawn from it, so the day's real closing equity would have been unrecoverable
until midnight — silently, with no traceback to notice.

So the rule is not "don't crash on None", it is: an unreadable balance produces no row
at all. The iteration loop runs again in minutes and the network comes back.
"""
import math

import pytest

from bot.strategy import DebbieLaSMC


class _Stub:
    """Minimal stand-in for the strategy: just the attributes the snapshot touches."""

    def __init__(self, balance, daily_open=10_000.0, sheet_log_date=None):
        self._balance = balance
        self._daily_open_balance = daily_open
        self._daily_open_spy = 500.0
        self._sheet_log_date = sheet_log_date
        self.logged = []
        self.rows = []
        self.saved = 0

    # what the method calls on self
    def get_portfolio_value(self):
        return self._balance

    def get_last_price(self, symbol):
        return 500.0

    def log_message(self, msg, color=None):
        self.logged.append(msg)

    def _save_daily_snapshot_flag(self):
        self.saved += 1


def _run(stub, monkeypatch):
    """Call the real method against the stub, with the Sheets write captured."""
    import bot.strategy as S
    monkeypatch.setattr(S, "get_sheet_client", lambda: object())
    monkeypatch.setattr(S, "log_daily_snapshot",
                        lambda client, url, tab, *row: stub.rows.append((tab, row)))
    DebbieLaSMC._log_daily_snapshot_if_new_day(stub)


@pytest.mark.parametrize("unreadable", [None, float("nan"), "n/a", object()])
def test_an_unreadable_balance_writes_no_row_and_does_not_raise(unreadable, monkeypatch):
    stub = _Stub(balance=unreadable)
    _run(stub, monkeypatch)                      # the 2026-09-21 TypeError, if it regresses
    assert stub.rows == [], "a non-number reached the Macro tab"


@pytest.mark.parametrize("unreadable", [None, float("nan")])
def test_the_day_is_left_unlogged_so_the_next_iteration_retries(unreadable, monkeypatch):
    """The quiet half: stamping the date would burn the day's only snapshot."""
    stub = _Stub(balance=unreadable, daily_open=None)   # fresh process, as after a crash
    _run(stub, monkeypatch)
    assert stub._sheet_log_date is None, "the day was marked logged without a row"
    assert stub.saved == 0, "the skip flag was persisted for a day that never got written"


def test_the_skip_is_announced_rather_than_silent(monkeypatch):
    stub = _Stub(balance=None)
    _run(stub, monkeypatch)
    assert any("unreadable" in m.lower() for m in stub.logged), stub.logged


def test_a_readable_balance_still_writes_the_row(monkeypatch):
    """The guard must not swallow the normal path."""
    stub = _Stub(balance=10_500.0, daily_open=10_000.0)
    _run(stub, monkeypatch)
    assert len(stub.rows) == 1
    tab, (date_s, balance, ret_pct, spy, spy_pct) = stub.rows[0]
    assert tab == "Stock Macro"
    assert balance == 10_500.0
    assert ret_pct == pytest.approx(5.0)
    assert stub._sheet_log_date is not None and stub.saved == 1


def test_zero_is_a_real_balance_not_a_failure(monkeypatch):
    """A wiped-out account is a number. Only unreadable means skip."""
    stub = _Stub(balance=0.0, daily_open=10_000.0)
    _run(stub, monkeypatch)
    assert len(stub.rows) == 1 and stub.rows[0][1][1] == 0.0


def test_the_first_ever_snapshot_still_works_with_no_prior_balance(monkeypatch):
    stub = _Stub(balance=10_000.0, daily_open=None)
    _run(stub, monkeypatch)
    assert len(stub.rows) == 1
    assert stub.rows[0][1][2] == pytest.approx(0.0), "nothing to compare against yet"


def test_backfill_does_not_format_a_None_carry_value(monkeypatch):
    """A restart that also spans midnight: _daily_open_balance is None AND there are gap
    days to fill. f"${None:,.2f}" is its own TypeError."""
    import datetime as _dt
    stub = _Stub(balance=10_000.0, daily_open=None,
                 sheet_log_date=_dt.date.today() - _dt.timedelta(days=3))
    _run(stub, monkeypatch)
    assert len(stub.rows) >= 2, "the gap days were not backfilled"
    for _tab, row in stub.rows:
        assert isinstance(row[1], float) and not math.isnan(row[1]), row

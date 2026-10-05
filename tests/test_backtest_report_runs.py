"""The report path must execute. It is the last thing a backtest does and the only part
that produces output.

A data-coverage tally was added as a LOCAL inside the instrumentation setup while report()
— a separate function — read it as a global. Every run then died with

    NameError: name 'COVERAGE' is not defined

at the report stage, AFTER the full simulation had already run. Fifteen sweep runs
completed their work and threw all of it away at the last line. Nothing in the suite
touched the report path, so the break was invisible until the compute was already spent.

This is a smoke test, not a correctness test: it asserts the thing RUNS and that every
module-level tally it reads actually exists at module level.
"""
import ast
import datetime as dt
import pathlib

import pytest

import backtest_stocks as B

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _round_trip(**over):
    base = dict(symbol="AAPL", side="LONG", entry=100.0, exit=102.0, qty=10,
                pnl=20.0, stop=99.0,
                entry_dt=dt.datetime(2026, 8, 10, 14, 0),
                exit_dt=dt.datetime(2026, 8, 10, 18, 0))
    base.update(over)
    return base


def test_report_runs_on_a_normal_trade_list(capsys):
    B.report([_round_trip(), _round_trip(symbol="NVDA", pnl=-15.0, exit=98.5)],
             dt.datetime(2026, 8, 5), dt.datetime(2026, 10, 4), ["AAPL", "NVDA"])
    out = capsys.readouterr().out
    assert "RESULTS" in out, "the report produced no results block"


def test_report_runs_with_no_trades(capsys):
    """A zero-trade run is a real outcome and must still report, not crash."""
    B.report([], dt.datetime(2026, 8, 5), dt.datetime(2026, 10, 4), ["AAPL"])
    capsys.readouterr()


def test_report_runs_when_a_trade_has_no_stop(capsys):
    """R-multiples are skipped for these; they must not take the whole report down."""
    B.report([_round_trip(stop=None)], dt.datetime(2026, 8, 5),
             dt.datetime(2026, 10, 4), ["AAPL"])
    capsys.readouterr()


def test_coverage_tally_is_module_level():
    """The specific defect: report() reads it, so a local in another function is a
    guaranteed NameError that only shows up after a full simulation."""
    assert hasattr(B, "COVERAGE"), "COVERAGE must be module-level — report() reads it"
    assert hasattr(B, "GATES")
    assert hasattr(B, "FILLS")


# A static "does every name in report() resolve?" check was attempted here and removed.
# It flagged ten false positives — function-local imports (ZoneInfo, EOD_FLATTEN_MIN),
# nested-function parameters (keyfn, order, title) and `except ... as _e` bindings — because
# a correct version has to model nested scopes, imports and exception handlers. A test that
# is wrong about its own premise is worse than no test: it gets silenced, and then it is
# silenced when it is right.
#
# The three tests above are the better guard anyway. They EXECUTE the report path, which is
# exactly what nothing did when COVERAGE went in as a local and every run died at the last
# line after a full simulation.

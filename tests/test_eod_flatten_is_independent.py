"""The EOD flatten must not depend on the trading loop it is protecting against.

2026-10-05: the stock bot stalled 10:12 -> 13:01, roughly 34 missed 5-minute cycles, and
woke after the close. The flatten lived inside on_trading_iteration, so the 12:45 window
passed with no iteration in it and META carried overnight.

A safety net that fails whenever the thing it guards fails is not a safety net.

The GTC bracket legs left at the broker are NOT equivalent. A stop is a TRIGGER, not a fill:
a gap below it turns the stop into a market order that fills at the gap price. Flattening
before the close is what actually avoids overnight gap risk, which is the entire reason the
operator set that rule.
"""
import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("eod_flatten", ROOT / "scripts" / "eod_flatten.py")
eod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eod)


class _Clock:
    def __init__(self, is_open, next_close=None, timestamp=None):
        self.is_open, self.next_close, self.timestamp = is_open, next_close, timestamp


def _clock(minutes):
    from datetime import datetime, timedelta
    now = datetime(2026, 10, 5, 15, 0, tzinfo=eod.ET)
    return _Clock(True, now + timedelta(minutes=minutes), now)


class TestTheWindow:
    def test_closed_market_returns_none(self):
        assert eod.minutes_to_close(_Clock(False)) is None

    def test_reports_minutes_while_open(self):
        assert eod.minutes_to_close(_clock(45)) == pytest.approx(45, abs=0.1)

    def test_missing_next_close_is_none_not_a_crash(self):
        assert eod.minutes_to_close(_Clock(True, None)) is None

    @pytest.mark.parametrize("mins,should_act", [
        (120, False), (16, False), (15, True), (5, True), (1, True),
    ])
    def test_acts_only_inside_the_window(self, mins, should_act):
        mtc = eod.minutes_to_close(_clock(mins))
        assert (mtc <= eod.FLATTEN_WINDOW_MIN) is should_act


class TestIndependence:
    def test_it_does_not_import_the_bot(self):
        """The whole point. Importing strategy/lumibot would couple it to the thing that
        stalled — and to lumibot's own startup, which is where the stall happened."""
        import ast
        # Parse the IMPORTS, not the prose. A substring check flagged the docstring, which
        # names DebbieLaSMC only to explain what went wrong — a test that cannot tell an
        # explanation from a dependency gets silenced, and then it is silenced when a real
        # import appears.
        tree = ast.parse((ROOT / "scripts" / "eod_flatten.py").read_text())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
        banned = {"lumibot", "tradingbot", "bot", "binance_bot", "test_bot"}
        assert not (mods & banned), (
            f"eod_flatten imports {mods & banned} — that couples the safety net to the "
            f"thing it protects against, including lumibot's startup, which is where the "
            f"stall happened")

    def test_it_cancels_orders_before_closing(self):
        """A held bracket leg reserves the shares, so a close submitted underneath one is
        rejected for insufficient quantity. Order matters."""
        src = (ROOT / "scripts" / "eod_flatten.py").read_text()
        assert src.index("cancel_orders") < src.index("close_position")

    def test_a_failed_close_is_reported_not_swallowed(self):
        src = (ROOT / "scripts" / "eod_flatten.py").read_text()
        assert "FAILED to close" in src and "carry overnight" in src


def test_the_launchd_job_exists_and_self_gates():
    """Scheduled every 5 minutes with the ET check in the SCRIPT, so it cannot drift with
    daylight saving the way a fixed wall-clock launchd time would."""
    plist = pathlib.Path.home() / "Library" / "LaunchAgents" / "com.debbiela.eod_flatten.plist"
    if not plist.exists():
        pytest.skip("launchd job not installed in this environment")
    text = plist.read_text()
    assert "StartInterval" in text and "eod_flatten.py" in text

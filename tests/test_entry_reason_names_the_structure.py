"""A ledger row must say WHICH structure the entry was taken on, not just the AMD phase.

binance_bot and bot/strategy each built the reason string themselves, identically and
incompletely:  f"{amd_phase or 'BOS'} {'LONG' if is_long else 'SHORT'}"

So "manipulation_down LONG" was logged whether the setup was a bullish FVG, an order
block, a breaker or an inverted FVG. Those are different trades with different failure
modes, and nothing in the journal or the sheet could tell them apart afterwards — which
makes "do breakers actually work?" unanswerable from months of recorded history.

state.amd_zone_type already held the answer; the status line prints it as zone=bullish_fvg
every cycle. It just never reached the ledger.
"""
import ast
import pathlib

import pytest

from bot.indicators import entry_reason
from sheets_logger import LEDGER_HEADER, build_ledger_row

ROOT = pathlib.Path(__file__).resolve().parents[1]


class TestEntryReason:
    def test_names_phase_structure_and_side(self):
        assert entry_reason("manipulation_down", "bullish_fvg", True) == \
            "manipulation_down | bullish_fvg | LONG"

    def test_short_side(self):
        assert entry_reason("distribution", "bearish_ob", False) == \
            "distribution | bearish_ob | SHORT"

    def test_missing_phase_falls_back_to_bos(self):
        assert entry_reason(None, "breaker", True).startswith("BOS | breaker |")

    def test_missing_structure_is_marked_not_dropped(self):
        """A blank must be visible as a blank, not silently absent — otherwise the field
        count changes and the string stops being splittable."""
        r = entry_reason("accumulation", None, True)
        assert r.count("|") == 2
        assert "—" in r

    def test_chase_is_flagged(self):
        assert entry_reason("breakout", "momentum", True, is_chase=True).startswith("CHASE |")

    def test_is_splittable_into_fixed_fields(self):
        """The journal and any later scorer cut on this, so the shape must be stable."""
        parts = entry_reason("manipulation_up", "ifvg_support", False).split(" | ")
        assert len(parts) == 3
        assert parts[-1] in ("LONG", "SHORT")


class TestBothBotsUseTheProducer:
    @pytest.mark.parametrize("rel", ["binance_bot.py", "bot/strategy.py"])
    def test_no_bot_hand_builds_the_reason_string(self, rel):
        src = (ROOT / rel).read_text()
        assert "indicators.entry_reason(" in src, f"{rel} must use the shared producer"
        # the old hand-rolled shape, in either bot's spelling
        for old in ("or 'BOS'} {'LONG' if is_long else 'SHORT'}",
                    "or 'BOS'} {'LONG' if is_long else 'SHORT'}\""):
            assert old not in src, f"{rel} still hand-builds the reason string"


class TestZoneTypeReachesTheLedger:
    def test_column_exists_and_is_appended(self):
        assert LEDGER_HEADER[-1] == "Zone Type"
        assert LEDGER_HEADER.index("Chart") == 14, "existing columns must not shift"

    def test_row_carries_it(self):
        row = build_ledger_row("t0", "t1", "SOL", "LONG", 100, 99, 102, 101.5,
                               1, 1, 1, 10, 1.5, "manipulation_down | bullish_fvg | LONG",
                               None, fees=0.1, exit_reason="TARGET",
                               zone_type="bullish_fvg")
        assert len(row) == len(LEDGER_HEADER)
        assert row[LEDGER_HEADER.index("Zone Type")] == "bullish_fvg"
        assert row[LEDGER_HEADER.index("Exit Reason")] == "TARGET"

    def test_absent_zone_type_is_blank_not_an_error(self):
        row = build_ledger_row("t0", "t1", "SOL", "LONG", 1, 1, 1, 1, 1, 1, 1, 10, 0, "r")
        assert row[LEDGER_HEADER.index("Zone Type")] == ""

    @pytest.mark.parametrize("rel", ["binance_bot.py", "bot/strategy.py"])
    def test_both_bots_populate_it(self, rel):
        src = (ROOT / rel).read_text()
        assert "zone_type=" in src, f"{rel} builds a ledger row without the structure"


def test_journal_exposes_both_reasons():
    """The data existed for exitReason and was never rendered; zoneType must not repeat
    that — a column written but never shown is the same as not having it."""
    srv = (ROOT / "chart_server.py").read_text()
    assert '"zoneType"' in srv, "chart_server must expose zoneType to the journal"
    tpl = (ROOT / "templates" / "journal.html").read_text()
    assert "t.zoneType" in tpl, "journal must display the structure"
    assert "t.exitReason" in tpl, "journal must display why the trade closed"

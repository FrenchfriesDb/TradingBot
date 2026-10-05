"""A demand zone is bought at its LOW, not halfway up it.

price_in_entry_zone answers "has price REACHED the zone" and returns True anywhere between
lo and hi. Nothing then asked WHERE in the zone the fill sat.

PLTR, 2026-10-05: zone 187.62-190.50, filled 189.50 — 65% up a demand zone. That is paying
a premium for a discount setup, and the operator read it straight off the journal: "it
always buys literally in the middle of a weak consolidating zone".

It compounds downstream. Risk is measured from the FILL, so a fill at the top of a demand
zone sits further from the structural invalidation, which widens the stop, which widens the
target by the same R multiple. One bad fill location inflates both legs.
"""
import pytest

from bot.indicators import zone_entry_edge_ok as ok

LO, HI = 187.62, 190.50          # the real PLTR zone


class TestTheFillLocation:
    def test_the_actual_pltr_fill_is_refused(self):
        assert not ok(189.50, LO, HI, True, 0.5), "65% up a demand zone is not a discount"

    def test_a_fill_at_the_low_is_allowed(self):
        assert ok(188.00, LO, HI, True, 0.5)

    def test_overshooting_below_a_demand_zone_is_allowed(self):
        """Deeper discount is the fill IMPROVING. Only the premium side is refused."""
        assert ok(187.00, LO, HI, True, 0.5)

    def test_short_sells_the_high_of_supply(self):
        assert ok(190.00, LO, HI, False, 0.5)

    def test_short_halfway_down_is_refused(self):
        assert not ok(188.50, LO, HI, False, 0.5)

    def test_overshooting_above_a_supply_zone_is_allowed(self):
        assert ok(191.50, LO, HI, False, 0.5)

    def test_the_exact_midpoint_is_the_boundary(self):
        mid = (LO + HI) / 2
        assert ok(mid, LO, HI, True, 0.5) and ok(mid, LO, HI, False, 0.5)

    @pytest.mark.parametrize("frac,expect", [(0.33, False), (0.5, False), (0.9, True)])
    def test_the_fraction_is_what_tightens_it(self, frac, expect):
        assert ok(189.50, LO, HI, True, frac) is expect


class TestItCannotBreak:
    @pytest.mark.parametrize("args", [
        (None, LO, HI, True), (189.0, None, HI, True), (189.0, LO, None, True),
        (189.0, HI, LO, True), (189.0, LO, LO, True), ("x", LO, HI, True),
    ])
    def test_bad_input_refuses_rather_than_raising(self, args):
        """It runs inside the entry path. Refusing is safe; raising is not."""
        assert ok(*args, 0.5) is False


class TestEveryTapSiteIsGated:
    """The 10s watcher wins nearly every race against the 5m cycle, so a gate living only
    in the 5m path is dead code — exactly how the candle veto ended up unenforced."""

    @pytest.mark.parametrize("rel,n", [
        ("binance_bot.py", 2),        # 5m cycle + 10s watcher
        ("bot/strategy.py", 1),
        ("backtest_crypto.py", 1),    # the replay must mirror live
    ])
    def test_all_tap_sites_check_the_edge(self, rel, n):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1] / rel).read_text()
        got = src.count("zone_entry_edge_ok(")
        assert got == n, f"{rel}: expected {n} edge checks, found {got}"

    @pytest.mark.parametrize("rel", ["binance_bot.py", "bot/strategy.py"])
    def test_both_bots_got_it_this_time(self, rel):
        """The AMD gate was fixed on crypto and left on stocks for a whole trading day.
        Both bots, same commit, or it is the same mistake again."""
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1] / rel).read_text()
        assert "ZONE_ENTRY_MAX_FRAC" in src

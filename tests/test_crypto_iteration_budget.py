"""Widening the symbol list must not stretch the blind spot in front of open stops.

binance_bot's SL/TP watcher only starts AFTER the analysis pass, so every open position is
unwatched for exactly as long as that pass runs. At 13 symbols that was ~8s of fetching. At
40 it is ~25s, and one AI call can add up to its 25s timeout on top. The bot had NO
iteration budget — the stock bot got one when it moved to a 5M loop, this one never did —
so widening the list would silently convert a 5m cadence into something longer.

Two invariants, both of which a naive budget gets wrong:
  * a symbol HOLDING A POSITION is never deferred (management is what must not be dropped)
  * the remainder ROTATES, so a budget that always cuts at the same count cannot starve the
    tail of the list forever
"""
import pytest

import binance_bot as bb

SYMS = [f"S{i}/USD" for i in range(10)]


class TestIterationOrder:
    def test_held_positions_come_first(self):
        order = bb.iteration_order(SYMS, ["S7/USD", "S3/USD"], cycle=0)
        assert set(order[:2]) == {"S3/USD", "S7/USD"}

    def test_every_symbol_appears_exactly_once(self):
        for cycle in range(25):
            order = bb.iteration_order(SYMS, ["S4/USD"], cycle)
            assert sorted(order) == sorted(SYMS), f"cycle {cycle} lost or duplicated a symbol"

    def test_the_remainder_rotates(self):
        a = bb.iteration_order(SYMS, [], cycle=0)
        b = bb.iteration_order(SYMS, [], cycle=1)
        assert a != b, "without rotation a fixed budget starves the tail forever"

    def test_rotation_brings_every_symbol_to_the_front(self):
        """The whole point: a budget cutting after k symbols must still cover the list."""
        seen, k = set(), 3
        for cycle in range(len(SYMS)):
            seen.update(bb.iteration_order(SYMS, [], cycle)[:k])
        assert seen == set(SYMS), f"never reached: {set(SYMS) - seen}"

    def test_no_positions_is_a_plain_rotation(self):
        assert bb.iteration_order(SYMS, [], 0) == SYMS

    def test_empty_symbol_list_does_not_blow_up(self):
        assert bb.iteration_order([], [], 7) == []

    def test_all_held_is_stable(self):
        assert sorted(bb.iteration_order(SYMS, SYMS, 5)) == sorted(SYMS)


class TestBudgetIsSane:
    def test_budget_leaves_room_for_the_watcher(self):
        assert bb.CRYPTO_ITERATION_BUDGET_SEC < bb.SLEEP_SECONDS, (
            "a budget >= the cycle length cannot bound anything")
        assert bb.CRYPTO_ITERATION_BUDGET_SEC <= bb.SLEEP_SECONDS / 2, (
            "the SL/TP watcher needs the majority of the cycle")

    def test_the_loop_actually_checks_the_budget(self):
        import pathlib
        src = (pathlib.Path(bb.__file__).parent / "binance_bot.py").read_text()
        assert "CRYPTO_ITERATION_BUDGET_SEC" in src.split("def iteration_order")[-1], (
            "the budget constant is defined but never enforced in the loop")


class TestSymbolList:
    def test_widened_to_forty(self):
        assert len(bb.DEFAULT_SYMBOLS) == 40

    def test_no_duplicates(self):
        assert len(set(bb.DEFAULT_SYMBOLS)) == len(bb.DEFAULT_SYMBOLS)

    def test_no_stablecoin_pairs(self):
        """A USD-pegged pair does not move, so it adds load and contributes no setups."""
        stable = {"USDT", "USDC", "DAI", "PYUSD", "EURC", "GUSD", "USDP", "RLUSD"}
        bad = [s for s in bb.DEFAULT_SYMBOLS if s.split("/")[0] in stable]
        assert not bad, f"stablecoin pairs cannot produce setups: {bad}"

    def test_the_original_thirteen_are_kept_and_lead(self):
        """Swapping the population wholesale would make every pre-2026-10-04 crypto
        measurement incomparable with everything after it."""
        original = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "XRP/USD", "AVAX/USD",
                    "POL/USD", "ADA/USD", "HYPE/USD", "INJ/USD", "SEI/USD", "DRIFT/USD",
                    "ASTER/USD"]
        assert bb.DEFAULT_SYMBOLS[:13] == original

    def test_all_quoted_in_usd(self):
        assert all(s.endswith("/USD") for s in bb.DEFAULT_SYMBOLS)

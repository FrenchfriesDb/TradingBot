"""The target must aim AT the nearest liquidity pool, not past it.

The TP search was seeded at `entry + MIN_AI_RR*risk`, so find_next_liquidity_target could
only return a pool BEYOND 2R; anything nearer was invisible, and structural_take_profit
floored the target at 2R anyway. Whenever the first draw on liquidity sat inside 2R — on
6H crypto that is the normal case, 93% of live trades landed on exactly 1:2 — the bot
aimed past the exact level its entry thesis said price was going to go take.

Measured (60d, 13 symbols, routes A+C): median target 2.23R vs median best travel 0.59R,
and only 15% of setups ever reached 2R.
"""
import ast
import pathlib

import pytest

from bot import indicators

ROOT = pathlib.Path(__file__).resolve().parents[1]


class TestStructuralTakeProfit:
    def test_pool_inside_min_rr_is_used_when_pool_min_rr_allows_it(self):
        # entry 100, stop 99 -> R = 1. Pool at 101.4 = 1.4R, inside the old 2R floor.
        tp = indicators.structural_take_profit(
            100.0, 1.0, 101.4, True, min_rr=2.0, max_rr=15.0, pool_min_rr=1.0)
        assert tp == pytest.approx(101.4), "target must sit ON the nearest pool"

    def test_legacy_behaviour_is_unchanged_when_pool_min_rr_is_omitted(self):
        """Back-compatibility: callers that do not opt in must behave exactly as before."""
        tp = indicators.structural_take_profit(
            100.0, 1.0, 101.4, True, min_rr=2.0, max_rr=15.0)
        assert tp == pytest.approx(102.0), "omitting pool_min_rr must keep the 2R floor"

    def test_pool_nearer_than_the_noise_floor_falls_back_to_min_rr(self):
        # Pool at 100.3 = 0.3R: inside the noise, and fees alone would eat it.
        tp = indicators.structural_take_profit(
            100.0, 1.0, 100.3, True, min_rr=2.0, max_rr=15.0, pool_min_rr=1.0)
        assert tp == pytest.approx(102.0)

    def test_short_side_aims_at_the_nearest_pool_below(self):
        tp = indicators.structural_take_profit(
            100.0, 1.0, 98.6, False, min_rr=2.0, max_rr=15.0, pool_min_rr=1.0)
        assert tp == pytest.approx(98.6)

    def test_far_pool_is_still_capped_at_max_rr(self):
        tp = indicators.structural_take_profit(
            100.0, 1.0, 400.0, True, min_rr=2.0, max_rr=15.0, pool_min_rr=1.0)
        assert tp == pytest.approx(115.0)

    def test_absent_pool_still_defaults_to_min_rr(self):
        tp = indicators.structural_take_profit(
            100.0, 1.0, None, True, min_rr=2.0, max_rr=15.0, pool_min_rr=1.0)
        assert tp == pytest.approx(102.0)


class TestGatesAgreeWithTheTarget:
    """Every R:R GATE must use MIN_TRADE_RR, never MIN_AI_RR.

    MIN_AI_RR now only sets the fallback target when no pool exists. If any gate still
    compares against it, a nearest-pool target inside 2R passes target selection and then
    gets vetoed downstream — the change silently becomes a no-op on that path. This is the
    repo's most repeated defect: a rule wired into some paths and not others. One gate
    (binance_bot.py:3341, the R:R-survives-the-fill check) was in fact missed on the first
    pass and only turned up by enumerating them.
    """

    @pytest.mark.parametrize("rel", ["binance_bot.py", "backtest_crypto.py"])
    def test_no_rr_gate_compares_against_min_ai_rr(self, rel):
        tree = ast.parse((ROOT / rel).read_text())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for side in [node.left, *node.comparators]:
                if isinstance(side, ast.Name) and side.id == "MIN_AI_RR":
                    offenders.append(f"{rel}:{node.lineno} — {ast.unparse(node)}")
        assert not offenders, (
            "R:R gates must use MIN_TRADE_RR, not MIN_AI_RR:\n  " + "\n  ".join(offenders))

    @pytest.mark.parametrize("rel", ["binance_bot.py", "backtest_crypto.py"])
    def test_reachability_gate_is_given_the_trade_floor(self, rel):
        tree = ast.parse((ROOT / rel).read_text())
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", getattr(n.func, "id", None)) == "reachable_target"]
        assert calls, f"{rel}: no reachable_target call found — did it get renamed?"
        for node in calls:
            last = ast.unparse(node.args[-1])
            assert last == "MIN_TRADE_RR", (
                f"{rel}:{node.lineno} — reachable_target's min_rr is `{last}`, "
                f"must be MIN_TRADE_RR")

    def test_pool_search_is_not_seeded_from_min_ai_rr(self):
        """Seeding the search at the 2R floor is what made nearer pools invisible."""
        for rel in ("binance_bot.py", "backtest_crypto.py"):
            tree = ast.parse((ROOT / rel).read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = getattr(node.func, "attr", getattr(node.func, "id", None))
                if fn != "find_next_liquidity_target" or len(node.args) < 2:
                    continue
                seed = ast.unparse(node.args[1])
                assert "MIN_AI_RR" not in seed, (
                    f"{rel}:{node.lineno} — pool search seeded from MIN_AI_RR (`{seed}`), "
                    f"which hides every pool nearer than 2R")

    def test_trade_floor_is_not_looser_than_the_pool_floor(self):
        import binance_bot
        assert binance_bot.MIN_TRADE_RR <= binance_bot.MIN_AI_RR
        if binance_bot.TARGET_NEAREST_POOL:
            assert binance_bot.MIN_TRADE_RR == binance_bot.POOL_MIN_RR, (
                "a pool target at POOL_MIN_RR must be able to clear the trade gate")
        assert binance_bot.POOL_MIN_RR >= 1.0, (
            "never risk more than the target can pay back")

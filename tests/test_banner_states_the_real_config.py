"""The startup banner must be derived from the config, never hand-written.

tradingbot.py printed three hardcoded literals and all three were wrong:
  "Execution Interval: 15 minutes"  — the loop moved to 5M in f838799
  "3% total"                        — the live strategy is constructed with 0.02
  "~0.5% per symbol"                — 0.02/14 is 0.14%

This is the first thing anyone reads when asking "did the change go live?", and a Monday
review had already been scheduled to verify the 5M cadence from exactly this log. The
banner would have answered "15 minutes" on the session built to confirm it was 5M.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "tradingbot.py").read_text()


def _printed_literals():
    """Every static string that reaches a print(), via AST — NOT raw source.

    Checking raw text was wrong: this test's own explanatory comment quotes the literals
    it forbids, so it failed on itself. Comments describing a bug must not count as the bug.
    """
    out = []
    for node in ast.walk(ast.parse(SRC)):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print"):
            continue
        for arg in node.args:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    out.append(sub.value)
    return out


def test_banner_does_not_hardcode_an_interval():
    printed = " ".join(_printed_literals())
    for lie in ("Execution Interval: 15 minutes", "LTF: 15m", "Risk per trade: 3% total"):
        assert lie not in printed, f"banner prints hardcoded {lie!r} instead of the config"


def test_banner_reads_the_loop_interval_from_strategy():
    assert "LOOP_INTERVAL" in SRC, "the interval must come from bot.strategy.LOOP_INTERVAL"


def test_strategy_and_banner_share_one_parameters_dict():
    """They drifted because the banner described one dict and the strategy was built from
    another. One source, so they cannot disagree again."""
    tree = ast.parse(SRC)
    built = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "DebbieLaSMC"]
    assert built, "DebbieLaSMC construction not found — did it get renamed?"
    for call in built:
        kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
        params = kw.get("parameters", "")
        assert params and not params.startswith("{"), (
            "DebbieLaSMC must be built from the same dict the banner printed, "
            f"not an inline literal: {params[:60]}")


def test_the_live_interval_is_actually_five_minutes():
    """Guards the change itself, not just its description."""
    from bot.strategy import LOOP_INTERVAL
    assert LOOP_INTERVAL.upper() == "5M", (
        f"LOOP_INTERVAL is {LOOP_INTERVAL!r}; the chase fix (133d4c7) depends on checking "
        f"live price 3x more often than the old 15M loop")

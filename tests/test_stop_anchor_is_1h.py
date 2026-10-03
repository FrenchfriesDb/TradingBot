"""The structural stop must be floored against the 1H ATR everywhere — including the
backtest.

binance_bot.py:2594 and :2863 and bot/strategy.py:1391 all pass a 1H ATR to
structural_stop_price. backtest_crypto.py passed the **6H** ATR until 2026-10-03, which
made every replayed stop ~2.4x wider than live, every 2R target ~2.4x further away, and
left the 18h stale timer as the only exit the replay could reach. Sixty days of
"0 take-profits, 73% died on the timer" was measuring that bug, not the strategy.

This is the repo's recurring defect shape: a rule wired into some paths and not others.
Asserting it at the AST level is the only way a call site that silently uses the wrong
anchor fails a test instead of quietly rewriting the conclusions.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FILES = ["binance_bot.py", "bot/strategy.py", "backtest_crypto.py"]


def _calls_to(tree, fname):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name == fname:
            yield node


def _sites():
    for rel in FILES:
        path = ROOT / rel
        tree = ast.parse(path.read_text())
        for node in _calls_to(tree, "structural_stop_price"):
            yield rel, node


def test_every_structural_stop_is_floored_against_the_1h_atr():
    sites = list(_sites())
    # Guard the guard: if the helper is renamed, this test must fail loudly rather than
    # pass by finding nothing to check.
    assert len(sites) >= 4, f"expected >=4 call sites, found {len(sites)} — did it get renamed?"

    offenders = []
    for rel, node in sites:
        if len(node.args) < 3:
            offenders.append(f"{rel}:{node.lineno} — fewer than 3 positional args")
            continue
        anchor = ast.unparse(node.args[2])
        if "1h" not in anchor.lower():
            offenders.append(f"{rel}:{node.lineno} — anchor is `{anchor}`, not a 1H ATR")

    assert not offenders, (
        "structural_stop_price must be floored against the 1H ATR:\n  "
        + "\n  ".join(offenders))


@pytest.mark.parametrize("rel", ["binance_bot.py", "backtest_crypto.py"])
def test_anything_that_places_a_target_also_checks_reachability(rel):
    """A target chosen from structure must be clamped to what the hold window delivers.

    backtest_crypto.py picked targets with structural_take_profit but never called
    reachable_target, so it opened trades live REFUSES and then scored them timing out.
    """
    src = (ROOT / rel).read_text()
    if "structural_take_profit" not in src:
        pytest.skip(f"{rel} does not place structural targets")
    assert "reachable_target" in src, (
        f"{rel} places a structural target but never calls reachable_target — it will "
        f"score unreachable targets that live would skip")

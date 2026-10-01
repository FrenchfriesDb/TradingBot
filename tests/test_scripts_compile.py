"""Every executable script must at least PARSE.

WHY (2026-10-01). scripts/bot_watchdog.py was shipped with a SyntaxError — an edit left an
`if/else` with two `else:` branches. The whole suite stayed green because nothing imports
these scripts: they are launchd entry points, exercised only by launchd. The watchdog
would simply have stopped running, which is the one failure a watchdog cannot report.

Parsing is a floor, not a substitute for behaviour tests (the logic lives in bot/watchdog.py
and is tested there). It just means a launchd entry point can never again be broken in a
way the suite cannot see.
"""
import ast
import glob
import io

import pytest

SCRIPTS = sorted(glob.glob("scripts/*.py"))


def test_there_are_scripts_to_check():
    assert SCRIPTS, "no scripts/*.py found — did the layout move?"


@pytest.mark.parametrize("path", SCRIPTS)
def test_script_parses(path):
    src = io.open(path, encoding="utf-8").read()
    try:
        ast.parse(src, filename=path)
    except SyntaxError as e:
        pytest.fail(f"{path}:{e.lineno} {e.msg}")


@pytest.mark.parametrize("path", SCRIPTS)
def test_script_has_no_duplicate_else_in_one_block(path):
    """The exact shape of the bug: a patch inserted a branch and orphaned the original."""
    tree = ast.parse(io.open(path, encoding="utf-8").read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            assert len(node.orelse) < 2 or not all(
                isinstance(n, ast.If) for n in node.orelse), path

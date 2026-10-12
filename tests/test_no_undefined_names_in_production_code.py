"""An undefined name must not be able to reach a live bot.

`save_state` was called in two places in binance_bot.py and defined nowhere. One was an old
dashboard path that failed quietly inside a try/except; the other was copied from it into the
sleep catch-up, where it crashed all 28 live runs. py_compile accepts an undefined name, and so
did 1,300 tests, because nothing executed either line. pyflakes finds it in under a second, and
a scan of the whole codebase turned up exactly those two and nothing else — so the bar is zero.
"""
import glob
import io
import pathlib

import pytest

pyflakes_api = pytest.importorskip("pyflakes.api")
pyflakes_reporter = pytest.importorskip("pyflakes.reporter")

ROOT = pathlib.Path(__file__).resolve().parents[1]
FILES = sorted({str(p.relative_to(ROOT)) for pat in
                ("*.py", "bot/*.py", "scripts/*.py", "tools/*.py") for p in ROOT.glob(pat)})


@pytest.mark.parametrize("rel", FILES)
def test_no_undefined_names(rel):
    out, err = io.StringIO(), io.StringIO()
    pyflakes_api.check((ROOT / rel).read_text(), rel, pyflakes_reporter.Reporter(out, err))
    bad = [l for l in out.getvalue().splitlines() if "undefined name" in l]
    assert not bad, "undefined name(s) — this raises NameError at runtime, in a live bot:\n  " + "\n  ".join(bad)


def test_the_scan_covers_the_bots():
    """Guard against the glob silently matching nothing."""
    for must in ("binance_bot.py", "bot/strategy.py", "chart_server.py", "test_bot.py"):
        assert must in FILES

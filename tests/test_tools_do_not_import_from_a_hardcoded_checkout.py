"""Tools must measure the code that sits NEXT TO them, not a copy at a hardcoded path.

Seventeen tools in tools/ began with sys.path.insert(0, "/Users/<me>/Desktop/TradingBot").
After the project moved to ~/dev/TradingBot (to escape iCloud) the Desktop copy stayed
behind, 31 commits stale, so every one of those tools quietly imported OLD strategy code:
a measurement of last week's bot reported as this week's. Nothing failed, which is the
worst way for it to be wrong. A move would have turned that into an ImportError instead.
"""
import pathlib
import re

import pytest

TOOLS = sorted((pathlib.Path(__file__).resolve().parents[1] / "tools").glob("*.py"))
HARDCODED_CHECKOUT = re.compile(r"/Users/[^/\"']+/(?:Desktop|dev|Documents)/TradingBot")


@pytest.mark.parametrize("path", TOOLS, ids=lambda p: p.name)
def test_no_tool_hardcodes_a_checkout_path(path):
    hits = HARDCODED_CHECKOUT.findall(path.read_text())
    assert not hits, f"{path.name} pins an absolute checkout path {hits[0]!r}; derive it from __file__"


def test_there_are_tools_to_check():
    assert len(TOOLS) >= 20, "tools/ glob found almost nothing — the test above would pass vacuously"

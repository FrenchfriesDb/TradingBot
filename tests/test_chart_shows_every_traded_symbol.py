"""The chart must offer every symbol the bot can trade.

chart_server hardcoded its symbol list with the comment "smc mirrors binance_bot.py
DEFAULT_SYMBOLS (13 as of 2026-08-20)". When the bot widened 13 -> 40, the chart kept the
old 13. The operator then had live positions in AERO and TAO — real trades, with stops and
targets, moving the balance — that could not be selected or drawn anywhere on the dashboard.

A dashboard that cannot display a live position is worse than no dashboard, because an
absent position reads as "no position". The same screen had already shown a phantom ETH
short that no longer existed; between them the two failures are "shows what isn't there"
and "hides what is".

The lists are now substituted from binance_bot.DEFAULT_SYMBOLS at serve time.
"""
import json
import re

import chart_server as C
from binance_bot import DEFAULT_SYMBOLS


def _page():
    return C._render_page()


def test_no_markers_survive_rendering():
    page = _page()
    for marker in ("__PAIRS__", "__SMC_SYMBOLS__"):
        assert marker not in page, f"{marker} was never substituted — the page ships broken JS"


def test_every_tradeable_symbol_is_selectable():
    page = _page()
    missing = [s for s in DEFAULT_SYMBOLS if s not in page]
    assert not missing, f"bot trades these but the chart cannot show them: {missing}"


def test_the_smc_list_matches_the_bot_exactly():
    """Not just 'contains' — a stale EXTRA symbol is its own bug (a dropdown entry that
    fetches a product the bot never trades)."""
    m = re.search(r"smc:\s*(\[[^\]]*\])", _page())
    assert m, "smc symbol list not found in the rendered page"
    assert json.loads(m.group(1)) == list(DEFAULT_SYMBOLS)


def test_pairs_map_covers_every_symbol():
    """Each symbol needs a Coinbase product id or its candles silently fail to load."""
    m = re.search(r"const PAIRS = (\{.*?\});", _page(), re.S)
    assert m, "PAIRS map not found"
    pairs = json.loads(m.group(1))
    missing = [s for s in DEFAULT_SYMBOLS if s not in pairs]
    assert not missing, f"no Coinbase product id for: {missing}"
    assert pairs["BTC/USD"] == "BTC-USD"


def test_render_degrades_instead_of_raising(monkeypatch):
    """The page is served from an HTTP handler. An import problem must not 500 the
    dashboard — a reduced chart beats a dead one."""
    import builtins
    real = builtins.__import__

    def boom(name, *a, **k):
        if name == "binance_bot":
            raise RuntimeError("simulated import failure")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    page = C._render_page()
    assert "__PAIRS__" not in page and "BTC/USD" in page


def test_test_bot_list_is_deliberately_separate():
    """test_bot really does trade only 8 coins. Widening it to match the SMC bot would put
    symbols in its dropdown that it never trades."""
    # single-quoted JS, not JSON — this list is still a literal on purpose, so count the
    # symbols rather than parsing it as JSON (the first version of this test did, and
    # failed on the quoting rather than on anything real).
    m = re.search(r"test:\s*\[([^\]]*)\]", _page())
    assert m, "test_bot symbol list not found"
    assert len(re.findall(r"/USD", m.group(1))) == 8

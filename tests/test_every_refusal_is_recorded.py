"""Every rule that REFUSES a setup must leave evidence in the shadow ledger.

The shadow ledger exists to answer "is this rule too strict?" from the live tape instead of
from a backtest. It was wired into the retest rule and nothing else — so the single most
active filter on the stock bot recorded nothing at all. Across 2026-09-29..10-02 the
displacement gate rejected 28 of 28 taps that had already cleared the chase guard, and left
no trace of what any of them would have done.

Same shape as the gates, the stop fills, the fee legs, the HTF frame and the banners: the
rule is right where it is defined and absent at the other call sites.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "bot" / "strategy.py").read_text()

#: Refusal log messages that end the entry path. Each must record a shadow row, so the
#: question "would these have worked?" is answerable later from live data.
REFUSAL_MARKERS = [
    "No fresh displacement at tap",
    "not a return to it",
]


def test_the_shadow_ledger_has_more_than_one_producer():
    n = SRC.count("record_refusal(")
    assert n >= 2, (
        f"only {n} refusal is recorded; the ledger was built to compare REFUSED setups "
        f"against taken ones, and one producer cannot do that")


def test_distinct_reasons_are_tagged():
    """Rows must be separable by rule, or the sample is an undifferentiated pile."""
    tree = ast.parse(SRC)
    reasons = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "record_refusal"):
            continue
        for kw in node.keywords:
            if kw.arg == "reason" and isinstance(kw.value, ast.Constant):
                reasons.add(kw.value.value)
    assert "no_tap_displacement" in reasons, (
        f"the displacement gate must tag its own reason; found {reasons or 'none'}")


@pytest.mark.parametrize("marker", REFUSAL_MARKERS)
def test_each_refusal_path_records_a_shadow_row(marker):
    """Not tied to `return`: one refusal path returns, the other sets in_fvg = False and
    falls through. The invariant is that a row gets written near the refusal, not how the
    path happens to end — the first version of this test assumed `return` and failed on a
    site that was already correct."""
    i = SRC.find(marker)
    assert i != -1, f"refusal message {marker!r} not found — did the wording change?"
    assert "record_refusal" in SRC[i:i + 1200], (
        f"the {marker!r} path refuses a setup without writing a shadow row")


def test_recording_cannot_raise_into_the_entry_path():
    """Observation must never be able to break trading. Both sites are wrapped."""
    for i in [m for m in range(len(SRC)) if SRC.startswith("record_refusal(", m)]:
        window = SRC[max(0, i - 400):i]
        assert "try:" in window, (
            "record_refusal must sit inside a try/except — it observes the entry path "
            "and must not be able to raise into it")

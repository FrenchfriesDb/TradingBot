"""Every armed zone must say which chart it came from — not just the three FVG ones.

The operator asked for the timeframe on the chart labels, and 7c4e091 added it. But it
added it to three arming branches out of nine, and the three it covered were the ones
that fire least. Checked against the live bot straight after a restart, all four armed
symbols came back blank:

    BTC/USD    zone=choch_fvg        fvg_tf=None
    ETH/USD    zone=bullish_breaker  fvg_tf=None
    SOL/USD    zone=ifvg_support     fvg_tf=None
    POL/USD    zone=bullish_ob       fvg_tf=None

chart_server renders `sm.fvg_tf ? ' '+sm.fvg_tf : ''`, so a missing value is not a
visible error — the label just silently goes back to being the unlabelled one the
operator complained about.

Two distinct holes, both covered here:

  1. Five arming paths never set it (AMD supply/demand, the HTF displacement FVG, and
     both trend-zone fallbacks). They all read df_htf_closed, so they carry the HTF name.
     This matters most for "choch_fvg", which is armed from BOTH frames at two different
     sites — the label is the only thing distinguishing them on the chart.

  2. The field was saved to disk and never read back, so even a correctly-labelled zone
     lost its label on the next restart.

This test reads the source rather than driving the bot, because reaching nine arming
branches live needs nine different market shapes. The point is coverage: if someone adds
a tenth branch, it fails.
"""
import ast
import io
import re

import pytest

SRC = io.open("binance_bot.py", encoding="utf-8").read()
TREE = ast.parse(SRC)


def _zone_type_assignments():
    """Every `state.amd_zone_type = ...` in the file, with its line number."""
    out = []
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if (isinstance(tgt, ast.Attribute) and tgt.attr == "amd_zone_type"
                    and isinstance(tgt.value, ast.Name) and tgt.value.id == "state"):
                out.append(node.lineno)
    return sorted(out)


def _fvg_tf_lines():
    return {i + 1 for i, line in enumerate(SRC.splitlines())
            if re.match(r"\s*state\.fvg_tf\s*=", line)}


def test_there_are_still_arming_branches_to_check():
    """Guard the guard: if the parse breaks, the tests below pass vacuously."""
    assert len(_zone_type_assignments()) >= 8, _zone_type_assignments()


def test_every_arming_branch_also_names_its_timeframe():
    """Each `state.amd_zone_type = X` must have a `state.fvg_tf = Y` within a few lines.

    They are written as an adjacent pair everywhere, so proximity is the honest check —
    it catches a new branch that sets the zone type and forgets the frame."""
    tf_lines = _fvg_tf_lines()
    orphans = [ln for ln in _zone_type_assignments()
               if not any(abs(ln - t) <= 6 for t in tf_lines)]
    assert not orphans, (
        f"arming branches at lines {orphans} set amd_zone_type but no fvg_tf — "
        f"their zones render with a blank timeframe on the chart")


def test_the_two_choch_fvg_branches_are_labelled_with_different_frames():
    """The one that actually needs the label: choch_fvg is armed off df_ltf at one site
    and df_htf_closed at another. Identical zone type, different chart."""
    frames = set()
    for ln in _zone_type_assignments():
        line = SRC.splitlines()[ln - 1]
        if "choch_fvg" not in line:
            continue
        nearby = "\n".join(SRC.splitlines()[ln - 1: ln + 4])
        m = re.search(r"state\.fvg_tf\s*=\s*(\w+)", nearby)
        assert m, f"choch_fvg at line {ln} has no fvg_tf"
        frames.add(m.group(1))
    assert frames == {"LTF_TIMEFRAME", "HTF_TIMEFRAME"}, frames


def test_the_timeframe_survives_a_restart():
    """Saved-and-never-restored is how it came back blank on the live bot."""
    assert re.search(r'st\.fvg_tf\s*=\s*saved\.get\("fvg_tf"\)', SRC), \
        "fvg_tf is persisted but never read back — every restart drops the label"


def test_no_arming_branch_hardcodes_a_timeframe_string():
    """A hardcoded "4H" is exactly the bug this line of work started from: tf_tag said
    4H while the fetch pulled 6h. The right-hand side must be a name — either a
    TIMEFRAME constant, or a variable like fvg_bull_tf that zone_source_tf() derived
    from those same constants — never a literal."""
    for ln in sorted(_fvg_tf_lines()):
        rhs = SRC.splitlines()[ln - 1].split("=", 1)[1].split("#")[0].strip()
        assert not (rhs.startswith(("'", '"'))), \
            f"line {ln} hardcodes a timeframe literal: {rhs!r}"
        assert re.fullmatch(r"[A-Za-z_]\w*", rhs), \
            f"line {ln} is not a plain name: {rhs!r}"


def test_the_derived_timeframe_variables_come_from_the_constants():
    """fvg_bull_tf / fvg_bear_tf are computed, so follow them one hop to their source."""
    derived = {SRC.splitlines()[ln - 1].split("=", 1)[1].split("#")[0].strip()
               for ln in _fvg_tf_lines()}
    for name in derived - {"HTF_TIMEFRAME", "LTF_TIMEFRAME"}:
        m = re.search(rf"{name}\s*=\s*indicators\.zone_source_tf\((.*?)\)", SRC, re.S)
        assert m, f"{name} is assigned to fvg_tf but is not derived from zone_source_tf"
        assert "HTF_TIMEFRAME" in m.group(1) and "LTF_TIMEFRAME" in m.group(1), \
            f"{name} does not name both frames: {m.group(1)!r}"


def test_reset_clears_the_timeframe():
    """A stale label on a freshly armed zone is worse than none."""
    assert re.search(r"self\.fvg_tf\s*=\s*None", SRC), "reset()/__init__ must clear it"

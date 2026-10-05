"""The tap-time momentum floor is its own number, measured separately from formation.

WHY THIS EXISTS (2026-09-29). The stock bot checks displacement TWICE, for two different
questions:

  1. FORMATION — "was this zone built by a decisive move?" (displacement_gates, 1.8x)
  2. THE TAP    — "is the bar filling the zone right now decisive?" (has_displacement)

Both read DISPLACEMENT_ATR_MULT, so the tap inherited 1.8x by accident. 1.8x was chosen
for formation on the CRYPTO side (POL/USD armed off a 1.07x-ATR bar; see
displacement_min_body). Nobody had ever measured what the tap wants.

tools/measure_tap_displacement.py swept it over n=8289 taps (12 symbols, 15m bars,
session-capped holds, scored 2R-before-1R):

    threshold  taps  %kept  win@2R   exp@2R   total R
       0.0x    8289   100%    29%    -0.12R     -995R
       0.8x    1174    14%    35%    +0.04R      +47R
       1.0x     820    10%    38%    +0.14R     +115R   <- total-R peak
       1.4x     464     6%    38%    +0.13R      +60R
       1.8x     282     3%    39%    +0.17R      +48R   <- inherited value
       2.5x     157     2%    40%    +0.21R      +33R

Two findings, and they matter in opposite directions:

  • The 0.0x row is the first direct evidence that this gate EARNS its keep. The same
    taps with no momentum requirement run -0.12R. Every negative expectancy number
    measured on this bot before now was measured on the ungated population.
  • Per-trade expectancy keeps drifting up past 1.0x, but +0.14R vs +0.17R is inside the
    noise at n=820 vs n=282. The argument for 1.0x is frequency at indistinguishable
    quality: 2.9x the trades for the same edge. It is NOT a claim that 1.0x trades better.

The tap therefore gets its own constant. Collapsing it back into DISPLACEMENT_ATR_MULT
would loosen stock zone formation AND both of binance_bot's timeframes to buy this one
change — exactly the class of accident this repo keeps making (see
tests/test_every_armed_zone_names_its_timeframe.py).
"""
import ast
import io
import os

import pytest

from bot import indicators

SRC  = io.open("bot/strategy.py", encoding="utf-8").read()
TREE = ast.parse(SRC)
CRYPTO_SRC = io.open("binance_bot.py", encoding="utf-8").read()


def _assigned_default(tree, name):
    """The literal string passed to os.getenv for a module-level `name = float(getenv)`."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if name not in targets:
            continue
        for call in ast.walk(node.value):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "getenv" and len(call.args) == 2):
                return ast.literal_eval(call.args[1])
    raise AssertionError(f"{name} is not a module-level os.getenv assignment")


def test_tap_constant_exists_and_defaults_to_the_measured_peak():
    assert _assigned_default(TREE, "TAP_DISPLACEMENT_ATR_MULT") == "1.0"


def test_formation_multiple_is_untouched():
    """The 1.8x that protects zone FORMATION must not move with the tap."""
    assert _assigned_default(TREE, "DISPLACEMENT_ATR_MULT") == "1.8"


def test_both_constants_are_env_overridable():
    """Neither number is hardcoded — the sweep has to stay re-runnable without an edit."""
    for name in ("TAP_DISPLACEMENT_ATR_MULT", "DISPLACEMENT_ATR_MULT"):
        assert f'os.getenv("{name}"' in SRC, name


def _displacement_calls():
    return [n for n in ast.walk(TREE)
            if isinstance(n, ast.Call) and (
                (isinstance(n.func, ast.Attribute) and n.func.attr == "has_displacement")
                or (isinstance(n.func, ast.Name) and n.func.id == "has_displacement"))]


def _tap_call():
    """The TAP-time recheck, told apart from the FORMATION checks by its multiple.

    This used to assert there was exactly one has_displacement call in the file, with a
    message instructing whoever hit it to ENUMERATE rather than delete the assertion. A
    second call arrived on 2026-10-05 — AMD's distribution leg, which is a FORMATION check
    on the HTF and correctly uses DISPLACEMENT_ATR_MULT. Enumerating, as instructed.

    The invariant that matters is unchanged and is now stated directly: exactly one call
    uses the TAP multiple, and every other call uses the FORMATION multiple. Neither may
    borrow the other's threshold — 1.0x vs 1.8x is a 3x difference in the body a bar must
    print, and swapping them silently cuts qualifying taps to a third or lets formation
    arm on noise.
    """
    calls = _displacement_calls()
    tap = [c for c in calls
           if "TAP_DISPLACEMENT_ATR_MULT" in {n.id for n in ast.walk(c)
                                              if isinstance(n, ast.Name)}]
    assert len(tap) == 1, (
        f"expected exactly 1 TAP-multiple has_displacement call in bot/strategy.py, "
        f"found {len(tap)} of {len(calls)} total.")
    return tap[0]


def test_every_displacement_call_declares_which_multiple_it_is():
    """No call may be left on a bare default — that is how one silently becomes the other."""
    orphans = []
    for c in _displacement_calls():
        names = {n.id for n in ast.walk(c) if isinstance(n, ast.Name)}
        if not ({"TAP_DISPLACEMENT_ATR_MULT", "DISPLACEMENT_ATR_MULT"} & names):
            orphans.append(c.lineno)
    assert not orphans, (
        f"has_displacement calls with neither multiple named, at lines {orphans}")


def test_the_tap_uses_the_tap_multiple():
    """The name passed into the tap's size floor, read off the AST — not a grep."""
    names = {n.id for n in ast.walk(_tap_call()) if isinstance(n, ast.Name)}
    assert "TAP_DISPLACEMENT_ATR_MULT" in names
    assert "DISPLACEMENT_ATR_MULT" not in names, (
        "the tap is back on the formation multiple — that silently triples the size it "
        "demands of the filling bar and cuts qualifying taps to a third")


def test_formation_still_uses_the_formation_multiple():
    """displacement_gates is the arming path; it must NOT drift onto the tap number."""
    gate_calls = [n for n in ast.walk(TREE)
                  if isinstance(n, ast.Call) and (
                      (isinstance(n.func, ast.Attribute) and n.func.attr == "displacement_gates")
                      or (isinstance(n.func, ast.Name) and n.func.id == "displacement_gates"))]
    assert gate_calls, "no displacement_gates calls found — arming path moved?"
    for call in gate_calls:
        names = {n.id for n in ast.walk(call) if isinstance(n, ast.Name)}
        assert "DISPLACEMENT_ATR_MULT" in names
        assert "TAP_DISPLACEMENT_ATR_MULT" not in names, (
            "zone formation is now armed on the looser tap floor — the 1.0x number was "
            "measured for the FILLING bar only")


def test_each_bot_carries_its_own_measured_tap_value():
    """UPDATED 2026-10-02. This used to assert crypto had NO tap constant, which was right
    while its tap was unmeasured. It has since been measured (85k taps, 60d) and wired at
    1.4x — the best NET-expectancy row there — after SOL and DOGE both filled fresh zones
    on 0.14x and 0.36x ATR bars and stopped.

    The intent the original test protected still holds and is what is pinned now: the two
    bots are tuned SEPARATELY, from their own data. Stock is commission-free and peaks at
    1.0x; crypto pays 0.25%/side and needs 1.4x to clear the fee drag. One shared number
    would be wrong for both."""
    crypto_tree = ast.parse(CRYPTO_SRC)
    assert _assigned_default(crypto_tree, "TAP_DISPLACEMENT_ATR_MULT") == "1.4"
    assert _assigned_default(TREE, "TAP_DISPLACEMENT_ATR_MULT") == "1.0"
    # zone FORMATION stays at 1.8x in both — a different question from the tap.
    assert _assigned_default(crypto_tree, "DISPLACEMENT_ATR_MULT") == "1.8"
    assert _assigned_default(TREE, "DISPLACEMENT_ATR_MULT") == "1.8"


# ── What the number actually does to a bar ────────────────────────────────────────────
# A full-bodied bar 1.3x ATR is the whole population this change buys: it clears 1.0x and
# fails 1.8x. If displacement_min_body's arithmetic ever changes, these two fail together
# and say so.
ATR   = 1.0
PRICE = 101.3
BAR   = [[100.0, 101.3, 100.0, 101.3]]   # body 1.3 == its full range, bullish


def _passes(mult):
    return indicators.has_displacement(
        BAR, is_long=True, min_body_frac=0.5,
        min_body_abs=indicators.displacement_min_body(ATR, PRICE, mult, 0.0015))


def test_a_1_3x_atr_bar_is_a_tap_but_not_a_formation_displacement():
    assert _passes(1.0) is True,  "1.3x ATR must clear the 1.0x tap floor"
    assert _passes(1.8) is False, "1.3x ATR must NOT clear the 1.8x formation floor"


def test_the_percentage_backstop_still_binds_on_a_flat_tape():
    """1.0x of a collapsed ATR is invisible; min_pct is what stops that."""
    # ATR 0.001 on a $100 stock -> ATR term 0.001, pct term 0.15. The floor is the pct.
    floor = indicators.displacement_min_body(0.001, 100.0, 1.0, 0.0015)
    assert floor == pytest.approx(0.15)


def test_default_is_looser_than_formation_by_construction():
    """Guards the direction of the change, so a future edit can't invert it unnoticed."""
    tap  = float(_assigned_default(TREE, "TAP_DISPLACEMENT_ATR_MULT"))
    form = float(_assigned_default(TREE, "DISPLACEMENT_ATR_MULT"))
    assert tap < form
    assert tap >= 1.0, (
        "below 1.0x the floor asks for LESS than an average-sized candle, which is how "
        "POL/USD armed on a 1.07x bar under the old 0.6x — see displacement_min_body")

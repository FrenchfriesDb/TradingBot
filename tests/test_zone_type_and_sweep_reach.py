"""Why AVAX, SEI and ADA all went long into chop on 2026-09-17.

THE COMPLAINT, AND IT WAS RIGHT. Three positions opened within 17 minutes, all long,
all in a sideways drift, with — as the operator put it — "NO FVG or displacement
candle, there isn't even 3 red momentum candles that an FVG has... and there is no
CHoCH or anything". The bot's own persisted state agreed, identically for all three:

    bos_dir None | choch_dir None | disp_high None | disp_low None
    amd_phase None | amd_zone_type None | sniper_armed True

WHAT THE LOG SHOWED. The 5-minute cycle got it right and said so:

    [AVAX] STEP 2: Bullish OB/FVG locked $7.6060-$7.6180  sweep=$7.1700
    [AVAX] 🕯 Zone tap — candle: ⚠️ normal (no candle confirm) | ⚠️ no 15m CHoCH
    [AVAX] ⏳ In zone — waiting for rebounce
    [AVAX] ⏭ Sniper stood down — ... (zone=None, 1 bars armed)      × 11
    [AVAX] ⚡ SNIPER ENTRY — LONG @ $7.6050

Eleven refusals, then one ordinary green candle and it fired anyway.

THE THREE DEFECTS, which are one failure wearing three hats:

  1. binance_bot.py's two fallback arming branches call state.arm_zone() without ever
     setting amd_zone_type, so it stays None. Every OTHER arming branch sets one. That
     is the literal `zone=None` in the log.

  2. sniper_entry_allowed() consulted has_momentum ONLY on the stale branch. A FRESH
     zone whose type was None fell straight through to `candle_confirms or
     choch_aligned` — so a single confirming candle was the entire entry requirement,
     and the displacement gate was unreachable on the path taking most of the trades.

  3. Nothing checked whether the swept level was still anywhere near price. AVAX armed
     off a sweep 5.7% below the market, SEI 6.3%, ADA 5.8%. sweep_hunt_expired() caps
     how LONG a hunt may run; it says nothing about how FAR away the liquidity was.

Fixing (1) alone would not have helped, because (2) only requires displacement on a
stale zone. Fixing (2) alone would not have helped, because (1) means no zone type ever
arrives to reason about. They have to move together, which is why they are one file.
"""
import ast
import io
import pathlib

import pytest

from bot.indicators import sniper_entry_allowed, sweep_within_reach

REPO = pathlib.Path(__file__).resolve().parent.parent


# ── 2. the sniper gate: displacement is now required on fresh zones too ───────

def test_the_avax_entry_is_now_refused():
    """The exact live case: fresh generic zone, a candle that confirms, no displacement."""
    ok, why = sniper_entry_allowed(None, is_stale=False, has_momentum=False,
                                   candle_confirms=True, choch_aligned=False)
    assert ok is False, why


def test_a_typed_fallback_zone_with_displacement_and_a_confirming_candle_fires():
    ok, _ = sniper_entry_allowed("bullish_fvg", is_stale=False, has_momentum=True,
                                 candle_confirms=True, choch_aligned=False)
    assert ok is True


def test_displacement_alone_is_not_enough_on_a_fresh_zone():
    """Both halves still required: the move must be real AND the tap must confirm."""
    ok, _ = sniper_entry_allowed("bullish_fvg", is_stale=False, has_momentum=True,
                                 candle_confirms=False, choch_aligned=False)
    assert ok is False


def test_a_15m_choch_substitutes_for_the_candle_but_not_for_displacement():
    assert sniper_entry_allowed("bullish_fvg", False, True, False, True)[0] is True
    assert sniper_entry_allowed("bullish_fvg", False, False, False, True)[0] is False


def test_a_real_displacement_fvg_still_takes_a_direct_tap():
    """choch_fvg means the displacement IS the zone — re-demanding it would be circular,
    and this is the one path whose entries the operator has never objected to."""
    ok, _ = sniper_entry_allowed("choch_fvg", is_stale=False, has_momentum=False,
                                 candle_confirms=False, choch_aligned=False)
    assert ok is True


@pytest.mark.parametrize("zone", [None, "bullish_fvg", "bearish_ob", "choch_fvg", "ifvg"])
def test_a_stale_zone_always_needs_fresh_momentum_whatever_its_type(zone):
    """Unchanged behaviour, pinned so the rewrite cannot quietly loosen it."""
    assert sniper_entry_allowed(zone, True, True, False, False)[0] is True
    assert sniper_entry_allowed(zone, True, False, True, True)[0] is False


def test_the_refusal_reason_names_displacement_so_the_log_is_diagnosable():
    """"zone=None" cost hours of guessing. A refusal has to say which gate stopped it."""
    _, why = sniper_entry_allowed(None, False, False, True, False)
    assert "displacement" in why.lower()


# ── 3. a swept level has to still be near price to mean anything ──────────────

AVAX = dict(sweep_level=7.17, price=7.605)      # the live trade, 5.7% away


def test_the_avax_sweep_was_too_far_to_arm_on():
    ok, why = sweep_within_reach(atr=0.05, **AVAX)
    assert ok is False
    assert "5.7%" in why or "5.72%" in why, why


def test_a_nearby_sweep_is_fine():
    assert sweep_within_reach(sweep_level=7.55, price=7.605, atr=0.05)[0] is True


def test_the_limit_scales_with_volatility_rather_than_being_a_flat_percentage():
    """The same 5.7% is absurd on a quiet chart and ordinary on a violent one."""
    assert sweep_within_reach(atr=0.05, **AVAX)[0] is False   # quiet -> too far
    assert sweep_within_reach(atr=0.20, **AVAX)[0] is True    # violent -> in range


def test_a_dead_atr_falls_back_to_the_percentage_floor_rather_than_allowing_anything():
    """atr=0 must not collapse the limit to zero (refuse everything) or to infinity."""
    assert sweep_within_reach(sweep_level=7.60, price=7.605, atr=0.0)[0] is True
    assert sweep_within_reach(atr=0.0, **AVAX)[0] is False


def test_a_sweep_above_price_is_measured_the_same_way():
    """Short side: buy-side liquidity swept above, price since fallen away from it."""
    assert sweep_within_reach(sweep_level=8.10, price=7.605, atr=0.05)[0] is False
    assert sweep_within_reach(sweep_level=7.66, price=7.605, atr=0.05)[0] is True


@pytest.mark.parametrize("bad", [None, 0, 0.0])
def test_a_missing_sweep_is_not_a_reach_failure(bad):
    """No sweep at all is a different question, answered elsewhere. Do not report it
    as 'too far' — that would send the caller hunting for a level that never existed."""
    ok, why = sweep_within_reach(sweep_level=bad, price=7.605, atr=0.05)
    assert ok is True, why


def test_a_nonsense_price_fails_closed():
    assert sweep_within_reach(sweep_level=7.17, price=0, atr=0.05)[0] is False


# ── 1. every arming branch must declare what kind of zone it armed ────────────

def _module_of(body):
    return ast.Module(body=body, type_ignores=[])


def _arm_calls(body):
    return [n for n in ast.walk(_module_of(body))
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "arm_zone"]


def _sets_zone_type(body):
    return any(isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Attribute) and t.attr == "amd_zone_type"
                       for t in n.targets)
               for n in ast.walk(_module_of(body)))


def test_no_branch_arms_a_zone_without_naming_its_type():
    """The structural invariant the two fallback branches broke.

    Written as an invariant rather than two assertions about lines 2175 and 2182,
    because the next fallback branch somebody adds will forget in exactly the same way,
    and an untyped zone is invisible until it costs three trades.
    """
    tree = ast.parse((REPO / "binance_bot.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        for body in (node.body, node.orelse):
            calls = _arm_calls(body)
            if calls and not _sets_zone_type(body):
                offenders.append(min(c.lineno for c in calls))
    assert not offenders, (
        "binance_bot.py arms a zone without setting amd_zone_type at line(s) "
        + ", ".join(map(str, sorted(set(offenders))))
        + " — an untyped zone reaches sniper_entry_allowed() as None")


# ── the same two defects in the stock bot ─────────────────────────────────────
#
# bot/strategy.py has no sniper, so defect (2) cannot bite there — but it arms zones
# through the same shape of fallback branch and it had the same two holes:
#
#   * lines 1628/1640 set fvg_low/fvg_high and never amd_zone_type, so the generic
#     zone reached get_ai_confirmation(zone_type=None) — the AI was being asked to
#     grade a setup while being told nothing about what kind of zone it was;
#   * nothing checked how far the swept level sat from price.
#
# The first audit of this missed it entirely, because strategy.py assigns
# `self.amd_zone_type[symbol]` (a subscript) rather than `state.amd_zone_type` (an
# attribute) and never calls arm_zone(). Hence the two shapes below.

STOCK = REPO / "bot" / "strategy.py"


def _sets_subscript(body, attr):
    return any(isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Subscript)
                       and isinstance(t.value, ast.Attribute) and t.value.attr == attr
                       for t in n.targets)
               for n in ast.walk(_module_of(body)))


def test_the_stock_bot_never_arms_a_zone_without_naming_its_type():
    tree = ast.parse(STOCK.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        for body in (node.body, node.orelse):
            if _sets_subscript(body, "fvg_low") and not _sets_subscript(body, "amd_zone_type"):
                offenders.append(min(n.lineno for n in ast.walk(_module_of(body))
                                     if hasattr(n, "lineno")))
    assert not offenders, (
        "bot/strategy.py sets fvg_low without amd_zone_type at line(s) "
        + ", ".join(map(str, sorted(set(offenders))))
        + " — the zone reaches the AI as zone_type=None")


@pytest.mark.parametrize("script", ["binance_bot.py", "bot/strategy.py"])
def test_both_bots_check_that_a_swept_level_is_still_near_price(script):
    """sweep_hunt_expired() bounds the age of a hunt; only this bounds the distance.
    Both bots run the same SWEEP_HUNT -> ENTRY_WAIT shape and both need it."""
    src = (REPO / script).read_text(encoding="utf-8")
    assert "sweep_within_reach" in src, (
        f"{script} advances to ENTRY_WAIT without ever asking whether the swept level "
        f"is still anywhere near the market")

"""A "retest" is not a retest if price never left the zone.

TWO LIVE ENTRIES, 2026-09-24, both refused by eye on the chart:

    [ASTER] STEP 2: 🎯 CHoCH FVG (displacement) locked $0.7011-$0.7034 — waiting for retest/refill
    [ASTER]   ⚡ SNIPER ENTRY — LONG @ $0.7004        <- last candle: marubozu_bear

    [DOGE]  STEP 2: 🎯 CHoCH FVG (displacement) locked $0.0951-$0.0955 — waiting for retest/refill
    [DOGE]  🔫 Sniper armed ... 14:44 candle=shooting_star
    [DOGE]    ⚡ SNIPER ENTRY — LONG @ $0.0954        <- same cycle as the arm

"waiting for retest/refill" was a print statement and nothing else. NOTHING in the bot
tracked whether price ever left the zone. A choch_fvg zone IS the gap the displacement
just tore open, so price is typically still inside it at the moment it is armed — the
first "tap" is therefore the impulse itself, and the bot buys the top of the move.
DOGE armed and filled in the same cycle, on a shooting star.

WHY THE EXISTING GATES DID NOT CATCH IT. sniper_entry_allowed() exempts this one zone
type from everything:

    if zone_type == "choch_fvg":
        return True, "fresh displacement FVG — direct tap"

no momentum check, no candle check. The same log proves what that exemption costs — the
same symbol, the same sniper, twelve refusals in a row while the zone was bearish_fvg:

    [ASTER] ⏭ Sniper stood down — fresh zone with no displacement at the tap  (x12)

and then an instant fill once the zone type became choch_fvg.

The exemption's stated reasoning is that re-checking displacement would be "circular"
because the displacement IS the zone. That part is fair. But it does not license
skipping the retest itself: "the gap was made by a real impulse" and "price came back to
the gap" are different claims, and only the first was ever checked.
"""
import pytest

from bot.indicators import zone_left_since_arming


LO, HI = 0.0951, 0.0955          # DOGE's actual armed zone


# ── the DOGE case: armed while price is still inside the gap ─────────────────

def test_price_inside_the_zone_at_arming_has_not_left_it():
    assert zone_left_since_arming(0.0954, LO, HI, already_left=False) is False


def test_the_doge_fill_price_would_not_count_as_a_retest():
    """$0.0954 sits inside $0.0951-$0.0955. Nothing has been retested."""
    assert zone_left_since_arming(0.0954, LO, HI, already_left=False) is False


def test_leaving_upward_counts_as_leaving():
    assert zone_left_since_arming(0.0962, LO, HI, already_left=False) is True


def test_leaving_downward_counts_as_leaving():
    assert zone_left_since_arming(0.0940, LO, HI, already_left=False) is True


def test_once_left_it_stays_left_when_price_returns():
    """The whole point: leave, then come back — THAT is the retest."""
    left = zone_left_since_arming(0.0962, LO, HI, already_left=False)
    assert left is True
    assert zone_left_since_arming(0.0953, LO, HI, already_left=left) is True


def test_the_flag_is_sticky_and_never_flips_back():
    assert zone_left_since_arming(0.0953, LO, HI, already_left=True) is True


# ── the normal case must be unaffected ───────────────────────────────────────

def test_a_zone_armed_BELOW_price_is_already_a_pending_retest():
    """The ordinary setup — demand sits under price, price later drops into it. That was
    never the broken case and must not become slower to trade."""
    assert zone_left_since_arming(0.0980, LO, HI, already_left=False) is True


def test_a_zone_armed_ABOVE_price_is_likewise_ready():
    assert zone_left_since_arming(0.0900, LO, HI, already_left=False) is True


# ── edges ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("edge", [LO, HI])
def test_sitting_exactly_on_an_edge_is_not_outside(edge):
    assert zone_left_since_arming(edge, LO, HI, already_left=False) is False


@pytest.mark.parametrize("bad", [(None, HI), (LO, None), (None, None)])
def test_an_unreadable_zone_does_not_invent_a_retest(bad):
    assert zone_left_since_arming(0.09, bad[0], bad[1], already_left=False) is False


def test_an_unreadable_zone_does_not_erase_a_retest_already_earned():
    assert zone_left_since_arming(0.09, None, None, already_left=True) is True


@pytest.mark.parametrize("bad_price", [None, "x", float("nan")])
def test_an_unreadable_price_does_not_grant_a_retest(bad_price):
    assert zone_left_since_arming(bad_price, LO, HI, already_left=False) is False


def test_an_inverted_zone_is_refused_rather_than_guessed():
    assert zone_left_since_arming(0.0953, HI, LO, already_left=False) is False


# ── the second hole: the sniper never applied the opposing-candle veto ───────

from bot.indicators import sniper_entry_allowed


def test_a_decisive_opposing_bar_vetoes_even_a_choch_fvg():
    """ASTER filled LONG on a marubozu_bear. tap_candle_opposes_bias existed and was
    wired into the 5-MINUTE cycle only, while this watcher polls every 10 seconds and
    wins nearly every race — so the veto was dead code."""
    ok, why = sniper_entry_allowed("choch_fvg", False, True, True, False,
                                   opposing_candle=True)
    assert not ok and "against" in why.lower()


@pytest.mark.parametrize("zone", ["choch_fvg", "bullish_fvg", "wedge_retest", None])
def test_the_veto_applies_to_every_zone_type(zone):
    assert not sniper_entry_allowed(zone, False, True, True, True,
                                    opposing_candle=True)[0]


@pytest.mark.parametrize("stale", [True, False])
def test_the_veto_applies_stale_or_fresh(stale):
    assert not sniper_entry_allowed("choch_fvg", stale, True, True, True,
                                    opposing_candle=True)[0]


def test_choch_fvg_still_takes_its_direct_tap_when_nothing_opposes():
    """The exemption's premise is sound — re-demanding displacement WOULD be circular.
    This narrows it to 'no opposing bar', it does not delete it."""
    ok, why = sniper_entry_allowed("choch_fvg", False, False, False, False,
                                   opposing_candle=False)
    assert ok and "direct tap" in why


def test_the_default_is_no_veto_so_existing_callers_are_unchanged():
    assert sniper_entry_allowed("choch_fvg", False, False, False, False)[0] is True

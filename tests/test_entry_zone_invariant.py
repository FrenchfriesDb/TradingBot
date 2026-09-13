"""A retest entry must actually fill inside the zone it armed.

THE INCIDENT (ASTER/USD LONG, 2026-09-05 18:28 UTC). The AMD engine armed a demand zone
at 0.7190-0.7588 and the bot filled at 0.7906 -- 4.19% ABOVE the top of its own zone.
Price never traded down to the zone at all. The operator spotted it by eye: "the entry of
this trade needs to be fixed bruh".

    armed zone   0.7190 .. 0.7588
    OTE band     0.7812 .. 0.7863     (entry was above this too)
    filled at    0.7906
    stop         0.7751               -> below the CHOP, not below the zone

price_in_entry_zone(0.7906, 0.7190, 0.7588, is_long=True) is False, so the ordinary
retest path could not have produced that fill. Which path did is unknown -- the bot was
launched bare in a terminal, so its reasoning went to /dev/ttys003 and no file recorded
it. That is the third diagnosis this has blocked, hence tee_logging alongside this.

The guard here does not need to know which path was at fault. Every entry funnels through
execute_confirmed_entry, so asserting the invariant THERE catches it regardless of cause,
and logs loudly enough to identify the path next time.

WHY THE CHASE EXEMPTION IS NOT A HOLE: a breakout chase deliberately enters OUTSIDE the
zone -- that is its entire premise -- so it is exempt by design, and it is the one path
that already announces itself via state.is_chase.
"""
import pytest

from bot.indicators import entry_respects_zone

# the real ASTER numbers
Z_LO, Z_HI = 0.7190, 0.7588
FILL       = 0.7906


def test_the_real_aster_fill_is_refused():
    ok, why = entry_respects_zone(FILL, Z_LO, Z_HI, is_long=True, is_chase=False)
    assert not ok
    assert "above" in why.lower()


def test_the_refusal_reports_how_far_out_it_was():
    _, why = entry_respects_zone(FILL, Z_LO, Z_HI, is_long=True, is_chase=False)
    assert "4.2" in why or "4.19" in why, f"should quantify the miss, got: {why}"


def test_a_fill_inside_the_zone_passes():
    ok, _ = entry_respects_zone(0.7400, Z_LO, Z_HI, is_long=True, is_chase=False)
    assert ok


def test_fill_at_either_edge_passes():
    assert entry_respects_zone(Z_LO, Z_LO, Z_HI, True, False)[0]
    assert entry_respects_zone(Z_HI, Z_LO, Z_HI, True, False)[0]


def test_overshooting_past_a_long_zone_is_allowed():
    """Filling BELOW a demand zone is a better price for a long -- that is not the bug."""
    ok, _ = entry_respects_zone(Z_LO * 0.995, Z_LO, Z_HI, is_long=True, is_chase=False)
    assert ok


def test_short_side_mirrors():
    """A short must not fill BELOW its supply zone -- selling supply at a discount. This
    is the AVAX 2026-08-11 incident, guarded here at execution as well as at the tap."""
    ok, _ = entry_respects_zone(0.7000, Z_LO, Z_HI, is_long=False, is_chase=False)
    assert not ok
    ok, _ = entry_respects_zone(Z_HI * 1.005, Z_LO, Z_HI, is_long=False, is_chase=False)
    assert ok, "overshooting deeper into supply improves a short's fill"


def test_chase_is_exempt_by_design():
    """A breakout chase enters outside the zone on purpose."""
    ok, why = entry_respects_zone(FILL, Z_LO, Z_HI, is_long=True, is_chase=True)
    assert ok
    assert "chase" in why.lower()


def test_missing_zone_does_not_block_an_entry():
    """Fail OPEN on absent data: some engines legitimately carry no zone, and a guard
    that vetoes those would silently switch the bot off."""
    assert entry_respects_zone(FILL, None, None, True, False)[0]
    assert entry_respects_zone(FILL, Z_LO, None, True, False)[0]


def test_degenerate_zone_does_not_divide_by_zero():
    ok, _ = entry_respects_zone(0.79, 0.0, 0.0, True, False)
    assert ok


def test_small_drift_between_tap_and_fill_is_tolerated():
    """`price` is not re-read between the tap check and execution, and the AI call in
    between can take up to 25s. A fill a few ticks past the edge is normal; only a
    material miss is a defect."""
    just_past = Z_HI * 1.002          # 0.2% above the zone top
    assert entry_respects_zone(just_past, Z_LO, Z_HI, True, False)[0]
    well_past = Z_HI * 1.02           # 2% above
    assert not entry_respects_zone(well_past, Z_LO, Z_HI, True, False)[0]

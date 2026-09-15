"""Two gates from the 2026-09-14 four-trade review.

1. THE choch_fvg DIRECT TAP HAD NO CHECK AT ALL.
   The premise is sound — the displacement that left the gap already broke structure, so a
   quick retest is self-confirming. But "no confirmation needed" was implemented as "no
   check at all", and those are different. POL/USD tapped a LONG zone on a `marubozu_bear`
   (a full-bodied DOWN bar) with the bot logging '⚠️ marubozu_bear (no candle confirm)'
   and entering anyway. Not an unconfirmed tap — an actively contradicted one.

   The fix is a VETO, not a confirmation requirement, chosen deliberately: on a tape that
   mostly consolidates, demanding positive confirmation would cut trade count hard for
   little gain. Only the decisive opposing bar is blocked.

2. SWEEP_PATIENCE WAS UNREACHABLE.
   `sweep_hunt_bar > 48 and not state.sweep_low` — sweep_low is set on the FIRST sweep and
   only cleared by reset(), so one sweep disabled the 4h limit permanently. BTC held
   SWEEP_HUNT for 373 cycles (~62h50m) on a three-day-old 1H BOS, surviving a restart
   because sweep_hunt_bar is persisted. POL's was ~34h.
"""
import pytest

from bot.indicators import sweep_hunt_expired, tap_candle_opposes_bias

SWEEP_PATIENCE = 48


# ── 1. the tap veto ──

def test_the_real_pol_tap_is_vetoed():
    """marubozu_bear at a LONG tap — the bar that was entered on."""
    assert tap_candle_opposes_bias("marubozu_bear", "BULLISH")


def test_the_real_xrp_tap_still_passes():
    """`normal` is not opposition. XRP tapped on it and won +$26.49."""
    assert not tap_candle_opposes_bias("normal", "BULLISH")


def test_a_doji_still_passes():
    """Indecision is not opposition — vetoing it would gut trade count on a flat tape."""
    assert not tap_candle_opposes_bias("doji", "BULLISH")


@pytest.mark.parametrize("c", ["shooting_star", "gravestone_doji", "bearish_engulfing",
                               "hanging_man", "marubozu_bear"])
def test_every_decisive_down_bar_vetoes_a_long(c):
    assert tap_candle_opposes_bias(c, "BULLISH")


@pytest.mark.parametrize("c", ["hammer", "dragonfly_doji", "bullish_engulfing",
                               "inverted_hammer", "marubozu_bull"])
def test_the_short_side_mirrors(c):
    assert tap_candle_opposes_bias(c, "BEARISH")
    assert not tap_candle_opposes_bias(c, "BULLISH"), "a bullish bar must not veto a LONG"


def test_a_confirming_bar_is_never_vetoed():
    assert not tap_candle_opposes_bias("hammer", "BULLISH")
    assert not tap_candle_opposes_bias("marubozu_bull", "BULLISH")


def test_unknown_bias_vetoes_nothing():
    assert not tap_candle_opposes_bias("marubozu_bear", None)


# ── 2. the BOS expiry ──

def test_the_old_guard_was_unreachable_once_any_sweep_printed():
    """Documents the defect: `bars > patience AND not has_sweep`."""
    bars, has_sweep = 373, True
    assert bars > SWEEP_PATIENCE
    assert not (bars > SWEEP_PATIENCE and not has_sweep), "old guard could never fire"


def test_the_real_btc_state_now_expires():
    """373 cycles (~62h50m) on a three-day-old BOS, with a sweep on the books."""
    expired, why = sweep_hunt_expired(373, SWEEP_PATIENCE, has_sweep=True)
    assert expired and "too stale" in why


def test_the_real_pol_state_now_expires():
    expired, _ = sweep_hunt_expired(195, SWEEP_PATIENCE, has_sweep=True)
    assert expired


def test_no_sweep_at_all_still_expires_at_the_original_limit():
    assert sweep_hunt_expired(49, SWEEP_PATIENCE, has_sweep=False)[0]
    assert not sweep_hunt_expired(48, SWEEP_PATIENCE, has_sweep=False)[0]


def test_a_swept_setup_gets_the_longer_leash_not_an_infinite_one():
    """With a sweep it survives past `patience`, but not past the hard ceiling."""
    assert not sweep_hunt_expired(60, SWEEP_PATIENCE, has_sweep=True)[0]
    assert not sweep_hunt_expired(96, SWEEP_PATIENCE, has_sweep=True)[0]
    assert sweep_hunt_expired(97, SWEEP_PATIENCE, has_sweep=True)[0]


def test_a_young_hunt_is_untouched():
    assert not sweep_hunt_expired(5, SWEEP_PATIENCE, has_sweep=False)[0]
    assert not sweep_hunt_expired(5, SWEEP_PATIENCE, has_sweep=True)[0]


@pytest.mark.parametrize("bad", [None, "x", -1])
def test_bad_input_does_not_expire_a_live_setup(bad):
    """Failing OPEN here is right: a parse error must not silently drop every zone."""
    assert not sweep_hunt_expired(bad, SWEEP_PATIENCE, True)[0]
    assert not sweep_hunt_expired(100, bad, True)[0]

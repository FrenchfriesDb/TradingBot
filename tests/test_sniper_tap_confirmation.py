"""The sniper must enforce the SAME tap rule as the 5-minute cycle.

THE DIVERGENCE. The 5m ENTRY_WAIT path requires, on a fresh zone:

    choch_fvg      -> direct tap (the displacement IS the change of character)
    anything else  -> rebounce = candle_confirms OR choch_aligned

The sniper checked nothing on a fresh zone. A staleness gate was later added whose comment
claimed it mirrored the main cycle "exactly" — but it only covered the STALE branch.

Because the sniper polls every 10s against the main cycle's 5 minutes, it wins nearly
every race, so the candle gate was effectively dead code. Measured over one log:

    22 zone taps graded by the bot's own check
    19 FAILED it (11 'normal', 4 marubozu_bear, shooting_star, gravestone_doji,
       bearish_engulfing, doji) -- and were entered anyway
     3 passed

Four marubozu_bear candles -- a strong BEARISH bar -- at LONG entries. And 35 of 36 zones
armed via the generic OB/FVG fallback, which is NOT choch_fvg, so every one of them should
have required confirmation.
"""
import pytest

from bot.indicators import sniper_entry_allowed


def _call(zone_type="bullish_ob", is_stale=False, has_momentum=False,
          candle_confirms=False, choch_aligned=False):
    return sniper_entry_allowed(zone_type, is_stale, has_momentum,
                                candle_confirms, choch_aligned)


# ── the real taps this fixes ──

def test_marubozu_bear_at_a_long_tap_is_refused():
    """Four of these were entered. A bearish marubozu fails candle_confirms_bias."""
    ok, why = _call(candle_confirms=False)
    assert not ok and "does not confirm" in why


def test_a_plain_normal_candle_is_refused():
    """Eleven of the nineteen failures were simply 'normal' — no pattern at all."""
    assert not _call(candle_confirms=False)[0]


def test_a_confirming_candle_passes():
    assert _call(candle_confirms=True)[0]


# ── parity with the 5-minute path ──

def test_fresh_choch_fvg_still_gets_a_direct_tap():
    """The displacement that left the gap already broke structure — that IS the
    confirmation, exactly as the 5m path treats it."""
    ok, why = _call(zone_type="choch_fvg")
    assert ok and "direct tap" in why


def test_the_fallback_zone_types_all_require_confirmation():
    """35 of 36 arms were OB/FVG fallbacks, none of which are choch_fvg."""
    for zt in ("bullish_ob", "bullish_breaker", "bullish_fvg", "ifvg_support", None):
        assert not _call(zone_type=zt)[0], f"{zt} must require confirmation"


def test_choch_alignment_also_satisfies_it():
    assert _call(choch_aligned=True)[0]


# ── the stale branch, unchanged ──

def test_stale_zone_still_needs_fresh_momentum():
    assert not _call(is_stale=True, has_momentum=False)[0]
    assert _call(is_stale=True, has_momentum=True)[0]


def test_staleness_outranks_a_pretty_candle():
    """A stale zone needs real momentum; a confirming candle alone is not enough."""
    assert not _call(is_stale=True, has_momentum=False, candle_confirms=True)[0]


def test_a_stale_choch_fvg_gets_no_free_pass_either():
    assert not _call(zone_type="choch_fvg", is_stale=True, has_momentum=False)[0]


# ── the deliberate asymmetry ──

def test_sniper_is_stricter_not_looser_when_choch_is_unknown():
    """It does not fetch 15m on a 10s cadence, so choch_aligned arrives False. A tap it
    declines is picked up by the next 5m cycle, which does have the 15m read."""
    assert not _call(candle_confirms=False, choch_aligned=False)[0]
    assert _call(candle_confirms=False, choch_aligned=True)[0], \
        "if a caller CAN supply it, it must still count"

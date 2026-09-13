"""Regression target: ADA fired a real trade under amd_phase='trend_follow' AFTER
ENABLE_TREND_FOLLOW was set False and the bot was restarted. Root cause — the gate
only blocked the ARMING code path (`if ENABLE_TREND_FOLLOW and state.state=="IDLE"...`),
but a zone already sitting in ENTRY_WAIT (armed by an OLDER, pre-freeze process run,
then restored verbatim from crypto_state.json on restart via load_crypto_state) is
engine-agnostic at tap time — the fill logic only checks confirmation signals, not which
engine armed the zone. So a stale pending zone from a disabled engine could still fire.

is_disabled_engine_zone() is the check a startup reconcile uses to reset any such
stale pending zone to IDLE before it gets a chance to tap in."""
import pytest

from bot.indicators import is_disabled_engine_zone


def test_trend_follow_zone_is_disabled_when_flag_is_off():
    assert is_disabled_engine_zone("trend_follow", enable_trend_follow=False,
                                   enable_wedge_breakout=True) is True


def test_trend_follow_zone_is_allowed_when_flag_is_on():
    assert is_disabled_engine_zone("trend_follow", enable_trend_follow=True,
                                   enable_wedge_breakout=True) is False


def test_wedge_breakout_zone_is_disabled_when_flag_is_off():
    assert is_disabled_engine_zone("wedge_breakout", enable_trend_follow=True,
                                   enable_wedge_breakout=False) is True


def test_wedge_breakout_zone_is_allowed_when_flag_is_on():
    assert is_disabled_engine_zone("wedge_breakout", enable_trend_follow=True,
                                   enable_wedge_breakout=True) is False


def test_amd_manipulation_zones_are_never_disabled():
    assert is_disabled_engine_zone("manipulation_up", enable_trend_follow=False,
                                   enable_wedge_breakout=False) is False
    assert is_disabled_engine_zone("manipulation_down", enable_trend_follow=False,
                                   enable_wedge_breakout=False) is False


def test_breakout_chase_is_never_disabled():
    # chase is a continuation of an already-armed BOS zone, not a separate primary
    # engine the user asked to gate off.
    assert is_disabled_engine_zone("breakout_chase", enable_trend_follow=False,
                                   enable_wedge_breakout=False) is False


def test_none_phase_is_never_disabled():
    # BOS-displacement zones never set amd_phase at all.
    assert is_disabled_engine_zone(None, enable_trend_follow=False,
                                   enable_wedge_breakout=False) is False

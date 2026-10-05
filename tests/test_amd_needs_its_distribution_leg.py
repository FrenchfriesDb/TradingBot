"""An AMD sweep is MANIPULATION. On its own it is not a setup.

The model is accumulation -> manipulation -> DISTRIBUTION. Both AMD branches armed on the
manipulation alone:

    sweep detected + daily trend agrees + ATR gate + depth >= 0.3% + "a zone exists"

then straight to waiting for a pullback. Nothing asked whether price actually displaced out
of the sweep — whether the stop-run resolved into a move or was just a wick. The BOS route
has always required detect_displacement_bos; the AMD route required nothing equivalent, and
AMD is roughly a third of all arming.

Operator, on an AERO long armed from a 6H high-sweep: "if it was an AMD, it needs a strong
breakout". The code did not ask for one.

Checked at the FORMATION multiple (1.8x HTF ATR), not the tap multiple (1.4x 5m ATR): this
asks whether the move happened at all, which is a stronger and different question from
whether the retest candle is decisive.
"""
import pandas as pd
import pytest

import binance_bot as B


@pytest.fixture(autouse=True, scope="module")
def _libs_loaded():
    """binance_bot loads pandas/indicators on a BACKGROUND THREAD and exposes them as
    globals. Production waits on _libs_ready before it trades; a test that does not will
    see `indicators` undefined and get a swallowed AttributeError that looks exactly like
    a failing gate. Wait for the same event the bot waits for."""
    assert B._libs_ready.wait(timeout=120), "indicators never finished loading"


def _frame(bars):
    return pd.DataFrame(bars, columns=["open", "high", "low", "close"])


def _flat(n=20, px=100.0):
    """Noise: tiny bodies, no displacement anywhere."""
    return [[px, px + 0.2, px - 0.2, px + 0.05] for _ in range(n)]


def _with_push(n=20, px=100.0, up=True, size=12.0):
    bars = _flat(n - 1, px)
    bars.append([px, px + size, px - 0.2, px + size] if up
                else [px, px + 0.2, px - size, px - size])
    return bars


class TestDistributionGate:
    def test_a_flat_tape_is_refused(self):
        ok, why = B.amd_distribution_confirmed(_frame(_flat()), True)
        assert not ok and "distribution" in why

    def test_a_real_push_up_confirms_a_long(self):
        ok, _ = B.amd_distribution_confirmed(_frame(_with_push(up=True)), True)
        assert ok

    def test_a_real_push_down_confirms_a_short(self):
        ok, _ = B.amd_distribution_confirmed(_frame(_with_push(up=False)), False)
        assert ok

    def test_direction_matters(self):
        """A push DOWN cannot confirm a LONG. Without this the gate is just a volatility
        filter wearing a structure costume."""
        ok, _ = B.amd_distribution_confirmed(_frame(_with_push(up=False)), True)
        assert not ok

    def test_fails_closed_on_unreadable_data(self):
        """An unverifiable breakout must not become a free pass — same rule the tap
        momentum check follows."""
        ok, why = B.amd_distribution_confirmed(_frame([[1, 1, 1, 1]]), True)
        assert not ok and why

    def test_never_raises(self):
        ok, why = B.amd_distribution_confirmed(None, True)
        assert ok is False and why

    def test_the_flag_can_turn_it_off_for_measurement(self, monkeypatch):
        monkeypatch.setattr(B, "AMD_REQUIRE_DISPLACEMENT", False)
        ok, _ = B.amd_distribution_confirmed(_frame(_flat()), True)
        assert ok, "the flag must restore the old behaviour exactly, for A/B measurement"

    def test_it_is_on_by_default(self):
        assert B.AMD_REQUIRE_DISPLACEMENT is True


class TestBothBranchesAreGated:
    """The low-sweep and high-sweep paths are mirror images and have drifted apart before."""

    def test_both_amd_branches_call_the_producer(self):
        import pathlib
        src = (pathlib.Path(B.__file__).parent / "binance_bot.py").read_text()
        # one definition + one call per branch
        assert src.count("amd_distribution_confirmed(") == 3, (
            "both AMD branches must gate on the shared producer — one of them is unguarded")

    def test_neither_branch_finds_a_zone_before_confirming(self):
        """Order matters: confirming AFTER arming would log a refusal for a zone already
        armed."""
        import pathlib
        src = (pathlib.Path(B.__file__).parent / "binance_bot.py").read_text()
        for zone_fn in ("find_supply_zone", "find_demand_zone"):
            i = src.index(zone_fn + "(df_htf_closed")
            window = src[max(0, i - 500):i]
            assert "amd_distribution_confirmed" in window, (
                f"{zone_fn} is reached without the distribution check above it")

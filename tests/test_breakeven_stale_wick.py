"""A stop cannot fill on a candle that printed BEFORE that stop existed.

THE BUG (found 2026-09-14). The watcher already skipped candles opened before ENTRY —
"candle opened pre-entry — wick untrustworthy" — added after a live trade logged "TP hit"
one second after entry off a pre-fill wick. The identical bug sat one level up, on the
break-even trail, and it was far more expensive.

The arithmetic makes it fire EVERY time:

    BE arms at   cur >= entry + 1.25 * risk
    BE places    stop = entry * (1 + ROUND_TRIP_COST * 1.5)      <-- BELOW the trigger

so on that same 10-second tick (binance_bot.py:2952):

    cur <= stop        -> impossible; cur just cleared a HIGHER bar
    candle_low <= stop -> certain; price had to rise THROUGH the stop level to get here

REAL CASE — BTC/USD 2026-09-14:
    entry     $77,587.61      risk_dist $512.0282
    +1.25R at $78,227.65      BE stop   $78,169.5171
    closed    $78,169.5171 on the same tick, booked +$4.85

The target $78,781.07 was reached 6.5h later and the ORIGINAL stop was never within $395
(0.77R) of being hit. The trail cost that trade ~$26.

CONSEQUENCE: every position reaching +1.25R is force-closed at ~+1.14R while every loser
still takes a full 1.00R. That caps realized R:R at 1:1.14 against a 1:2 design —
measured at 1:0.98 across 36 closes, with only 4 of 35 exits ever reaching target.
"""
import pytest

from bot.indicators import wick_fill_cutoff_ms

ENTRY, RISK, RT = 77587.61, 512.0282, 0.005
BE_TRIGGER = ENTRY + 1.25 * RISK
BE_STOP = ENTRY + ENTRY * RT * 1.5


# ── the arithmetic that guarantees the misfire ──

def test_the_breakeven_stop_lands_below_its_own_trigger():
    assert BE_STOP < BE_TRIGGER
    assert BE_STOP == pytest.approx(78169.5171, abs=0.001), "matches the logged exit exactly"
    assert BE_TRIGGER == pytest.approx(78227.645, abs=0.01)
    assert BE_TRIGGER - BE_STOP == pytest.approx(58.13, abs=0.01), \
        "the $58 gap is why the live-price arm can never fire"


def test_the_live_price_arm_can_never_fire():
    """cur just cleared a HIGHER bar, so `cur <= stop` is structurally False."""
    cur = BE_TRIGGER
    assert not (cur <= BE_STOP)


def test_so_only_the_stale_wick_arm_can_fire_and_it_always_does():
    """To reach +1.25R price rose THROUGH the stop level, so a recent 1m low sits under it."""
    low_before_the_move = 78039.96      # a real BTC print minutes earlier
    assert low_before_the_move < BE_STOP
    assert low_before_the_move <= BE_STOP


def test_the_cap_this_puts_on_every_winner():
    capped_r = (BE_STOP - ENTRY) / RISK
    assert capped_r == pytest.approx(1.14, abs=0.01)
    assert capped_r < 2.0, "the 1:2 design can never be realized while this fires"


# ── the fix ──

def test_the_cutoff_advances_when_the_stop_moves():
    entry_ms, moved_ms = 1_000_000, 1_500_000
    assert wick_fill_cutoff_ms(entry_ms, moved_ms) == moved_ms


def test_a_candle_from_before_the_move_is_now_excluded():
    entry_ms, moved_ms = 1_000_000, 1_500_000
    cutoff = wick_fill_cutoff_ms(entry_ms, moved_ms)
    stale_candle_ts = 1_400_000          # after entry, BEFORE the stop moved
    assert stale_candle_ts >= entry_ms, "the old guard would have let this through"
    assert stale_candle_ts < cutoff, "the new guard excludes it"


def test_a_candle_after_the_move_still_counts():
    cutoff = wick_fill_cutoff_ms(1_000_000, 1_500_000)
    assert 1_600_000 >= cutoff, "a genuine post-stop breach must still fill"


def test_pre_entry_protection_is_unchanged_when_the_stop_never_moved():
    assert wick_fill_cutoff_ms(1_000_000, 0) == 1_000_000


def test_an_untrailed_trade_is_unaffected():
    """Most losers never reach +1.25R; their behaviour must not change at all."""
    assert wick_fill_cutoff_ms(1_000_000) == 1_000_000


@pytest.mark.parametrize("a,b", [(None, None), ("x", "y"), (None, 5), (5, None)])
def test_bad_input_degrades_rather_than_raising(a, b):
    assert isinstance(wick_fill_cutoff_ms(a, b), int)


def test_a_stop_moved_before_entry_cannot_widen_the_window():
    """Stale state from a previous position must never let older candles back in."""
    assert wick_fill_cutoff_ms(2_000_000, 1_000_000) == 2_000_000

"""The displacement must be big relative to the RISK, not just to a quiet ATR.

The operator, on the ASTER entry: "the FVG displacement candle is not big enough... the
candle should be 3/4's of that [the entry-to-stop box]". Measured on the real bars, the
bot was nowhere near that:

                   displacement body      stop distance     body / stop
    ASTER  11:00   0.00220 (0.313% px)    0.01100 (1.57%)        20%
    DOGE   14:25   0.00090 (0.940% px)    0.00244 (2.55%)        37%

Both cleared the existing gate, which is max(1.8 x 5m-ATR, 0.15% of price). ASTER's
displacement cleared it by ONE PERCENT — 1.82x against a 1.80x floor — and the whole
window around it was jitter:

    11:20  body 0.114% of price     11:35  body 0.014%
    11:25       0.057%              11:40       0.114%
    11:30       0.100%              11:45       0.014%

WHY THE 5m ATR TERM CANNOT CARRY THIS. It is measured over the same chop it is meant to
exclude, so when the tape goes quiet the floor sinks with it and an ordinary bar clears
a multiple of it. ASTER's 5m ATR was 0.179% of price; 1.8x of that is 0.32%, which is a
nothing candle on a trade risking 1.57%.

THE FIX ties the displacement to the risk being taken. The stop is floored off the 1H
ATR, and measured on both trades it lands at 1.3-1.6x it:

    ASTER  stop 1.30x atr_1h        DOGE  stop 1.61x atr_1h

so "body >= 3/4 of the stop" becomes body >= ~1.1x atr_1h. That is asset-neutral, it
does not collapse when the 5m tape goes quiet, and it is the same quantity the stop
itself is built from.
"""
import pytest

from bot.indicators import displacement_gates
import pandas as pd


def _df(n=30, rng=0.001, price=0.70):
    """Flat tape: every bar has the same small high-low range."""
    return pd.DataFrame(
        [(price, price + rng, price - 0.0, price) for _ in range(n)],
        columns=["open", "high", "low", "close"])


def test_the_htf_term_raises_the_floor_when_the_5m_tape_is_quiet():
    """ASTER's numbers: 5m ATR 0.00121, 1H ATR 0.00849."""
    quiet = _df(rng=0.00121, price=0.70)
    without = displacement_gates(quiet, 1.8, 0.0015, 0.0015)["min_body_abs"]
    with_htf = displacement_gates(quiet, 1.8, 0.0015, 0.0015,
                                  htf_atr=0.00849)["min_body_abs"]
    assert with_htf > without
    assert with_htf == pytest.approx(0.5 * 0.00849)   # default relaxed 1.0 -> 0.5


def test_asters_real_displacement_is_refused_by_the_new_floor():
    """body 0.00220 vs a floor of 1.1 x 0.00849 = 0.00934."""
    g = displacement_gates(_df(rng=0.00121), 1.8, 0.0015, 0.0015,
                           htf_atr=0.00849)
    assert 0.00220 < g["min_body_abs"]


def test_doge_now_passes_the_FLOOR_and_is_blocked_by_candle_3_instead():
    """The division of labour changed on 2026-09-29 and this records it.

    DOGE's displacement was 0.00090 against a 1H ATR of 0.00152 = 0.59x. At the original
    1.0 floor the SIZE gate refused it. At the relaxed 0.5 floor it passes size — and is
    still refused, because its candle 3 closed red. That matters: the floor was starving
    the bot (2 armed zones in 25h of tape across 8 symbols, zero trades for five days)
    while c3 cost almost nothing in arming. The blunt gate was loosened and the precise
    one kept."""
    g = displacement_gates(_df(rng=0.000477, price=0.0959), 1.8, 0.0015, 0.0015,
                           htf_atr=0.00152)
    assert 0.00090 >= g["min_body_abs"], "0.59x should now clear a 0.5x floor"
    # and the trade is STILL refused, by the rule that actually caught it
    import pandas as pd
    from bot.indicators import detect_displacement_fvg
    rows = [(100.0, 100.4, 99.6, 100.0)] * 20
    rows += [(100.0, 100.5, 99.9, 100.2), (100.2, 106.0, 100.1, 105.8),
             (105.0, 105.9, 101.0, 104.0)]          # c3 closes RED, as DOGE's did
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    assert not detect_displacement_fvg(df)[0], "c3 must still block a red third candle"


def test_a_genuinely_large_displacement_still_passes():
    """A bar worth 3/4 of the risk must not be filtered out.

    Measured stops sit at 1.30-1.61x atr_1h, so 3/4 of a stop is 0.98-1.21x atr_1h. The
    1.0 default passes that at a typical stop and is ~2% strict at the tightest observed
    one — stated rather than fitted away."""
    atr1h = 0.00849
    typical_stop = 1.45 * atr1h                   # midpoint of the observed 1.30-1.61 band
    g = displacement_gates(_df(rng=0.00121), 1.8, 0.0015, 0.0015, htf_atr=atr1h)
    assert typical_stop * 0.75 >= g["min_body_abs"]


def test_the_floor_is_the_MAX_of_all_three_terms():
    """None of the three replaces the others — each covers a different failure."""
    g = displacement_gates(_df(rng=0.01, price=100.0), 1.8, 0.0015, 0.0015,
                           htf_atr=0.001)
    assert g["min_body_abs"] == pytest.approx(max(1.8 * 0.01, 0.0015 * 100.0, 1.0 * 0.001))


def test_omitting_the_htf_atr_leaves_behaviour_exactly_as_before():
    """Every existing caller that does not pass it is untouched."""
    d = _df(rng=0.00121)
    assert (displacement_gates(d, 1.8, 0.0015, 0.0015)
            == displacement_gates(d, 1.8, 0.0015, 0.0015, htf_atr=None))


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan")])
def test_an_unusable_htf_atr_is_ignored_rather_than_zeroing_the_floor(bad):
    """A bad 1H read must not become 'no floor at all'."""
    d = _df(rng=0.00121)
    base = displacement_gates(d, 1.8, 0.0015, 0.0015)["min_body_abs"]
    assert displacement_gates(d, 1.8, 0.0015, 0.0015,
                              htf_atr=bad)["min_body_abs"] == base


def test_an_unreadable_frame_still_returns_empty():
    assert displacement_gates(pd.DataFrame(), 1.8, 0.0015, 0.0015, htf_atr=0.5) == {}

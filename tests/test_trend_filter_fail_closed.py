"""An unreadable trend is not permission to fade a rally.

WHAT HAPPENED (2026-09-18). test_bot shorted ADA at $0.2291 — the exact 1H high — into a
move that had run from $0.2210 and carried on to $0.2323. Stopped out for the full
$42.97. The operator's words: "why did the stock bot try to stop a flying rocket? the
opposite of trying to buy a falling knife."

The filter that exists to prevent precisely this was already in the code, and its comment
names the failure by name:

    # A LONG sweep is buy-the-dip (needs an uptrend); a SHORT sweep is sell-the-rip
    # (needs a downtrend). Blocking the counter-trend side stops the bot fading a
    # running move — the "all shorts in an uptrend, all stopped" pattern from the
    # ledger. None = not enough data, allow.

Measured afterwards against real ADA 1h data resampled to 4H, the trend at the moment of
entry was unambiguous:

    entry-24h  UP      entry-6h  UP      entry-1h  UP
    entry-18h  UP      entry-4h  UP      entry-0h  UP   <-- entry taken here
    entry-12h  UP      entry-2h  UP

So the gate could not have evaluated and allowed it. It never evaluated at all. There are
exactly two ways that happens, and both end on `trend = None`, which that last comment
spells out as "allow":

    except Exception:                 # any network blip during the 1h fetch
        trend = None

and trend_direction() returning None when fewer than EMA_LEN bars arrive — with the fetch
sized at (30+5)*4 = 140 hourly candles, that is 35 4H bars against a 30-bar minimum. A
five-bar margin. Any short or partial response silently removes the filter.

THE SAME SHAPE AS THE AI GATE, one file over: a check that cannot reach a verdict was
treated as a verdict in favour. "I could not read the trend" is not "the trend is fine".
On the direction that matters, not knowing has to mean not trading — the next 1-minute
tick will try again, and declining late is recoverable in a way that fading a rocket is not.
"""
import pytest

from bot.indicators import trend_filter_verdict


# ── the live failure ──────────────────────────────────────────────────────────

def test_shorting_into_a_confirmed_uptrend_is_blocked():
    allowed, why = trend_filter_verdict("UP", "SHORT")
    assert allowed is False
    assert "UP" in why


def test_an_unreadable_trend_no_longer_permits_the_trade():
    """The ADA case: the fetch failed, trend came back None, and the short went on."""
    allowed, why = trend_filter_verdict(None, "SHORT")
    assert allowed is False
    assert "could not" in why.lower() or "unknown" in why.lower()


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_an_unreadable_trend_blocks_both_directions(direction):
    """Without the read we do not know WHICH side is the counter-trend one, so neither
    may pass. The filter's whole purpose is knowing that."""
    assert trend_filter_verdict(None, direction)[0] is False


# ── the trades it must still allow ────────────────────────────────────────────

def test_buying_the_dip_in_an_uptrend_is_allowed():
    assert trend_filter_verdict("UP", "LONG")[0] is True


def test_selling_the_rip_in_a_downtrend_is_allowed():
    assert trend_filter_verdict("DOWN", "SHORT")[0] is True


def test_longing_into_a_downtrend_is_blocked():
    allowed, why = trend_filter_verdict("DOWN", "LONG")
    assert allowed is False and "DOWN" in why


# ── degenerate input ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("junk", ["", "sideways", 0, object()])
def test_an_unrecognised_trend_value_is_treated_as_unreadable(junk):
    """Anything that is not UP or DOWN is 'we do not know', never 'go ahead'."""
    assert trend_filter_verdict(junk, "SHORT")[0] is False


@pytest.mark.parametrize("junk", ["", "sideways", None, 0])
def test_an_unrecognised_direction_is_refused(junk):
    assert trend_filter_verdict("UP", junk)[0] is False


def test_case_and_whitespace_do_not_defeat_the_filter():
    """A stray lowercase value must not read as unknown and re-open the hole."""
    assert trend_filter_verdict(" up ", "short")[0] is False
    assert trend_filter_verdict("up", "long")[0] is True


def test_the_reason_is_specific_enough_to_diagnose_from_the_log():
    """"skipped" told us nothing; the ADA entry took an hour to reconstruct."""
    for trend, direction in (("UP", "SHORT"), (None, "LONG")):
        _, why = trend_filter_verdict(trend, direction)
        assert len(why) > 15 and why.strip() == why

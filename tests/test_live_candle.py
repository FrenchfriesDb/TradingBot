"""The newest candle has to follow live price, because Coinbase's does not.

WHAT THE OPERATOR SAW: "the chart candles are updating a bit slow and behind ngl".

MEASURED 2026-09-19, sampling both endpoints every 5s for 43 seconds:

       t  candle close   live ticker       gap
       0     81,250.15     81,251.29     -1.14
      11     81,250.15     81,242.44     +7.71
      22     81,250.15     81,229.16    +20.99
      38     81,250.15     81,229.85    +20.30
      43     81,250.15     81,250.23     -0.08

The candle close never moved. /products/{id}/candles is Coinbase's HISTORIC-RATES
endpoint: it serves completed buckets from a cache and does not stream the bucket
currently forming. chart_server polls it every 5 seconds, so it re-fetched the same
frozen bucket nine times and the chart only jumped when Coinbase's own cache rolled.
The server was adding no staleness of its own — measured identical to a direct call.

So the granularity of the poll was never the problem and polling harder would not have
helped. What a live chart actually does is take completed candles from history and drive
the FORMING one from the live feed. That is what these functions do:

  * within the newest bucket's window, the ticker updates its close, and extends its
    high or low if price has traded beyond what the cached bucket knows about;
  * once the clock has passed that window and Coinbase has not yet published the next
    bucket, a new one is opened at the live price rather than leaving the chart frozen
    on a bucket whose time is over.

The invariant worth stating: this may only ever make the newest candle MORE current.
It must never rewrite a completed bucket, invent volume, or move a high or low inward —
a chart that edits history is worse than a chart that lags.
"""
import pytest

from bot.live_candle import patch_live_candle

GRAN = 300
T0 = 1_789_000_000          # a bucket boundary, divisible by 300


def _c(t, low, high, open_, close, vol=1.0):
    """Coinbase order: [time, low, high, open, close, volume]."""
    return [t, low, high, open_, close, vol]


def _newest(rows):
    return rows[0]


# ── updating the bucket that is still forming ─────────────────────────────────

def test_the_live_price_becomes_the_close_of_the_forming_candle():
    rows = [_c(T0, 100.0, 110.0, 105.0, 108.0)]
    out = patch_live_candle(rows, price=109.0, now_ts=T0 + 120, granularity=GRAN)
    assert _newest(out)[4] == 109.0


def test_a_new_high_extends_the_candle():
    rows = [_c(T0, 100.0, 110.0, 105.0, 108.0)]
    out = patch_live_candle(rows, price=112.0, now_ts=T0 + 120, granularity=GRAN)
    assert _newest(out)[2] == 112.0 and _newest(out)[4] == 112.0


def test_a_new_low_extends_the_candle():
    rows = [_c(T0, 100.0, 110.0, 105.0, 108.0)]
    out = patch_live_candle(rows, price=97.0, now_ts=T0 + 120, granularity=GRAN)
    assert _newest(out)[1] == 97.0 and _newest(out)[4] == 97.0


def test_a_price_inside_the_range_never_shrinks_the_wicks():
    """The high and low already happened. A later trade between them does not undo them."""
    rows = [_c(T0, 100.0, 110.0, 105.0, 108.0)]
    out = patch_live_candle(rows, price=106.0, now_ts=T0 + 120, granularity=GRAN)
    assert _newest(out)[1] == 100.0 and _newest(out)[2] == 110.0


def test_the_open_and_volume_of_the_forming_candle_are_left_alone():
    """The open is a fact and the volume is not ours to invent."""
    rows = [_c(T0, 100.0, 110.0, 105.0, 108.0, vol=42.0)]
    out = patch_live_candle(rows, price=112.0, now_ts=T0 + 120, granularity=GRAN)
    assert _newest(out)[3] == 105.0 and _newest(out)[5] == 42.0


# ── rolling into a bucket Coinbase has not published yet ──────────────────────

def test_a_new_bucket_opens_at_the_live_price_once_the_window_has_passed():
    """The frozen-chart case: the clock is into the next bucket and the cache has not
    caught up. A real chart starts a new candle rather than sitting still."""
    rows = [_c(T0, 100.0, 110.0, 105.0, 108.0)]
    out = patch_live_candle(rows, price=111.0, now_ts=T0 + 305, granularity=GRAN)
    assert len(out) == 2
    fresh = _newest(out)
    assert fresh[0] == T0 + GRAN                      # aligned to the boundary
    assert fresh[1] == fresh[2] == fresh[3] == fresh[4] == 111.0
    assert fresh[5] == 0.0, "we saw no volume, so do not claim any"


def test_the_completed_bucket_is_untouched_when_a_new_one_opens():
    rows = [_c(T0, 100.0, 110.0, 105.0, 108.0, vol=42.0)]
    out = patch_live_candle(rows, price=111.0, now_ts=T0 + 305, granularity=GRAN)
    assert out[1] == [T0, 100.0, 110.0, 105.0, 108.0, 42.0]


def test_a_far_future_now_still_opens_exactly_one_bucket():
    """A long gap (laptop asleep) must not fabricate the buckets nobody observed."""
    rows = [_c(T0, 100.0, 110.0, 105.0, 108.0)]
    out = patch_live_candle(rows, price=111.0, now_ts=T0 + 5000, granularity=GRAN)
    assert len(out) == 2
    assert _newest(out)[0] == T0 + (5000 // GRAN) * GRAN


# ── refusing to make things up ────────────────────────────────────────────────

def test_history_older_than_the_newest_bucket_is_never_rewritten():
    rows = [_c(T0, 100.0, 110.0, 105.0, 108.0), _c(T0 - GRAN, 90.0, 99.0, 92.0, 98.0)]
    out = patch_live_candle(rows, price=112.0, now_ts=T0 + 120, granularity=GRAN)
    assert out[1] == rows[1]


@pytest.mark.parametrize("bad_price", [None, 0, -1, "x", float("nan")])
def test_an_unusable_price_leaves_the_candles_exactly_as_they_came(bad_price):
    rows = [_c(T0, 100.0, 110.0, 105.0, 108.0)]
    assert patch_live_candle(rows, bad_price, T0 + 120, GRAN) == rows


@pytest.mark.parametrize("rows", [[], None, "nope"])
def test_missing_candles_are_returned_unchanged_rather_than_synthesised(rows):
    """No history means no chart. Inventing a lone candle from a ticker would draw a
    confident picture of nothing."""
    assert patch_live_candle(rows, 111.0, T0 + 120, GRAN) == rows


def test_a_malformed_row_is_left_alone_instead_of_raising():
    rows = [["garbage"], _c(T0 - GRAN, 90.0, 99.0, 92.0, 98.0)]
    assert patch_live_candle(rows, 111.0, T0 + 120, GRAN) == rows


def test_a_stale_price_from_before_the_bucket_is_ignored():
    """now_ts belongs to the caller's clock; if it predates the newest bucket the data
    is inconsistent and patching would move the candle backwards in time."""
    rows = [_c(T0, 100.0, 110.0, 105.0, 108.0)]
    assert patch_live_candle(rows, 111.0, T0 - 50, GRAN) == rows


def test_the_input_list_is_not_mutated():
    """The caller may be serving a cached list to other requests."""
    rows = [_c(T0, 100.0, 110.0, 105.0, 108.0)]
    snapshot = [list(r) for r in rows]
    patch_live_candle(rows, 112.0, T0 + 120, GRAN)
    assert rows == snapshot

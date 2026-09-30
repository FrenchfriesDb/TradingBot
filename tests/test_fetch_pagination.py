"""A short page must not end the walk.

WHY (2026-09-30). Coinbase serves a SHORT first page the further back you ask — 280 bars
at -60d on AVAX/USD, 272 at -90d — and fetch_paginated treated "fewer than requested" as
end-of-data, stopping the entire fetch. The tell was a 60-day run returning fewer taps
than a 14-day one (DOGE 1,625 -> 908; AVAX 1,937 -> 0), which market data cannot do.

Every long-window crypto measurement taken before this fix ran on truncated history.
"""
import backtest_crypto as bc

TF_SECS = 300
STEP = TF_SECS * 1000


class FakeEx:
    """Serves `n_total` bars from `start`, but the FIRST page is deliberately short."""

    def __init__(self, start, n_total, first_page=280):
        self.start, self.n_total, self.first_page = start, n_total, first_page
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=300):
        self.calls += 1
        end = self.start + self.n_total * STEP
        if since >= end:
            return []
        take = self.first_page if self.calls == 1 else limit
        bars = []
        t = since
        while t < end and len(bars) < take:
            bars.append([t, 1.0, 1.0, 1.0, 1.0, 1.0])
            t += STEP
        return bars


def test_short_first_page_does_not_end_the_walk():
    ex = FakeEx(start=0, n_total=1000, first_page=280)
    df = bc.fetch_paginated(ex, "AVAX/USD", "5m", TF_SECS, 0, 1000)
    assert len(df) == 1000, (
        f"got {len(df)} of 1000 bars — a short page ended the walk again")
    assert ex.calls > 1


def test_walk_stops_at_an_empty_page():
    """Fewer bars exist than requested: return what there is, do not loop forever."""
    ex = FakeEx(start=0, n_total=400, first_page=280)
    df = bc.fetch_paginated(ex, "AVAX/USD", "5m", TF_SECS, 0, 5000)
    assert len(df) == 400
    assert ex.calls < 50, "walked past the end instead of stopping on the empty page"


def test_no_infinite_loop_when_the_cursor_cannot_advance():
    """A stuck exchange returning the same bar must not hang the backtester."""
    class Stuck:
        calls = 0

        def fetch_ohlcv(self, *a, **k):
            Stuck.calls += 1
            return [[0, 1.0, 1.0, 1.0, 1.0, 1.0]]

    st = Stuck()
    df = bc.fetch_paginated(st, "X/USD", "5m", TF_SECS, 0, 1000)
    assert len(df) == 1
    assert Stuck.calls <= 2


def test_timestamps_are_unique_and_ordered():
    ex = FakeEx(start=1_700_000_000_000, n_total=900, first_page=280)
    df = bc.fetch_paginated(ex, "X/USD", "5m", TF_SECS, 1_700_000_000_000, 900)
    assert df["ts"].is_monotonic_increasing
    assert df["ts"].is_unique

"""Make the newest candle follow live price, because Coinbase's does not.

/products/{id}/candles is the HISTORIC-RATES endpoint. It serves completed buckets from
a cache and does not stream the bucket currently forming. Measured 2026-09-19, sampling
both endpoints every 5 seconds for 43 seconds while BTC moved $22:

       t  candle close   live ticker       gap
       0     81,250.15     81,251.29     -1.14
      22     81,250.15     81,229.16    +20.99
      43     81,250.15     81,250.23     -0.08

The candle close never moved once. chart_server polls that endpoint every 5 seconds, so
it re-fetched the same frozen bucket nine times — the chart only jumped when Coinbase's
own cache rolled, which is exactly the "slow and behind" the operator reported. The
server added no staleness of its own; a direct call measured identical. Polling harder
would not have helped, because the data being polled does not change.

What a live chart actually does is take completed candles from history and drive the
FORMING one from the live feed. That is all this is.

THE INVARIANT: this may only ever make the newest candle MORE current. It must never
rewrite a completed bucket, invent volume, or move a high or low inward — a chart that
edits history is worse than a chart that lags.
"""

# Coinbase row order, which is NOT OHLC: [time, low, high, open, close, volume]
_T, _LOW, _HIGH, _OPEN, _CLOSE, _VOL = range(6)


def _usable_price(price):
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if p <= 0 or p != p:            # non-positive, or NaN
        return None
    return p


def patch_live_candle(candles, price, now_ts, granularity):
    """Candles with the forming bucket brought up to `price`. Newest row first.

    Returns the input unchanged whenever it cannot do this honestly: no candles, an
    unusable price, a malformed newest row, or a clock that predates the newest bucket.
    Never mutates the list it was given — the caller may be serving it from a cache.
    """
    p = _usable_price(price)
    if p is None or not candles or not isinstance(candles, (list, tuple)):
        return candles

    newest = candles[0]
    try:
        bucket_start = int(newest[_T])
        low, high, open_ = float(newest[_LOW]), float(newest[_HIGH]), float(newest[_OPEN])
        vol = newest[_VOL]
        gran = int(granularity)
        now = float(now_ts)
    except (TypeError, ValueError, IndexError, KeyError):
        return candles
    if gran <= 0 or now < bucket_start:
        return candles

    rows = [list(r) if isinstance(r, list) else r for r in candles]

    if now < bucket_start + gran:
        # Still inside the newest bucket: update its close, and extend a wick only if
        # price has actually traded beyond what the cached bucket knows about.
        rows[0][_CLOSE] = p
        rows[0][_HIGH] = max(high, p)
        rows[0][_LOW] = min(low, p)
        rows[0][_OPEN] = open_          # the open is a fact
        rows[0][_VOL] = vol             # and the volume is not ours to invent
        return rows

    # The window is over and Coinbase has not published the next bucket yet. Open one at
    # the live price rather than leaving the chart frozen on a bucket whose time is up.
    # Exactly one — a long gap (a sleeping laptop) must not fabricate the buckets nobody
    # was there to observe.
    fresh_start = bucket_start + int((now - bucket_start) // gran) * gran
    return [[fresh_start, p, p, p, p, 0.0]] + rows

# bot/indicators.py
import math
from datetime import datetime, timezone

import pandas as pd


def should_resend_entry_after_error(broker_has_live_order, broker_has_position,
                                     verification_ok):
    """Decide whether it is safe to re-send an entry after the order POST raised.

    Pure so it can be tested without a broker. Returns True ONLY when the broker is
    PROVABLY clean — no live order, no open position, and the check itself succeeded.

    Fails CLOSED on purpose. A POST can raise after Alpaca has already accepted the
    order (read timeout, connection reset, unparseable body), and the old code treated
    every exception as "nothing happened" and fired a second market order. Real incident
    2026-08-14: two MSFT entries at the identical price $494.77, both closing near -3R.
    A missed entry costs one setup; a duplicate costs an unintended doubled position.
    """
    if not verification_ok:
        return False                      # could not check -> assume it landed
    return not (broker_has_live_order or broker_has_position)


def build_protective_leg(stop_price, is_crypto, is_long, buffer_pct=0.005):
    """Build the stop-loss leg of a bracket/OCO order. Equities accept a bare
    {"stop_price": ...} (a plain stop order) — unchanged, matches existing behavior.
    Crypto REJECTS that with a 422 "invalid order type for crypto order" (Alpaca requires
    stop_limit for crypto, confirmed live against a real BTC/USD position that had never
    had a stop-loss since its first fill because of exactly this) — so crypto legs also
    carry a limit_price a small buffer past the stop, in the direction that keeps it
    fillable as price continues moving past the trigger: below the stop when SELLING to
    close a long, above the stop when BUYING to cover a short."""
    leg = {"stop_price": str(stop_price)}
    if not is_crypto:
        return leg
    limit_price = stop_price * ((1 - buffer_pct) if is_long else (1 + buffer_pct))
    leg["limit_price"] = str(limit_price)
    return leg


# Re-exported so existing callers/tests can keep doing `from bot.indicators import
# should_refuse_duplicate_start` — the real implementation lives in
# bot/single_instance_lock.py, a zero-dependency (stdlib `os` only) module, because
# binance_bot.py needs to run this lock check BEFORE its lazy pandas/ccxt import
# finishes (a fresh-boot Gatekeeper scan can block that for ~16 minutes) and importing
# anything from this file — which imports pandas at module level — would defeat that.
from bot.single_instance_lock import should_refuse_duplicate_start, acquire_single_instance_lock  # noqa: E402,F401


def needs_eod_catchup_flatten(now, already_flattened_today, catchup_grace_min=45):
    """True if the pre-close EOD flatten window may have been MISSED (a single
    on_trading_iteration overrunning past the whole EOD_FLATTEN_MIN window jumps
    straight from 'not time yet' to 'market already closed', silently disabling that
    check for the rest of the day) and we're now within `catchup_grace_min` minutes
    AFTER the close, on a weekday, having not already flattened today. Independent of
    loop timing/drift — explicitly asks 'did today's flatten actually happen?' rather
    than relying on a narrow instant-in-time race."""
    if already_flattened_today or now.weekday() >= 5:
        return False
    close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now < close:
        return False
    mins_past = (now - close).total_seconds() / 60.0
    return mins_past <= catchup_grace_min


def is_leftover_position(entry_iso, now):
    """True if a still-open position was entered on an EARLIER calendar day than `now`
    (compared in `now`'s timezone) — a leftover from a prior session that an intraday
    bot must flatten at the next open rather than hold another whole day. This catches
    the Fri→Mon case is_regular_session can't: the bot restarts DURING Monday's session
    still holding Friday's position, which otherwise looks like a fresh mid-session entry.
    Missing/unparseable entry → False (can't prove it's stale, so never force-close)."""
    if not entry_iso:
        return False
    try:
        dt = datetime.fromisoformat(str(entry_iso).replace("Z", "+00:00"))
    except Exception:
        return False
    if dt.tzinfo is None:           # naive isoformat() output — treat as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(now.tzinfo).date() < now.date()


def detect_inducement(candles, zone_low, zone_high, current_price, is_long, lookback=30):
    """Inducement (IDM): the minor liquidity level sitting BETWEEN current price and the
    armed HTF zone — the stops that typically get swept to trap early entrants before
    price actually delivers into the zone.

    LONG setup (demand zone below price): the nearest minor swing LOW above zone_high and
    below current_price — price must run those stops on its way down into the zone.
    SHORT setup (supply zone above price): the nearest minor swing HIGH below zone_low and
    above current_price.

    "Nearest" = closest to the ZONE (the last level cleared before the zone tap), which is
    the one that actually front-runs the entry. Returns None when no zone is armed or no
    qualifying level exists. Display/context only — never gates an entry.

    candles: list of (open, high, low, close), oldest first."""
    if not candles or zone_low is None or zone_high is None:
        return None
    window = candles[-lookback:]
    if is_long:
        # strictly between the top of the demand zone and current price
        lows = [c[2] for c in window if zone_high < c[2] < current_price]
        return min(lows) if lows else None
    highs = [c[1] for c in window if current_price < c[1] < zone_low]
    return max(highs) if highs else None


def is_disabled_engine_zone(amd_phase, enable_trend_follow, enable_wedge_breakout):
    """True if a PENDING zone (state==ENTRY_WAIT, not yet filled) was armed by an engine
    that's currently gated off — meaning a startup reconcile should reset it to IDLE
    instead of letting it tap in. Catches the case a flag flip alone can't: an ARM-time
    gate only stops NEW zones from being set; a zone armed by an OLDER process run
    (before the flag was disabled) and restored verbatim from the state file on restart
    is engine-agnostic at fill time, so it can still silently execute post-freeze. AMD
    manipulation and BOS-chase zones are never gated by this — only the two engines the
    trader explicitly asked to disable."""
    if amd_phase == "trend_follow" and not enable_trend_follow:
        return True
    if amd_phase == "wedge_breakout" and not enable_wedge_breakout:
        return True
    return False


def is_regular_session(weekday, hour, minute):
    """True iff a US-equities regular session is active RIGHT NOW: Mon-Fri, 9:30am-4:00pm
    ET (caller passes already-ET-localized wall-clock components — weekday: 0=Mon..6=Sun).
    Used as a startup safety net: an intraday bot that restarts outside these hours while
    still holding a position missed its EOD flatten and must close immediately, not ride
    into the next session."""
    if weekday >= 5:
        return False
    minutes_since_midnight = hour * 60 + minute
    return 9 * 60 + 30 <= minutes_since_midnight < 16 * 60


def account_equity(balance, positions, entry_prices, margin_used, prices, leverage):
    """Mark-to-market equity for a paper margin account — what a real broker shows:
    free cash + for every open position its locked margin + unrealized P&L. `balance`
    alone is only FREE CASH (margin is deducted on open), so reporting it as portfolio
    value makes opening a position look like an instant loss. Used by BOTH the live
    header and the daily Macro snapshot so they can never disagree again."""
    eq = balance
    for sym, qty in positions.items():
        if abs(qty) < 1e-9:
            continue
        entry = entry_prices.get(sym, 0.0)
        cur = prices.get(sym, entry)
        upnl = (cur - entry) * qty if qty > 0 else (entry - cur) * abs(qty)
        margin = margin_used.get(sym, abs(qty) * entry / leverage)
        eq += margin + upnl
    return eq


def has_displacement(candles, is_long, min_body_frac=0.5, min_body_abs=0.0, lookback=3):
    """True if any of the last `lookback` candles is a real momentum (displacement) bar
    in the trade direction — a decisive body, not indecision. Gates the continuation
    setups (wedge/chase/BOS) so the bot stops entering weak breakouts into chop.

    candles: list of (open, high, low, close), oldest first.
    A bar qualifies when: body/range >= min_body_frac (decisive), body >= min_body_abs
    (not a tiny bar — pass ~0.6× ATR), and it closes in the trade direction."""
    for o, h, l, c in candles[-lookback:]:
        rng = h - l
        if rng <= 0:
            continue
        body = abs(c - o)
        if body / rng < min_body_frac or body < min_body_abs:
            continue
        if (c > o) if is_long else (c < o):
            return True
    return False


def blocked_by_overhead(entry, is_long, opposing_pool, min_room):
    """True if there isn't enough room to the nearest opposing liquidity pool — i.e. a
    long sitting right under a resistance pool (or a short right above a support pool),
    with no space to reach target. `opposing_pool` = nearest resistance (long) / support
    (short); None means none nearby (not blocked). A pool on the wrong side is ignored."""
    if opposing_pool is None:
        return False
    room = (opposing_pool - entry) if is_long else (entry - opposing_pool)
    if room <= 0:            # pool is behind us, not in the way
        return False
    return room < min_room


def match_last_round_trip(orders):
    """Pair the most recent closing fill with its true entry using ONLY the order
    sequence — never an external is_long/bias hint, which can go stale (e.g. leftover
    strategy_state.json from a much earlier, unrelated position) and splice together
    legs from two DIFFERENT real trades into one fabricated round trip.

    orders: Alpaca order dicts in any order (not required to be time-sorted). Filtered
    to status=='filled', then sorted by filled_at descending.
    exit  = the single most recent filled order (whatever side it is).
    entry = the nearest PRECEDING filled order with the OPPOSITE side — this is what
    stops two unrelated trips from merging: a same-side order in between is skipped, but
    the search never crosses past an opposite-side order into an earlier round trip's
    leg that isn't actually paired with this exit.
    Returns (entry_order, exit_order), or (None, None) if no round trip is found."""
    filled = sorted(
        (o for o in orders if o.get("status") == "filled" and o.get("filled_avg_price")),
        key=lambda o: o.get("filled_at") or "", reverse=True,
    )
    if len(filled) < 2:
        return None, None
    exit_o = filled[0]
    for cand in filled[1:]:
        if cand.get("side") != exit_o.get("side"):
            return cand, exit_o
    return None, None

# ============================================================================
# HIGHER TIMEFRAME (HTF) ANALYSIS - 4H Institutional Intent
# ============================================================================

def find_next_liquidity_target(df, price, bias, swing_bars=2):
    """
    Scans the 4H chart for the nearest swing high (bullish) or swing low (bearish)
    beyond current price. These are liquidity pools — where stops are clustered and
    where smart money drives price to collect them.
    A swing point requires its high/low to be the extreme in a ±swing_bars window.
    Returns the target price, or None if no clear pool exists beyond current price.
    """
    best = None
    for i in range(swing_bars, len(df) - swing_bars):
        if bias == "bullish":
            level = float(df.iloc[i]['high'])
            if level <= price:
                continue
            window_max = float(df.iloc[i - swing_bars: i + swing_bars + 1]['high'].max())
            if level == window_max:
                if best is None or level < best:   # nearest swing high above price
                    best = level
        else:
            level = float(df.iloc[i]['low'])
            if level >= price:
                continue
            window_min = float(df.iloc[i - swing_bars: i + swing_bars + 1]['low'].min())
            if level == window_min:
                if best is None or level > best:   # nearest swing low below price
                    best = level
    return best


def structural_stop_price(entry, swing, atr_ref, is_long, min_atr_mult, liq_cap_dist=None):
    """Stop PRICE anchored to real structure but kept OUTSIDE the noise.

    The stop distance is the WIDER of (a) the structural swing invalidation and (b) a
    noise floor of `min_atr_mult * atr_ref` (atr_ref = the higher-timeframe ATR). It is
    then capped at `liq_cap_dist` so a leveraged position can't stop past liquidation.
    Returns a price below entry (long) / above entry (short). This is what stops a $1
    micro-wick stop from sitting inside single-candle noise on a liquid stock."""
    struct_dist = abs(entry - swing) if swing else 0.0
    dist = max(struct_dist, min_atr_mult * atr_ref)
    if liq_cap_dist:
        dist = min(dist, liq_cap_dist)
    return entry - dist if is_long else entry + dist


def structural_take_profit(entry, stop_dist, pool, is_long, min_rr, max_rr):
    """Take-profit PRICE pinned to the nearest higher-timeframe liquidity pool / breaker.

    - Pool within [min_rr, max_rr] of risk  → TP sits ON the pool (target real structure).
    - Pool farther than max_rr              → capped at max_rr (kept reachable intraday).
    - Pool closer than min_rr (in the noise) or absent → default min_rr target.
    stop_dist is the positive entry→stop distance."""
    if pool:
        rr = (pool - entry) / stop_dist if is_long else (entry - pool) / stop_dist
        if rr >= min_rr:
            capped = min(rr, max_rr)
            return entry + capped * stop_dist if is_long else entry - capped * stop_dist
    return entry + min_rr * stop_dist if is_long else entry - min_rr * stop_dist


def crypto_zone_stop_level(fill_price, is_long, zone_edge, breathing_room, swing_extreme):
    """Combine the FVG/OB zone edge (+ ATR breathing room) with the structural
    swing-high/low guard into a single candidate stop LEVEL (a price) — the "swing"
    input later floored against higher-timeframe noise by structural_stop_price().

    Mirrors binance_bot.py's zone+swing stop logic (previously duplicated across its
    sniper-arm and retest code paths). The swing guard only ever WIDENS the stop
    (never tightens past the zone edge) — it protects against a manipulation wick
    sweeping the zone before real structure breaks. Falls back to a pure ATR offset
    from fill_price if the entry already sits through its own zone edge (a chase /
    momentum entry has no zone to reference)."""
    if is_long:
        zone_sl = zone_edge - breathing_room
        if zone_sl >= fill_price:                 # entry already through the zone
            zone_sl = fill_price - breathing_room
        return min(zone_sl, swing_extreme) if swing_extreme is not None else zone_sl
    else:
        zone_sl = zone_edge + breathing_room
        if zone_sl <= fill_price:
            zone_sl = fill_price + breathing_room
        return max(zone_sl, swing_extreme) if swing_extreme is not None else zone_sl


def find_swing_points(df, lookback=10):
    """
    Identifies the most recent swing high and swing low over a lookback period.
    Useful for finding key resistance (swing high) and support (swing low).
    """
    if len(df) < lookback:
        return None, None, None, None
    
    recent = df.tail(lookback)
    swing_high = recent['high'].max()
    swing_high_idx = recent['high'].idxmax()
    swing_low = recent['low'].min()
    swing_low_idx = recent['low'].idxmin()
    
    return swing_high, swing_high_idx, swing_low, swing_low_idx

def _finite(x, positive=True):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:                      # NaN
        return None
    if positive and v <= 0:
        return None
    return v


def zone_tapped(bars, zone_lo, zone_hi, lookback=2):
    """Did price trade into [zone_lo, zone_hi] during the last `lookback` bars?

    The entry test used to be `zone_lo <= last_price <= zone_hi` — the price at the
    instant of a 15-minute poll. A dip that begins and ends between two polls leaves no
    trace in it. On 2026-09-22 SPY sat inside its armed zone for 31 minutes and NVDA for
    1; neither was ever evaluated.

    A bar's [low, high] is the range price actually visited, so overlapping it with the
    zone asks the question the poll could not. lookback=2 because the bot polls every 15
    minutes on 15-minute bars: the newest bar may still be forming, so two of them always
    span the interval since the previous poll.

    This answers ONLY "did price get there". Whether it is still a fair place to fill is
    tap_chase_ok's job — see the ASTER incident quoted there.
    """
    lo = _finite(zone_lo)
    hi = _finite(zone_hi)
    if lo is None or hi is None or lo > hi:
        return False
    if bars is None:
        return False
    try:                                    # accept a DataFrame or a list of mappings
        rows = bars.tail(lookback).to_dict("records")
    except AttributeError:
        try:
            rows = list(bars)[-lookback:]
        except TypeError:
            return False
    for r in rows:
        try:
            b_lo = _finite(r["low"])
            b_hi = _finite(r["high"])
        except (TypeError, KeyError, IndexError):
            continue
        if b_lo is None or b_hi is None:
            continue
        if b_lo <= hi and b_hi >= lo:       # ranges overlap
            return True
    return False


def tap_chase_ok(price, zone_lo, zone_hi, atr, is_long, max_atr_mult=0.5):
    """(ok, why) — is `price` still a fair place to fill a tap of this zone?

    Detection widened to the bar's range, so by the time the bot looks, price may have
    left the zone. Everything downstream (stop, risk, target, R:R) is computed from the
    live price and the order is a MARKET order, so an unbounded "the bar touched it,
    buy now" reintroduces a bug already diagnosed on the crypto side:

        ASTER/USD LONG armed demand at 0.7190-0.7588 and filled at 0.7906 — 4.19% above
        the top of its own zone, on a price that never traded down to it.

    THE GUARD IS NOT SYMMETRIC, and that is the point:

      LONG, demand zone below — price back ABOVE the zone is the tap-and-reject working
        exactly as intended, so a bounded chase is allowed. Price BELOW the zone low
        means the demand has FAILED: the structural stop sits just under it and we would
        be buying into a live invalidation. Refused at any distance.

      SHORT, supply zone above — the mirror.

    A dead or unreadable ATR gives a chase budget of zero rather than an unlimited one,
    so a bad volatility read tightens this gate instead of opening it.
    """
    px = _finite(price)
    lo = _finite(zone_lo)
    hi = _finite(zone_hi)
    if px is None or lo is None or hi is None or lo > hi:
        return False, "unreadable price or zone"
    if lo <= px <= hi:
        return True, "in zone"

    if is_long:
        if px < lo:
            return False, (f"price {px:.4f} is BELOW the demand zone low {lo:.4f} — "
                           f"the zone failed, not a tap")
        gone = px - hi
        edge = hi
    else:
        if px > hi:
            return False, (f"price {px:.4f} is ABOVE the supply zone high {hi:.4f} — "
                           f"the zone failed, not a tap")
        gone = lo - px
        edge = lo

    _atr = _finite(atr)
    budget = (_atr * max_atr_mult) if _atr is not None else 0.0
    if gone <= budget:
        return True, f"chased {gone:.4f} past {edge:.4f} (budget {budget:.4f})"
    return False, (f"price ran {gone:.4f} past the zone edge {edge:.4f} — "
                   f"more than {max_atr_mult:g}x ATR ({budget:.4f}); the move left without us")


def nearest_sr(levels, price):
    """(nearest_above, nearest_below) — which levels are acting as R and S RIGHT NOW.

    find_support_resistance() sorts its two lists by TOUCH COUNT and never looks at
    current price, so `resistance[0]` is just "most-tested swing-high cluster in the
    window". After a rally that sits BELOW price, and the stock bot's status line
    printed it as `sr=res=` anyway. On 2026-09-22 that was every symbol but one:

        AAPL  px 342.33  res 335.73   -1.9%
        QQQ   px 746.85  res 718.86   -3.7%
        META  px 740.48  res 664.30  -10.3%

    A level below price is not resistance — price is already through it, which flips it
    to support. So membership of the highs-list or the lows-list does not decide the
    label; the side of price does. Both lists go in together and come back sorted by
    which side they are on.

    Returns (None, None) for an unusable price or no levels.
    """
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None, None
    if price != price or price <= 0 or not levels:
        return None, None
    vals = []
    for lv in levels:
        try:
            v = float(lv)
        except (TypeError, ValueError):
            continue
        if v == v and v > 0:
            vals.append(v)
    above = [v for v in vals if v > price]
    below = [v for v in vals if v < price]
    return (min(above) if above else None,
            max(below) if below else None)


def find_support_resistance(df, lookback=50, tolerance_pct=0.004, min_touches=2):
    """
    Finds significant S/R levels by clustering swing-point touches.
    Only levels hit at least min_touches times are returned — those are the ones
    institutions are actually watching. Lists are ordered most-tested first.
    Falls back to top single-touch swings if no cluster qualifies.
    """
    if len(df) < lookback:
        return [], []

    recent = df.tail(lookback)
    highs, lows = [], []
    for i in range(1, len(recent) - 1):
        h = float(recent.iloc[i]['high'])
        l = float(recent.iloc[i]['low'])
        if h >= float(recent.iloc[i-1]['high']) and h >= float(recent.iloc[i+1]['high']):
            highs.append(h)
        if l <= float(recent.iloc[i-1]['low']) and l <= float(recent.iloc[i+1]['low']):
            lows.append(l)

    def cluster(prices, mt):
        if not prices:
            return []
        prices = sorted(prices)
        groups = [[prices[0]]]
        for p in prices[1:]:
            if (p - groups[-1][-1]) / groups[-1][-1] <= tolerance_pct:
                groups[-1].append(p)
            else:
                groups.append([p])
        result = [(sum(g) / len(g), len(g)) for g in groups if len(g) >= mt]
        result.sort(key=lambda x: x[1], reverse=True)
        return [r[0] for r in result[:5]]

    res = cluster(highs, min_touches)
    sup = cluster(lows,  min_touches)
    # Fallback: if no multi-touch clusters, use top raw swing points
    if not res:
        res = sorted(highs, reverse=True)[:3]
    if not sup:
        sup = sorted(lows)[:3]
    return res, sup


def is_near_sr_level(price, levels, tolerance_pct=0.005):
    """
    Returns (True, nearest_level) if price is within tolerance_pct of any S/R level.
    Used to check confluence between an FVG/OB entry zone and a known multi-touch level.
    """
    for lvl in levels:
        if lvl and abs(price - lvl) / lvl <= tolerance_pct:
            return True, float(lvl)
    return False, None


def _detect_equal_wicks(df, side, lookback=40, tolerance_pct=0.0015,
                        min_touches=2, swing_window=2):
    """
    Core for equal-lows/equal-highs. Finds swing pivots on the given side and clusters
    those wicks within tolerance_pct. 'side' = 'low' or 'high'.
    Returns (found, level, touches) for the strongest pool (most touches; ties broken
    toward the nearest liquidity — lowest level for EQL, highest for EQH).
    """
    if df is None or len(df) < (2 * swing_window + 1):
        return False, None, 0
    recent = df.tail(lookback).reset_index(drop=True)
    n = len(recent)
    # Strict pivots only — a wick that genuinely sticks OUT below (above) its neighbours.
    # Strict '<' / '>' excludes flat consolidation bases (which aren't the equal-wick
    # liquidity shelves we're after) and keeps prominent swing wicks. We also record the
    # bar index so we can require clustered touches to be separated in time (real EQL/EQH,
    # not two adjacent candles of the same base).
    pivots = []   # (value, index)
    for i in range(swing_window, n - swing_window):
        val   = float(recent.iloc[i][side])
        others = [float(recent.iloc[j][side])
                  for j in range(i - swing_window, i + swing_window + 1) if j != i]
        is_pivot = all(val < o for o in others) if side == "low" else all(val > o for o in others)
        if is_pivot:
            pivots.append((val, i))
    if len(pivots) < min_touches:
        return False, None, 0

    best_level, best_touches = None, 0
    for base, _bi in pivots:
        if base <= 0:
            continue
        group = [(p, gi) for (p, gi) in pivots if abs(p - base) / base <= tolerance_pct]
        # require touches separated by ≥ swing_window bars (distinct, not adjacent)
        idxs = sorted(gi for _p, gi in group)
        distinct = 1
        last = idxs[0]
        for gi in idxs[1:]:
            if gi - last >= swing_window:
                distinct += 1
                last = gi
        cnt = distinct
        if cnt < min_touches:
            continue
        lvl = sum(p for p, _gi in group) / len(group)

        # Respected-level filter: a real EQH/EQL is liquidity price WICKED to but rarely
        # CLOSED beyond. If price has decisively closed THROUGH it more than once, the level
        # was swept/consumed (or it's just mid-range chop) — not a resting pool. This drops
        # the "lines that got blown clean through" that shouldn't have counted.
        if side == "high":
            breaks = int((recent['close'] > lvl * (1 + tolerance_pct)).sum())
        else:
            breaks = int((recent['close'] < lvl * (1 - tolerance_pct)).sum())
        if breaks > 1:
            continue

        better = cnt > best_touches
        tie    = cnt == best_touches and best_level is not None and (
            lvl < best_level if side == "low" else lvl > best_level
        )
        if better or tie:
            best_level, best_touches = lvl, cnt
    if best_level is not None:
        return True, float(best_level), best_touches
    return False, None, 0


def detect_equal_lows(df, lookback=40, tolerance_pct=0.0015, min_touches=2):
    """
    Equal Lows (EQL): two+ swing-low wicks at ~the same price. In SMC this is a
    SELL-SIDE liquidity pool — stops rest just below it, making it both a support
    shelf and a prime sweep target. Returns (found, level, touches).
    """
    return _detect_equal_wicks(df, "low", lookback, tolerance_pct, min_touches)


def detect_equal_highs(df, lookback=40, tolerance_pct=0.0015, min_touches=2):
    """
    Equal Highs (EQH): two+ swing-high wicks at ~the same price — a BUY-SIDE liquidity
    pool. Stops rest just above it; it's resistance and a prime sweep target.
    Returns (found, level, touches).
    """
    return _detect_equal_wicks(df, "high", lookback, tolerance_pct, min_touches)


def detect_trendline(df, lookback=50, swing_window=4, min_points=3):
    """
    Detects the DOMINANT diagonal trendline so it can be drawn on the chart:
      • ascending  — the longest run of strictly higher swing LOWS  → rising support
      • descending — the longest run of strictly lower  swing HIGHS → falling resistance
    Diagonal structure the horizontal EQH/EQL detector can't see.

    swing_window=4 means a candle must be more extreme than the 4 candles on either
    side of it to count as a swing point — wide enough that small intra-move noise
    doesn't get flagged as its own swing, so only genuine structural pivots qualify.

    Rather than only checking the most recent min_points swings, this scans the whole
    lookback for the longest monotonic run in each direction and returns whichever run
    covers more swing points (ties broken by price range covered) — so a big multi-hour
    rally/selloff wins over a small recent pullback that happens to sit at the tail end.

    Returns (found, kind, (t1, p1), (t2, p2)) — anchors as (unix_seconds, price) for the
    first and last swing on the winning line. (False, None, None, None) if no clean trendline.
    """
    if df is None or len(df) < (2 * swing_window + 1):
        return False, None, None, None
    recent = df.tail(lookback)
    idx = recent.index
    n = len(recent)
    lows, highs = [], []   # (unix_seconds, price)
    for i in range(swing_window, n - swing_window):
        lo = float(recent['low'].iloc[i]);  hi = float(recent['high'].iloc[i])
        others_lo = [float(recent['low'].iloc[j])  for j in range(i - swing_window, i + swing_window + 1) if j != i]
        others_hi = [float(recent['high'].iloc[j]) for j in range(i - swing_window, i + swing_window + 1) if j != i]
        t = int(idx[i].timestamp())
        if lo < min(others_lo):
            lows.append((t, lo))
        if hi > max(others_hi):
            highs.append((t, hi))

    def longest_monotonic_run(points, ascending):
        """Longest contiguous run of strictly increasing (ascending) or decreasing
        (descending) points, at least min_points long. Returns (run, price_range)."""
        best_run, best_range = None, 0.0
        if not points:
            return best_run, best_range
        current = [points[0]]
        for k in range(1, len(points)):
            keeps_direction = (points[k][1] > points[k - 1][1]) if ascending \
                          else (points[k][1] < points[k - 1][1])
            current = current + [points[k]] if keeps_direction else [points[k]]
            if len(current) >= min_points:
                price_range = abs(current[-1][1] - current[0][1])
                if best_run is None or len(current) > len(best_run) or \
                   (len(current) == len(best_run) and price_range > best_range):
                    best_run, best_range = current, price_range
        return best_run, best_range

    asc_run,  asc_range  = longest_monotonic_run(lows,  ascending=True)
    desc_run, desc_range = longest_monotonic_run(highs, ascending=False)

    if asc_run and desc_run:
        # More swing points covered wins; a tie is broken by which move spans more price.
        if len(asc_run) > len(desc_run) or (len(asc_run) == len(desc_run) and asc_range >= desc_range):
            return True, 'ascending', asc_run[0], asc_run[-1]
        return True, 'descending', desc_run[0], desc_run[-1]
    if asc_run:
        return True, 'ascending', asc_run[0], asc_run[-1]
    if desc_run:
        return True, 'descending', desc_run[0], desc_run[-1]
    return False, None, None, None

def detect_consolidation(df, lookback=20):
    """
    Detects if the market is in a consolidation (ranging) phase.
    Returns True if price is bouncing between two levels with low volatility.
    """
    if len(df) < lookback:
        return False
    
    recent = df.tail(lookback)
    range_high = recent['high'].max()
    range_low = recent['low'].min()
    range_size = range_high - range_low
    
    # If range is small relative to the highs, it's consolidating
    consolidation_ratio = range_size / range_high
    volatility = recent['close'].pct_change().std()
    
    is_consolidating = consolidation_ratio < 0.02 and volatility < 0.01
    return is_consolidating

def get_daily_trend(df_daily):
    """
    Returns 'bullish', 'bearish', or None (choppy/unclear).
    Requires BOTH conditions to agree before calling a trend:
      - Price is above/below the daily EMA50
      - Daily EMA20 is above/below the daily EMA50
    A 4H BOS against the daily trend is just a pullback — skip it.
    """
    if len(df_daily) < 52:
        return None
    close   = df_daily['close']
    ema20   = close.ewm(span=20, adjust=False).mean()
    ema50   = close.ewm(span=50, adjust=False).mean()
    price   = float(close.iloc[-1])
    e20_now = float(ema20.iloc[-1])
    e50_now = float(ema50.iloc[-1])

    if price > e50_now and e20_now > e50_now:
        return "bullish"
    if price < e50_now and e20_now < e50_now:
        return "bearish"
    return None   # mixed / choppy — no trade


def detect_displacement_bos(df, lookback=15):
    """
    Detects a FRESH institutional Break of Structure.
    Requirements (all three must pass):
      1. One of the last 3 bars closed above/below the prior structure level
      2. That candle body is >50% of its range (real momentum, not a doji)
      3. The close cleared the structure level by at least 0.15% (filters false breaks)
    """
    if len(df) < lookback:
        return False, None, None

    recent    = df.tail(lookback)
    structure = recent.iloc[:-3]
    if len(structure) < 5:
        return False, None, None

    swing_high = structure['high'].max()
    swing_low  = structure['low'].min()

    for i in range(-3, 0):
        candle = recent.iloc[i]
        body   = abs(candle['close'] - candle['open'])
        rng    = candle['high'] - candle['low']
        if rng == 0 or body / rng < 0.40:   # 40% body — real conviction without over-filtering
            continue
        if candle['close'] > swing_high * 1.0015:   # 0.15% clearance filters 1-tick false breaks
            return True, "bullish", float(swing_high)
        if candle['close'] < swing_low  * 0.9985:
            return True, "bearish", float(swing_low)

    return False, None, None


def range_atr(df, period=14):
    """Mean high-low range over `period` bars — the same inline calc both bots already
    repeat a dozen times. Returns 0.0 when it cannot be computed (short/NaN series), which
    callers can pass straight to min_body_abs as "no floor" rather than crashing."""
    try:
        v = float((df["high"] - df["low"]).rolling(period).mean().iloc[-1])
        return v if v == v and v > 0 else 0.0      # v == v filters NaN
    except Exception:
        return 0.0


def displacement_min_body(atr, price, atr_mult=1.8, min_pct=0.0015):
    """The absolute body a candle must clear to be called a displacement.

    Returns max(atr_mult * atr, min_pct * price). Both terms are load-bearing:

    • The ATR term is what makes "big" mean big FOR THIS MARKET. It must be > 1.0x,
      or the gate selects an AVERAGE bar. The old multiplier was 0.6 — i.e. "60% of a
      typical candle" — and on POL/USD 2026-09-04 a bar measuring 1.07x ATR sailed
      through it. Displacement has to mean outlier.

    • The percentage term is the backstop for a flat tape. When ATR collapses the ATR
      term collapses with it, so on a market barely moving, even 1.8x ATR can be an
      invisible bar. POL's 5m ATR was 0.169% of price at the time.

    Deliberately NOT scaled off the higher timeframe: the 6H ATR is what decides
    whether a market is tradeable at all (ATR_GATE), and reusing it here would let a
    market that is volatile daily but dead right now keep arming zones — which is the
    exact mismatch that produced the POL entry.
    """
    try:
        atr = float(atr) if atr and atr == atr else 0.0
        price = float(price) if price and price == price else 0.0
    except (TypeError, ValueError):
        return 0.0
    return max(atr_mult * max(atr, 0.0), min_pct * max(price, 0.0))


def momentum_expanding(bars, is_long, min_body_abs=0.0, lookback=3, decay_tol=0.05):
    """True while a move is still ACCELERATING — every bar pushing the trade's way, each
    body at least as large as the one before, and the latest clearing `min_body_abs`.

    Written for the breakout chase, whose whole momentum test used to be:

        momentum_intact = close[-1] > close[-3]

    Two bars of net drift. A run of SHRINKING green candles satisfies that perfectly,
    which is what the operator watched happen on ASTER — "the green candles after that
    showed slow momentum that kept getting smaller". A chase enters at MARKET with no
    zone underneath it, so "price is a bit higher than it was" is not enough: the only
    thing justifying a chase is that the move is still going, and a decaying sequence is
    the move being distributed into, with the chaser as exit liquidity.

    Equal bodies count as sustained — only shrinking disqualifies. `decay_tol` (5%) is
    what makes that workable on real data: consecutive equal-looking bodies differ by
    float dust (1.15-1.10 computes SMALLER than 1.10-1.05), so a strict non-decreasing
    test rejects a perfectly steady move. It also stops a 1% wobble reading as decay,
    while the decay this exists to catch — ASTER's roughly halving bodies — is nowhere
    near the threshold.

    Fails CLOSED on short or malformed input: acceleration that cannot be verified must
    not become a free pass on the one path with no zone protecting it.
    """
    try:
        if not bars or len(bars) < lookback:
            return False
        window = list(bars)[-lookback:]
        bodies = []
        for b in window:
            o, c = float(b["open"]), float(b["close"])
            if (c <= o) if is_long else (c >= o):
                return False                      # a bar against the move = stall
            bodies.append(abs(c - o))
    except (TypeError, ValueError, KeyError, IndexError):
        return False
    if bodies[-1] < min_body_abs:
        return False
    return all(bodies[i] >= bodies[i - 1] * (1.0 - decay_tol) for i in range(1, len(bodies)))


def entry_respects_zone(price, zone_lo, zone_hi, is_long, is_chase, tol_pct=0.005):
    """Last-line check that a retest entry is filling inside the zone it armed.

    Returns (ok, explanation). The explanation is meant to be printed on refusal — it
    names how far outside the fill was, which is the detail needed to identify which
    entry path misbehaved.

    REAL INCIDENT (ASTER/USD LONG, 2026-09-05): the AMD engine armed demand at
    0.7190-0.7588 and the bot filled at 0.7906 — 4.19% above the top of its own zone,
    on a price that never traded down to the zone at all.

    This duplicates price_in_entry_zone's directionality on purpose. That check runs at
    TAP time; this one runs at EXECUTION, after the AI confirmation call, which can take
    up to 25 seconds and during which `price` is never re-read. Every entry path funnels
    through execute_confirmed_entry, so the invariant holds here no matter which engine
    armed the zone.

    `tol_pct` (0.5%) is deliberately looser than the tap check's 0.15%: a few ticks of
    drift between tap and fill is ordinary, a 4% miss is a defect. Overshooting the far
    side is always allowed — deeper into demand is a better long, deeper into supply a
    better short.

    Fails OPEN when no zone is set: some engines legitimately carry none, and vetoing
    those would quietly switch the bot off.
    """
    if is_chase:
        return True, "chase entry — outside the zone by design"
    try:
        price = float(price)
        if zone_lo is None or zone_hi is None:
            return True, "no zone set — guard skipped"
        zone_lo, zone_hi = float(zone_lo), float(zone_hi)
    except (TypeError, ValueError):
        return True, "zone unreadable — guard skipped"
    if zone_lo <= 0 or zone_hi <= 0 or zone_hi < zone_lo:
        return True, "degenerate zone — guard skipped"

    tol = abs(price) * tol_pct
    if is_long:
        if price > zone_hi + tol:
            return False, (f"fill ${price:,.6g} is {(price - zone_hi) / zone_hi * 100:.2f}% "
                           f"ABOVE the armed zone ${zone_lo:,.6g}-${zone_hi:,.6g}")
        return True, "inside zone"
    if price < zone_lo - tol:
        return False, (f"fill ${price:,.6g} is {(zone_lo - price) / zone_lo * 100:.2f}% "
                       f"BELOW the armed zone ${zone_lo:,.6g}-${zone_hi:,.6g}")
    return True, "inside zone"


def normalize_exit_reason(raw, breakeven_moved=False):
    """Collapse a close's human label into one groupable token.

    Returns one of: TARGET, STOP, BREAKEVEN, STALE, OTHER.

    The ledger's existing "Reason" column records the strategy that OPENED a trade
    ("BOS LONG"), never why it closed — so a run of near-zero rows could not be read
    without replaying candles.

    THE BREAK-EVEN CASE IS WHY THIS TAKES A SECOND ARGUMENT. There is no separate
    break-even exit path: manage_open_trade trails `stop_loss` up at +1.25R and the
    position then closes through the ordinary stop branch, labelled "SL hit". The string
    is identical to a real stop-out; only `state.breakeven_moved` separates them. On 121
    real trades BREAKEVEN was -$228.35 — the single most addressable line item — and
    folding it into STOP hides it completely.

    Order matters: a winner that trailed to break-even and then ran to target is a
    TARGET, and one that trailed and then timed out exited on the CLOCK, not the stop.
    So TARGET and STALE are both checked before the stop branch consults the flag.

    Never raises — it runs after the position is already closed.
    """
    try:
        s = str(raw or "").upper()
    except Exception:
        return "OTHER"
    if "TP" in s or "TAKE PROFIT" in s or "TARGET" in s:
        return "TARGET"
    if "STALE" in s or "TIMEOUT" in s:
        return "STALE"
    if "SL" in s or "STOP" in s:
        return "BREAKEVEN" if breakeven_moved else "STOP"
    return "OTHER"


def infer_exit_reason(exit_price, stop_loss, take_profit, breakeven_moved=False,
                      tolerance=0.002):
    """Exit reason for a close the bot did not itself trigger — read from the fill.

    The crypto bot decides its own exits and hands normalize_exit_reason a label. The
    stock bot's exits mostly fire at the BROKER (bracket OCO legs) and are discovered
    afterwards by reconciliation, so there is no label to read — only the price the
    position actually closed at. Whichever protective level the fill landed nearer is
    the leg that filled, which is how you would read it off a chart by hand.

    `tolerance` is a fraction of the stop-to-target span: a fill more than this far from
    BOTH levels was neither leg (a manual flatten, an EOD close, a stale exit) and comes
    back OTHER rather than being forced into a bucket it doesn't belong in. Guessing here
    would poison exactly the grouping this column exists to enable.

    Returns TARGET / STOP / BREAKEVEN / OTHER. Never raises.
    """
    try:
        exit_price = float(exit_price); stop_loss = float(stop_loss)
        take_profit = float(take_profit)
    except (TypeError, ValueError):
        return "OTHER"
    span = abs(take_profit - stop_loss)
    if span <= 0:
        return "OTHER"
    d_tp, d_sl = abs(exit_price - take_profit), abs(exit_price - stop_loss)
    near = tolerance * span
    if d_tp > near and d_sl > near:
        return "OTHER"
    if d_tp <= d_sl:
        return "TARGET"
    return "BREAKEVEN" if breakeven_moved else "STOP"


def maker_limit_fill(limit_price, bar_low, bar_high, is_long, require_through=True):
    """Fill price for a RESTING limit order against one bar, or None if it did not fill.

    Earning the maker rate is not an accounting choice — it is an execution one. A maker
    order rests on the book and waits for someone to cross to it. Relabelling a market
    order's fee as "maker" while still filling at whatever price printed would make the
    simulation describe a cost nobody paid, which is the same self-flattery as reporting
    P&L gross.

    So the fill model changes with the fee: the order sits at `limit_price` and fills THERE
    — but only if the bar actually traded to it.

    `require_through` (default True) demands strict penetration rather than a touch. A
    resting order at a level price merely kisses is behind a queue of orders already
    posted there and usually does NOT fill. Requiring penetration under-fills slightly,
    which is the correct direction for a simulation to err.
    """
    try:
        limit_price = float(limit_price)
        bar_low, bar_high = float(bar_low), float(bar_high)
    except (TypeError, ValueError):
        return None
    if bar_high < bar_low:
        return None
    if is_long:
        reached = bar_low < limit_price if require_through else bar_low <= limit_price
    else:
        reached = bar_high > limit_price if require_through else bar_high >= limit_price
    return limit_price if reached else None


def round_trip_fee(entry_price, exit_price, qty, fee_rate, exit_fee_rate=None):
    """What the exchange bills for opening AND closing `qty` — charged on NOTIONAL.

    Each leg is priced at its own fill, because a big winner's exit leg costs more than
    its entry leg. At 10x leverage this is the difference between a green row and a red
    one: a $1,360 notional round trip at 0.25%/side costs $6.80, against trades whose
    whole edge is a few dollars.

    Exists because all three close paths reported the raw price difference as P&L while
    _charge_fee quietly moved the balance by net — so the ledger and the account
    disagreed on every single row. Returns 0.0 on bad input rather than raising: a close
    path runs after the position is already gone, and must not throw.

    `exit_fee_rate` exists because the two legs are not always the same kind of order. A
    resting entry and a resting take-profit are MAKER fills; a stop-loss triggers and
    crosses the book, so it is a TAKER fill no matter how the entry was placed. Charging
    one blended rate across both understates losers and overstates winners.

    NOTE on scale-outs: this prices the qty closed on THIS leg. A position that scaled
    out earlier paid its entry fee on the full original size, so a partially-closed
    trade is still slightly under-charged here. The scale-out leg's own fee belongs with
    banked_pnl; that path is not yet netted.
    """
    try:
        q = abs(float(qty))
        rate_in = float(fee_rate)
        rate_out = float(exit_fee_rate) if exit_fee_rate is not None else rate_in
        return q * abs(float(entry_price)) * rate_in + q * abs(float(exit_price)) * rate_out
    except (TypeError, ValueError):
        return 0.0


def displacement_gates(df, atr_mult=1.8, min_pct=0.0015, gap_pct=0.0015,
                       htf_atr=None, htf_mult=1.0):
    """Both size floors for detect_displacement_fvg, derived from one dataframe.

    Splat it at the call site — `**indicators.displacement_gates(df)` — so the crypto
    and stock bots cannot drift apart on how "big enough" is defined. They already did
    once: the arming path used a bare ATR fraction while the tap-time confirmation used
    a different one, so a zone could be armed by a bar the confirmation would reject.

    Returns {} when the frame has no usable close, which leaves detect_displacement_fvg
    on its permissive defaults rather than crashing the trading loop.
    """
    try:
        price = float(df["close"].iloc[-1])
        if price != price or price <= 0:
            return {}
    except Exception:
        return {}
    _floor = displacement_min_body(range_atr(df), price, atr_mult, min_pct)
    # THIRD TERM: tie the displacement to the RISK, not just to a quiet 5m tape.
    # The 5m ATR is measured over the same chop the gate is meant to exclude, so when the
    # tape goes quiet the floor sinks with it. ASTER 2026-09-24 cleared 1.8x by ONE
    # PERCENT (1.82x) on a bar worth 0.313% of price, against a trade risking 1.57%.
    # The stop is floored off the 1H ATR and measures 1.3-1.6x it on real trades, so the
    # operator's rule — "the candle should be 3/4 of the entry-to-stop box" — becomes
    # body >= 0.75 x (1.3 to 1.6) x atr_1h, i.e. 0.98x to 1.21x atr_1h. Default 1.0 sits at
    # the bottom of that band: exactly-3/4 passes at a typical stop (1.45x atr_1h -> 1.09x)
    # and is marginally strict at the tightest observed one (1.30x -> 0.98x). Rounder than
    # the fitted number and it states plainly: the displacement must be worth at least one
    # 1H candle's average range. Asset-neutral, built from the same quantity as the stop.
    # An unusable 1H read is IGNORED rather than treated as zero: a bad ATR must never
    # become "no floor at all".
    try:
        _h = float(htf_atr)
        if _h == _h and _h > 0:
            _floor = max(_floor, float(htf_mult) * _h)
    except (TypeError, ValueError):
        pass
    return {
        "min_body_abs": _floor,
        "min_gap_abs":  gap_pct * price,
    }


def reachable_target(entry, stop, target, htf_atr, max_atr_mult=1.5, min_rr=2.0):
    """Trim a target the hold window cannot possibly deliver, then re-check the R:R.

    Positions are force-closed at STALE_TRADE_HOURS (6h) — one HTF candle. A target
    further than `max_atr_mult` x the HTF ATR therefore needs several average HTF
    candles of travel inside the span of one, and in practice only ever resolves as a
    stale-timeout. It still prints a flattering R:R on the way in, which is worse than
    useless: it makes the least achievable setups look like the best ones.

    Real case: POL/USD LONG, entry 0.09465, target 0.10721 (13.3% away) against a
    2.755% 6H ATR — reported 1:9.4, exited on the timer. The trade BEFORE it carried
    the identical 0.10721 target and did the same thing for -$12.08.

    Clamps rather than vetoes, because the setup itself may be fine — it is the target
    that was fantasy. Returns (target, rr, ok); ok is False when the honest target can
    no longer pay `min_rr` against the stop, or the stop distance is degenerate.

    Fails OPEN on a missing/zero htf_atr: a data hiccup must not veto every setup, and
    the displacement and gap-width gates still carry the size requirement.
    """
    try:
        entry, stop, target = float(entry), float(stop), float(target)
        htf_atr = float(htf_atr) if htf_atr and htf_atr == htf_atr else 0.0
    except (TypeError, ValueError):
        return target, 0.0, False

    risk = abs(entry - stop)
    if risk <= 0:
        return target, 0.0, False
    if htf_atr <= 0:                      # unknown volatility -> leave it alone
        return target, abs(target - entry) / risk, True

    reach = max_atr_mult * htf_atr
    if target >= entry:                                   # long
        capped = min(target, entry + reach)
    else:                                                 # short
        capped = max(target, entry - reach)

    rr = abs(capped - entry) / risk
    return capped, rr, rr >= min_rr


def detect_displacement_fvg(df, lookback=20, window=10, min_body_pct=0.40,
                            clearance=0.0015, min_body_abs=0.0, min_gap_abs=0.0, require_c3_direction=True):
    """
    Finds the FVG left behind by the displacement that broke structure — the
    'CHoCH FVG'. This is the heart of the sweep → displacement → retest model:
    after a sweep, price reverses hard and breaks structure; that aggressive move
    leaves a 3-candle imbalance. The retest of THAT gap is the entry, and the gap
    itself IS the change of character — no separate CHoCH signal needed.

    A displacement candle (the middle of the 3) must:
      • have a body >= min_body_pct of its range (real momentum)
      • have a body >= min_body_abs in absolute price (not a tiny bar). Pass
        displacement_min_body(atr, price) — do NOT pass a bare fraction of ATR below
        1.0x, which selects an AVERAGE candle rather than an outlier. That was the
        original defect: 0.6x ATR let a 1.07x-ATR bar arm POL/USD on 2026-09-04.
        Without this the arming gate has NO size requirement at all, while the
        tap-time gate (has_displacement) demands its own floor — so a zone could be
        armed by a bar the confirmation step would reject.
      • leave a gap at least min_gap_abs wide. A one-tick gap is a line, not a zone;
        price grazes it on any tick and the "retest" carries no information.
      • close beyond the prior swing by `clearance` (genuine structure break)
      • leave a gap: prior.high < next.low (bullish) / prior.low > next.high (bearish)

    The gap is defined by the FIRST and THIRD candles only. The displacement candle in
    the middle necessarily trades through that range — a candle spans its own low to its
    own high — and that traversal is exactly what creates the imbalance. The gap stays
    "unfilled" until price RETURNS to it on a later bar.

    Scans the most recent `window` candles (newest first) and needs at least one
    candle AFTER the displacement so the gap is fully formed and retest-ready.

    Returns (found, direction, fvg_low, fvg_high, broken_level).
    """
    if len(df) < lookback:
        return False, None, None, None, None

    recent = df.tail(lookback).reset_index(drop=True)
    n = len(recent)
    oldest = max(2, n - window)

    for i in range(n - 2, oldest - 1, -1):          # newest displacement first, needs i+1
        disp  = recent.iloc[i]
        body  = abs(disp['close'] - disp['open'])
        rng   = disp['high'] - disp['low']
        # min_body_pct is SCALE-FREE: "body is 40% of its own range" scores a $0.02 candle
        # in dead chop identically to a $2,000 one. min_body_abs adds the absolute floor
        # (pass ~0.6x ATR) so an armed zone requires a bar that is actually identifiable.
        # Defaults to 0.0 = no floor, so untouched callers keep their old behaviour.
        if rng == 0 or body / rng < min_body_pct or body < min_body_abs:
            continue

        prior  = recent.iloc[i - 1]
        nxt    = recent.iloc[i + 1]
        struct = recent.iloc[:i]
        if len(struct) < 3:
            continue
        swing_high = float(struct['high'].max())
        swing_low  = float(struct['low'].min())

        # Bullish displacement: strong up-close above structure leaving C1.high < C3.low.
        # NOTE: do NOT add a "disp.low >= prior.high" style check here. That was tried on
        # 2026-08-07 and reverted on 2026-08-19: the displacement candle ALWAYS trades
        # through its own gap (one candle spans its low to its high), and that traversal
        # is what creates the imbalance. Requiring otherwise demands a true price gap
        # between consecutive candles, which effectively never happens in 24/7 crypto —
        # it silently disabled detection, rejecting BTC's real +5.75% 6h displacement on
        # 2026-08-19 ($3,521 gap) because C2's low sat $72 under C1's high.
        # See tests/test_displacement_fvg.py, which pins those real candles.
        # CANDLE 3 MUST CLOSE WITH THE DISPLACEMENT. The detector read exactly one
        # number off c3 — its low — so a gap armed even when c3 closed hard against the
        # move. DOGE 2026-09-24: c2 was +1.89x ATR up, c3 opened 0.09597 and closed
        # 0.09592 (red), the zone armed, and price collapsed through it. Measured over
        # 49 armed setups on 13 symbols, outcome = 2R before 1R within 24 bars:
        #     c3 agrees   n=19  win 89%
        #     c3 opposes  n=24  win 54%
        # and 55% of what the bot armed was in the losing bucket. A doji counts as
        # opposing: close == open is not "in the direction of" anything.
        _c3_up   = float(nxt['close']) > float(nxt['open'])
        _c3_down = float(nxt['close']) < float(nxt['open'])
        if (disp['close'] > disp['open']
                and (_c3_up or not require_c3_direction)
                and disp['close'] > swing_high * (1 + clearance)
                and float(prior['high']) < float(nxt['low'])):
            lo, hi = float(prior['high']), float(nxt['low'])
            # A gap narrower than min_gap_abs is a LINE, not a zone: price "taps" it on
            # any tick and the retest carries no information. POL/USD armed on a gap of
            # 0.00001 (0.011% of price) on 2026-09-04. `continue` rather than bail —
            # an older displacement in the window may have left a real one.
            if hi - lo < min_gap_abs:
                continue
            return True, 'bullish', lo, hi, swing_high

        # Bearish displacement: mirror — C1.low > C3.high.
        if (disp['close'] < disp['open']
                and (_c3_down or not require_c3_direction)
                and disp['close'] < swing_low * (1 - clearance)
                and float(prior['low']) > float(nxt['high'])):
            lo, hi = float(nxt['high']), float(prior['low'])
            if hi - lo < min_gap_abs:
                continue
            return True, 'bearish', lo, hi, swing_low

    return False, None, None, None, None

def detect_order_block(df, lookback=15):
    """
    Detects Order Blocks - zones where institutional money absorbed volume.
    These are identified as areas where price rejected and reversed.
    """
    if len(df) < lookback:
        return None, None
    
    recent = df.tail(lookback)
    
    # Find the zone of the last significant reversal
    # Order block is typically the candle that rejected price
    for i in range(len(recent) - 2, 0, -1):
        if recent.iloc[i]['close'] < recent.iloc[i]['open']:  # Bearish candle
            if recent.iloc[i + 1]['close'] > recent.iloc[i]['open']:  # Bullish reversal after
                order_block_high = recent.iloc[i]['high']
                order_block_low = recent.iloc[i]['low']
                return order_block_high, order_block_low
    
    return None, None

# ============================================================================
# LOWER TIMEFRAME (LTF) EXECUTION - 15m/5m Entry & Exit
# ============================================================================

def check_liquidity_sweep(df, sweep_window=3):
    """
    Checks the last sweep_window candles for a liquidity sweep:
    wick pierced the prior 20-candle support but closed back above it.
    Checking recent candles (not just the latest) prevents missing a sweep
    that occurred one or two iterations ago.
    """
    if len(df) < 22:
        return False, None, None

    for i in range(-sweep_window, 0):
        candle = df.iloc[i]
        lookback_start = i - 20 if i - 20 >= -len(df) else -len(df)
        prior = df.iloc[lookback_start:i]
        if len(prior) == 0:
            continue
        local_support = prior['low'].min()
        if candle['low'] < local_support and candle['close'] > local_support:
            return True, local_support, candle['low']

    return False, None, None


def check_liquidity_sweep_high(df, sweep_window=3):
    """
    Sell-side mirror of check_liquidity_sweep: wick pierced the prior 20-candle
    resistance (high) but closed back BELOW it. This is a sweep of BUY-side
    liquidity above — the classic stop-hunt before a move DOWN (short setup),
    or the manipulation_down leg before a LONG from demand when daily is bullish.
    Returns (found, local_resistance, sweep_high_wick).
    """
    if len(df) < 22:
        return False, None, None

    for i in range(-sweep_window, 0):
        candle = df.iloc[i]
        lookback_start = i - 20 if i - 20 >= -len(df) else -len(df)
        prior = df.iloc[lookback_start:i]
        if len(prior) == 0:
            continue
        local_resistance = prior['high'].max()
        if candle['high'] > local_resistance and candle['close'] < local_resistance:
            return True, local_resistance, candle['high']

    return False, None, None

def check_market_structure_shift(df):
    """
    Step 3 Math: Checks if the latest momentum candle broke cleanly 
    above the highest swing high. This is the "Rocket" - institutional buying
    that breaks through the recent lower high with aggressive close.
    """
    if len(df) < 11:
        return False, None
        
    latest_candle = df.iloc[-1]
    recent_candles = df.iloc[-10:-1]
    recent_swing_high = recent_candles['high'].max()
    
    # Aggressive institutional close ABOVE the swing high confirms the shift
    is_mss = latest_candle['close'] > recent_swing_high
    
    return is_mss, recent_swing_high

def find_bullish_fvg(df, lookback=15, min_body_abs=0.0, min_gap_abs=0.0):
    """
    Scans the last `lookback` bars for a bullish imbalance zone.
    Primary: true FVG (c1.low > c3.high — literal gap between wicks).
    Fallback: bullish order block (last bearish candle before a strong up move)
              which is the zone pros actually trade on intraday stock charts.
    Returns (found, bottom, top) where bottom < top is the entry zone.

    SIZE GATES ADDED 2026-09-08, and they matter more than they look. This is the
    FALLBACK arm in binance_bot's STEP 2 — reached only when detect_displacement_fvg
    finds nothing. When the displacement path was tightened on 2026-09-04 (1.8x ATR body,
    0.15% minimum gap), traffic did not stop: it REROUTED here, which had no size
    requirement whatsoever. Six of six setups in the following log armed through this
    path. Tightening the strict branch without tightening the fallback just moves the
    problem, and the operator spotted the result by eye: "there isn't even a single
    3 candle pattern, nor an FVG bro".

    So the same discipline applies here: the middle candle of a true FVG must clear
    min_body_abs, the gap must clear min_gap_abs, and an order block's candle must clear
    min_body_abs too. Both default to 0.0, so untouched callers keep prior behaviour.
    """
    if len(df) < 3:
        return False, None, None

    end   = len(df) - 1
    start = max(2, end - lookback)

    # 1. True FVG (most precise — common on crypto/overnight gaps)
    #    Bullish FVG = price gapped UP: c1.high < c3.low, middle candle bullish.
    #    Zone is c1.high (bottom) → c3.low (top), the unfilled imbalance below price.
    for i in range(end, start - 1, -1):
        c1, c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
        if c1['high'] < c3['low'] and c2['close'] > c2['open']:
            # the middle candle IS the displacement — "much bigger than average", not
            # merely green. Without this, any green bar of any size qualified.
            if abs(float(c2['close']) - float(c2['open'])) < min_body_abs:
                continue
            if float(c3['low']) - float(c1['high']) < min_gap_abs:
                continue
            return True, float(c1['high']), float(c3['low'])

    # 2. Order block fallback — last bearish candle before a confirmed breakout above its high.
    # OB candle body must be >40% of range (real institutional bearish move, not a doji).
    # Breakout move above the OB high must exceed half the OB body (conviction required).
    for i in range(end - 1, start - 1, -1):
        candle = df.iloc[i]
        if candle['close'] >= candle['open']:
            continue
        body = abs(candle['close'] - candle['open'])
        rng  = candle['high'] - candle['low']
        if rng == 0 or body / rng < 0.40 or body < min_body_abs:
            continue
        later  = df.iloc[i + 1: end + 1]
        breaks = later[later['close'] > candle['high']]
        if not breaks.empty:
            breakout_move = float(breaks.iloc[0]['close']) - float(candle['high'])
            if breakout_move >= body * 0.5:
                return True, float(candle['low']), float(candle['high'])

    return False, None, None

def find_bearish_fvg(df, lookback=15, min_body_abs=0.0, min_gap_abs=0.0):
    """
    Scans the last `lookback` bars for a bearish imbalance zone.
    Primary: true FVG (gap down). Fallback: bearish order block.

    Same size gates as find_bullish_fvg — see there for why the fallback needs them.
    """
    if len(df) < 3:
        return False, None, None

    end   = len(df) - 1
    start = max(2, end - lookback)

    # 1. True FVG
    for i in range(end, start - 1, -1):
        c1, c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
        if c1['low'] > c3['high'] and c2['close'] < c2['open']:
            if abs(float(c2['close']) - float(c2['open'])) < min_body_abs:
                continue
            if float(c1['low']) - float(c3['high']) < min_gap_abs:
                continue
            return True, float(c3['high']), float(c1['low'])

    # 2. Order block fallback — last bullish candle before a breakdown below its low.
    # OB candle body must be >40% of range, breakdown must have conviction.
    for i in range(end - 1, start - 1, -1):
        candle = df.iloc[i]
        if candle['close'] <= candle['open']:
            continue
        body = abs(candle['close'] - candle['open'])
        rng  = candle['high'] - candle['low']
        if rng == 0 or body / rng < 0.40 or body < min_body_abs:
            continue
        later  = df.iloc[i + 1: end + 1]
        breaks = later[later['close'] < candle['low']]
        if not breaks.empty:
            breakdown_move = float(candle['low']) - float(breaks.iloc[0]['close'])
            if breakdown_move >= body * 0.5:
                return True, float(candle['low']), float(candle['high'])

    return False, None, None


# ============================================================================
# AMD CYCLE INTELLIGENCE — Accumulation / Manipulation / Distribution
# ============================================================================

def detect_amd_phase(df_htf, structure_bars=25, recent_bars=5):
    """
    Detects which AMD phase the 4H chart is in by looking for liquidity sweeps.

    'manipulation_up'  : a recent wick swept BELOW the prior structural swing low
                         but the close is now BACK ABOVE it → stop hunt complete,
                         price is bleeding up. Real play may be SHORT from supply.
    'manipulation_down': symmetric — recent wick swept ABOVE prior swing high,
                         now rejected below → real play may be LONG from demand.
    'unknown'          : no clear sweep-and-recover pattern detected.

    Returns: (phase: str, info: dict)
      info keys: swept_level, sweep_wick, manipulation_target
    """
    if df_htf is None or len(df_htf) < structure_bars + recent_bars:
        return 'unknown', {}

    bars    = df_htf.tail(structure_bars + recent_bars).reset_index(drop=True)
    struct  = bars.iloc[:structure_bars]
    recent  = bars.iloc[structure_bars:]

    struct_swing_low  = float(struct['low'].min())
    struct_swing_high = float(struct['high'].max())
    current_close     = float(bars.iloc[-1]['close'])
    recent_low_wick   = float(recent['low'].min())
    recent_high_wick  = float(recent['high'].max())
    swing_range       = struct_swing_high - struct_swing_low
    swing_mid         = (struct_swing_high + struct_swing_low) / 2

    # Accumulation: structure bars coiling in a tight box, no sweep yet
    range_pct = swing_range / swing_mid if swing_mid > 0 else 1.0
    if (range_pct < 0.05                                      # tight box < 5% wide
            and struct_swing_low <= current_close <= struct_swing_high   # price still inside
            and recent_low_wick  >= struct_swing_low  * 0.995            # no sweep below yet
            and recent_high_wick <= struct_swing_high * 1.005):          # no sweep above yet
        return 'accumulation', {
            'range_high': struct_swing_high,
            'range_low':  struct_swing_low,
            'range_pct':  range_pct,
            'mid':        swing_mid,
        }

    # Manipulation UP: wick pierced below structure low, close recovered above it
    if (recent_low_wick < struct_swing_low
            and current_close > struct_swing_low
            and swing_range > 0
            and (current_close - struct_swing_low) / swing_range < 0.75):
        return 'manipulation_up', {
            'swept_level':         struct_swing_low,
            'sweep_wick':          recent_low_wick,
            'manipulation_target': struct_swing_high,
        }

    # Manipulation DOWN: wick pierced above structure high, close rejected below it
    if (recent_high_wick > struct_swing_high
            and current_close < struct_swing_high
            and swing_range > 0
            and (struct_swing_high - current_close) / swing_range < 0.75):
        return 'manipulation_down', {
            'swept_level':         struct_swing_high,
            'sweep_wick':          recent_high_wick,
            'manipulation_target': struct_swing_low,
        }

    return 'unknown', {}


# ============================================================================
# ENTRY GATING — the four defects behind the real 2026-08-11 AVAX SHORT
# (entry $6.231, zone $6.24-$6.454, $79 risk, in a market with a 1.5% 4h range).
# All pure so the gating is unit-provable; see tests/test_entry_gating.py.
# ============================================================================

def carried_zone_age(new_lo, new_hi, prev_lo, prev_hi, prev_age, tol_pct=0.002):
    """Bars-in-wait a newly armed zone should START at, carrying age across re-arms.

    THE ROOT CAUSE of the recurring "armed ages ago, entered on nothing" trades:
    binance_bot.py kept no memory of expired zones, and SymbolState.reset() calls
    __init__(), zeroing bars_in_entry_wait. So an expired zone fell to IDLE, the next
    5-min cycle re-derived the SAME zone from the same slow-moving 6h HTF data, and
    re-armed with the counter back at 0 — indefinitely. bars_in_entry_wait therefore
    measured time since the last RE-ARM, never how long the zone had actually existed,
    and the STALE_ZONE_BARS freshness gate could never fire. Live proof: the counter
    read 3 while the zone had visibly sat ~20 bars (1h40m) on the chart.

    Returns prev_age when the re-armed zone matches the previous one within tol_pct
    (HTF levels wobble slightly between recomputes), else 0 for a genuinely new zone."""
    if prev_lo is None or prev_hi is None or new_lo is None or new_hi is None:
        return 0
    for new, prev in ((new_lo, prev_lo), (new_hi, prev_hi)):
        ref = abs(prev) or 1.0
        if abs(new - prev) / ref > tol_pct:
            return 0
    return prev_age


def drop_forming_candle(df):
    """Drop the newest (still-forming) bar so zone edges come only from CLOSED candles.

    A zone is snapshotted at arm time but its edge could come from a bar still in
    progress, whose high/low keeps moving. Real incident: the stored edge was $6.24;
    by entry the same finder returned $6.326 because that bar had printed a higher
    high — against live data price sat 1.5% BELOW the zone and the entry would never
    have fired. Deriving zones from closed bars only makes them stable between
    recomputes, so the stored zone still means what it meant when it was armed."""
    if df is None or len(df) < 2:
        return df
    return df.iloc[:-1]


def zone_left_since_arming(price, zone_lo, zone_hi, already_left):
    """Has price traded OUTSIDE this zone since it was armed? Sticky once True.

    A "retest" means price left a level and came back to it. The bot printed
    "waiting for retest/refill" and then never checked: nothing tracked whether price
    had ever been outside the zone, so the FIRST touch counted — and for a choch_fvg
    zone the first touch is the displacement itself, because that zone IS the gap the
    impulse just tore open. Price is normally still inside it at the moment it is armed.

    REAL ENTRIES 2026-09-24:
        DOGE  zone $0.0951-$0.0955 armed 14:44 on a shooting_star, FILLED the same cycle
              at $0.0954 — inside the gap it had just made, at the top of the impulse.
        ASTER zone $0.7011-$0.7034, filled $0.7004 with the last candle a marubozu_bear.

    The ordinary case is unaffected and must stay that way: a demand zone armed BELOW
    price is already outside-the-zone, so it returns True immediately and the eventual
    drop into it trades exactly as before. This only delays the case where the zone was
    armed AROUND the current price, which is precisely the one that was never a retest.

    Fails CLOSED: an unreadable price or zone never GRANTS a retest, but it also never
    erases one already earned.
    """
    if already_left:
        return True
    try:
        px = float(price)
    except (TypeError, ValueError):
        return False
    if px != px:                                  # NaN
        return False
    try:
        lo = float(zone_lo)
        hi = float(zone_hi)
    except (TypeError, ValueError):
        return False
    if lo != lo or hi != hi or lo > hi:
        return False
    return px < lo or px > hi


def price_in_entry_zone(price, zone_lo, zone_hi, is_long, tol_pct=0.0015):
    """True when price has genuinely REACHED its zone, not merely come close to it.

    The old check was a symmetric ±tol band (`lo - tol <= price <= hi + tol`), which
    let a SHORT fill BELOW the supply zone — selling supply at a discount, the exact
    opposite of the setup. Real incident: price $6.231 vs zone_lo $6.24 cleared the
    old $6.2307 threshold by $0.0003 (0.005% of price) and filled 3.6% below the top
    of the supply it was supposedly selling into.

    Tolerance now applies only on the FAR side, where overshooting still improves the
    fill (a short deeper into supply, a long deeper into demand); the approach side is
    hard — price must actually trade into the zone."""
    if zone_lo is None or zone_hi is None:
        return False
    tol = abs(price) * tol_pct
    if is_long:
        return (zone_lo - tol) <= price <= zone_hi
    return zone_lo <= price <= (zone_hi + tol)


def tap_candle_opposes_bias(candle_type, bias):
    """True when the tap bar is a decisive move AGAINST the trade.

    A veto, deliberately — not a confirmation requirement.

    The choch_fvg direct tap exists on a real premise: the displacement that left the gap
    already broke structure, so it IS the change of character and a quick retest is
    self-confirming. Demanding a second confirmation would discard that, and on a tape
    that mostly consolidates it would cut trade count hard for little gain.

    But "no confirmation needed" was implemented as "no check at all", and the two are not
    the same. POL/USD 2026-09-14 tapped a LONG zone on a `marubozu_bear` — a full-bodied
    DOWN candle — with the bot printing '⚠️ marubozu_bear (no candle confirm)' and entering
    anyway. That is not an unconfirmed tap, it is an actively contradicted one.

    So this blocks only the decisive opposing bar and stays silent on everything else:
    a `normal` or `doji` tap still passes (XRP, which won, tapped on `normal`).
    """
    opposes_long = {"shooting_star", "gravestone_doji", "bearish_engulfing",
                    "hanging_man", "marubozu_bear"}
    opposes_short = {"hammer", "dragonfly_doji", "bullish_engulfing",
                     "inverted_hammer", "marubozu_bull"}
    if bias == "BULLISH":
        return candle_type in opposes_long
    if bias == "BEARISH":
        return candle_type in opposes_short
    return False


def sweep_hunt_expired(sweep_hunt_bar, patience, has_sweep, hard_ceiling_mult=2):
    """Has a SWEEP_HUNT sat so long that the BOS driving it is no longer worth trading?

    The original guard was `sweep_hunt_bar > patience and not has_sweep`. Because
    `sweep_low` is set on the FIRST sweep and only cleared by reset(), that `and not`
    made the expiry unreachable the moment any sweep printed — the 4-hour limit became
    infinite.

    Measured on the live log: BTC held SWEEP_HUNT for 373 consecutive cycles (~62h50m) on
    a 1H BOS from three days earlier, surviving a bot restart because sweep_hunt_bar is
    persisted. POL's was ~34h. The sweep level itself drifted $76,030 -> $77,455 (1.9%)
    across that window without ever being treated as a new setup.

    Two expiries now:
      - no sweep at all after `patience` bars  -> the BOS never produced its setup
      - any state older than `patience * hard_ceiling_mult` -> the BOS is simply too old,
        sweep or not. A directional read from three days ago is not a read on now.
    """
    try:
        bars = int(sweep_hunt_bar or 0)
        patience = int(patience or 0)
    except (TypeError, ValueError):
        return False, ""
    if patience <= 0:
        return False, ""
    if bars > patience * hard_ceiling_mult:
        return True, (f"BOS is {bars} bars old (>{patience * hard_ceiling_mult}) — too stale "
                      f"to trade off, re-hunting from IDLE")
    if bars > patience and not has_sweep:
        return True, f"no sweep in {bars} bars — BOS stale"
    return False, ""


def sniper_entry_allowed(zone_type, is_stale, has_momentum,
                         candle_confirms, choch_aligned, opposing_candle=False):
    """(ok, reason) — may the 10-second sniper fire on this tap?

    The sniper and the 5-minute cycle are supposed to enforce the same entry rule. They
    did not. The 5m path requires, on a FRESH zone:

        choch_fvg      -> direct tap (the displacement IS the change of character)
        anything else  -> rebounce = candle_confirms OR choch_aligned

    The sniper checked NOTHING on a fresh zone. A staleness gate was added and its comment
    claimed to mirror the main cycle "exactly", but it only covered the STALE branch — the
    fresh branch stayed wide open. Because the sniper polls every 10s against the main
    cycle's 5 minutes, it wins nearly every race, so in practice the candle gate became
    dead code: of 22 logged taps, 19 FAILED the bot's own confirmation check and were
    entered anyway — including four marubozu_bear candles at LONG taps.

    Deliberate asymmetry: the sniper does not fetch 15m data on a 10-second cadence, so
    callers pass choch_aligned=False. That makes it STRICTER than the 5m path, never
    looser — a tap it declines is simply picked up by the next 5m cycle, which does have
    the 15m read. Declining late is recoverable; entering wrongly is not.

    2026-09-17 — the FRESH branch was still too loose, and it cost three trades in one
    morning (AVAX, SEI, ADA, all long into a sideways drift). has_momentum was consulted
    ONLY on the stale branch, so a fresh zone whose type was None fell straight through
    to `candle_confirms or choch_aligned`: one ordinary green candle was the entire entry
    requirement. The AVAX log is the whole story — eleven refusals in a row, then a single
    confirming candle and it fired, with bos_dir, choch_dir, disp_high, disp_low and
    amd_zone_type ALL None.

    So displacement is now required on every zone except choch_fvg, fresh or stale.
    choch_fvg keeps its direct tap because there the displacement IS the zone; demanding
    it again would be circular, and that path's entries were never the ones complained
    about. Everything else must show a real impulse at the tap AND a confirming candle.
    """
    # A decisive bar AGAINST the trade vetoes every zone type, choch_fvg included.
    # 2026-09-24: ASTER filled LONG at $0.7004 with the last candle a marubozu_bear, and
    # DOGE armed on a shooting_star. tap_candle_opposes_bias existed and was wired into
    # the 5-MINUTE cycle only — and this watcher polls every 10 seconds, so it wins
    # nearly every race and the veto was effectively dead code. Buying a decisive down
    # bar is not a thing the choch premise ever justified.
    if opposing_candle:
        return False, "the tap bar is decisively AGAINST the trade"
    if is_stale:
        return (True, "stale zone, fresh momentum confirmed") if has_momentum else \
               (False, "stale zone with no fresh displacement")
    if zone_type == "choch_fvg":
        return True, "fresh displacement FVG — direct tap"
    if not has_momentum:
        return False, ("fresh zone with no displacement at the tap — needs a real "
                       "impulse, not just a confirming candle")
    if candle_confirms or choch_aligned:
        return True, "fresh zone, displacement and tap both confirmed"
    return False, "displacement present but the tap candle does not confirm the bias"


def sweep_within_reach(sweep_level, price, atr, max_atr_mult=2.5, max_pct=0.02):
    """(ok, why) — is the swept level still close enough to price to mean anything?

    sweep_hunt_expired() caps how LONG a hunt may run. Nothing capped how FAR the
    liquidity was. On 2026-09-17 all three bad entries armed off sweeps nowhere near the
    market: AVAX swept $7.17 and armed a zone at $7.61 (5.7% away), SEI 6.3%, ADA 5.8%.
    A grab that far below price is not the inducement for THIS move; it is a different
    piece of history that happens to still be sitting in a variable.

    The limit is volatility-relative, because a flat percentage is wrong in both
    directions — 5.7% is absurd on a quiet chart and unremarkable on a violent one. The
    percentage acts only as a floor, so a dead ATR reading tightens the gate to a sane
    constant instead of collapsing it to zero (refuse everything) or to infinity.

    A missing sweep is NOT reported as a reach failure: "there was no sweep" is a
    different question, owned by the caller's own `if not state.sweep_low` guard.
    """
    if not sweep_level:
        return True, "no sweep level to judge"
    try:
        price, sweep_level, atr = float(price), float(sweep_level), float(atr or 0.0)
    except (TypeError, ValueError):
        return False, "unreadable sweep level or price"
    if price <= 0:
        return False, "no usable price to measure the sweep against"
    distance = abs(price - sweep_level)
    limit = max(max_atr_mult * max(atr, 0.0), max_pct * price)
    pct, limit_pct = distance / price, limit / price
    if distance > limit:
        return False, (f"swept level ${sweep_level:,.4f} is {pct:.1%} from ${price:,.4f}"
                       f" — beyond the {limit_pct:.1%} reach limit")
    return True, f"swept level {pct:.1%} away, within the {limit_pct:.1%} limit"


def wick_fill_cutoff_ms(entry_ms, stop_moved_ms=0):
    """Earliest candle a resting stop could legitimately fill on.

    A stop can only fill on price action that happened WHILE THAT STOP EXISTED. The
    watcher already knew half of this — it skipped candles opened before ENTRY, with the
    comment "candle opened pre-entry — wick untrustworthy", added after a live trade
    logged "TP hit" one second after entry off a pre-fill wick.

    The identical bug sits one level up and is far more expensive: when the break-even
    trail MOVES the stop, every candle from before the move is still in the window.

    The arithmetic makes it fire every single time. Break-even arms at +1.25R and places
    the stop at entry x (1 + ROUND_TRIP_COST x 1.5) — BELOW the trigger. On the same
    10-second tick:

        cur <= stop        -> impossible, cur just cleared a HIGHER bar
        candle_low <= stop -> certain, because price had to rise THROUGH the stop level
                              to reach +1.25R in the first place

    Real case (BTC 2026-09-14): entry $77,587.61, +1.25R at $78,227.65, stop placed at
    $78,169.52, closed at $78,169.5171 on the same tick. The target was reached 6.5h
    later and the original stop was never within $395 of being hit.

    Consequence: EVERY position reaching +1.25R is force-closed at roughly +1.14R while
    every loser still takes a full 1.00R. That caps realized R:R at 1:1.14 against a 1:2
    design — measured at 1:0.98 over 36 closes, with only 4 of 35 exits ever reaching
    target.
    """
    try:
        entry_ms = int(entry_ms or 0)
    except (TypeError, ValueError):
        entry_ms = 0
    try:
        stop_moved_ms = int(stop_moved_ms or 0)
    except (TypeError, ValueError):
        stop_moved_ms = 0
    return max(entry_ms, stop_moved_ms)


def first_protective_breach(candles, stop, target, is_long, since_ms=0):
    """The FIRST protective level a price series breached — ("STOP"|"TARGET", price, ts).

    A paper bot's stop lives only in its own process, so when the bot is down the stop
    does not exist. The startup catch-up compared the CURRENT price against the levels,
    which means a stop that was blown through and recovered from during the downtime was
    missed entirely and the position carried on as though nothing happened. The account
    then reports a result no real broker would have produced.

    Walking the candles of the gap restores the stop's authority after the fact: a resting
    order would have filled the instant price TOUCHED the level, so wicks count, not
    closes.

    Chronological order is the whole point — a trade that hit its stop at 03:00 cannot be
    rescued by its target printing at 05:00. When BOTH levels fall inside the SAME candle
    the sequence within that bar is unknowable, so this returns STOP. A simulation must
    not hand itself the better of two outcomes it cannot distinguish; that is how a
    backtest ends up describing a strategy nobody could have traded.

    `since_ms` skips bars that opened before the position existed — their wicks carry
    prices that predate the entry, and an order that did not exist yet cannot fill on them.
    """
    if not candles:
        return None
    try:
        stop = float(stop); target = float(target)
    except (TypeError, ValueError):
        return None
    for c in candles:
        try:
            ts, high, low = int(c[0]), float(c[2]), float(c[3])
        except (TypeError, ValueError, IndexError):
            continue
        if ts < (since_ms or 0):
            continue
        if is_long:
            hit_stop, hit_target = low <= stop, high >= target
        else:
            hit_stop, hit_target = high >= stop, low <= target
        if hit_stop:                       # STOP wins ties — see above
            return "STOP", stop, ts
        if hit_target:
            return "TARGET", target, ts
    return None


def risk_budget(equity, pct, floor=0.0, ceiling=None):
    """Dollars to risk on ONE trade: `pct` of live equity, clamped to [floor, ceiling].

    Replaces a hardcoded MAX_RISK_DOLLARS. A fixed dollar amount is a different bet at
    every account size — $20 is 0.2% of $10,000 and 20% of $100 — so the constant silently
    changes meaning the moment the balance does. Percent-of-equity keeps the meaning fixed
    and does two things dollars cannot: it COMPOUNDS as the account grows, and it DE-RISKS
    automatically in a drawdown (down 20% -> risk per trade drops 20% with it).

    Deliberately takes EQUITY, not balance. PaperTrader.balance is free cash only, so with
    positions open it understates the account and would shrink the budget for reasons that
    have nothing to do with risk appetite.

    `ceiling` is a circuit breaker, not a target: one bad equity read (a stale price, a
    mispriced position) must not be able to size the next trade off a fantasy number.

    Leverage is deliberately absent. Risk is (entry - stop) x qty; leverage only changes
    how much cash is posted as margin. Sizing the same either way is what lets the same
    constants work on 1x spot and 10x perps.
    """
    try:
        equity = float(equity); pct = float(pct)
    except (TypeError, ValueError):
        return 0.0
    if equity != equity or equity <= 0 or pct <= 0:
        return 0.0
    budget = equity * pct
    if ceiling is not None:
        budget = min(budget, float(ceiling))
    return max(budget, float(floor or 0.0))


def meets_exchange_minimums(qty, price, min_amount=None, min_cost=None):
    """(ok, reason) — whether an order clears the venue's minimum size and notional.

    A risk-sized order on a small account can come out below what the exchange will
    accept, and the failure mode matters: rounding UP to the minimum silently breaks the
    risk cap that produced the number. A $20-risk order that gets rounded up to a $50-risk
    order is no longer the trade that was approved. So this REFUSES rather than adjusts —
    the setup is simply too small for this account at this stop distance.

    Both limits come from ccxt's market['limits']; either may be absent, and an absent
    limit is not a constraint.
    """
    try:
        qty = abs(float(qty)); price = float(price)
    except (TypeError, ValueError):
        return False, "unreadable qty/price"
    if qty <= 0 or price <= 0:
        return False, "zero qty or price"
    if min_amount is not None and qty < float(min_amount):
        return False, (f"qty {qty:.8g} below the venue minimum {float(min_amount):.8g} "
                       f"— rounding up would break the risk cap, so skipping")
    cost = qty * price
    if min_cost is not None and cost < float(min_cost):
        return False, (f"notional ${cost:,.2f} below the venue minimum "
                       f"${float(min_cost):,.2f} — skipping")
    return True, "ok"


def cap_qty_for_risk(qty, risk_per_unit, max_risk_dollars):
    """Shrink qty so a stop-out can't lose more than max_risk_dollars. Never scales UP.

    binance_bot.py's main entry path sized by MARGIN (balance x fraction), so real risk
    rode on however wide the structural stop happened to be — the live AVAX SHORT put
    $79 at risk on a 3.9% stop. The 10-second sniper path already sized by fixed risk
    (MAX_RISK_DOLLARS); this brings the main path in line so both agree."""
    if risk_per_unit <= 0:
        return 0.0
    return min(qty, max_risk_dollars / risk_per_unit)


def find_supply_zone(df_htf, current_price, min_distance_pct=0.001, max_distance_pct=0.12,
                      max_age_bars=30, min_width_pct=0.0015, return_all=False):
    """
    Scans the 4H chart for supply zones (bearish imbalances) ABOVE current price.

    Checks in order of reliability:
    1. Unmitigated bearish FVG — genuine gap-down imbalance that hasn't been refilled
    2. Bearish OB — bullish candle before a confirmed breakdown (institutional selling)
    3. IFVG (Inverse FVG) — old bullish FVG that price has since filled; it flips to
       resistance on the next retest from below
    4. Bearish BREAKER — a bullish (demand) candle that price later CLOSED below, so the
       demand failed and the block flips polarity into resistance.

    max_distance_pct: ignore zones more than this % above price (default 12%)
                      prevents locking a supply zone $20k above BTC current price.
    max_age_bars: ignore zones whose forming candle pattern is older than this many bars
                  (default 30, matching detect_amd_phase's structure+recent window — about
                  7.5 days on 6h HTF candles). "Unmitigated" (never revisited) alone is NOT
                  the same as fresh — REAL INCIDENT 2026-08-02: an AVAX SHORT was armed off
                  a bearish_ob candle from 34.2 days earlier, found purely because price
                  hadn't traded back through it since. Without this bound the full HTF
                  history (up to 200 bars / ~50 days) is fair game regardless of age.

    Returns: (found: bool, zone_low: float, zone_high: float, zone_type: str)
    """
    if df_htf is None or len(df_htf) < 5:
        return False, 0.0, 0.0, ''

    bars = df_htf.reset_index(drop=True)
    n    = len(bars)
    min_price = current_price * (1 + min_distance_pct)
    max_price = current_price * (1 + max_distance_pct)
    candidates = []   # (zone_low, zone_high, zone_type)

    scan_start = max(2, n - 1 - max_age_bars)
    for i in range(scan_start, n - 1):
        c1 = bars.iloc[i - 2]
        c2 = bars.iloc[i - 1]
        c3 = bars.iloc[i]
        sub = bars.iloc[i + 1:]   # bars that come after this pattern

        # 1. Bearish FVG: c1.low > c3.high  (price gaped DOWN, imbalance above)
        if c1['low'] > c3['high']:
            z_lo, z_hi = float(c3['high']), float(c1['low'])
            if min_price <= z_lo <= max_price:
                already_filled = len(sub) > 0 and float(sub['high'].max()) >= z_lo
                if not already_filled:
                    candidates.append((z_lo, z_hi, 'bearish_fvg'))

        # 2. Bearish OB: bullish c1 immediately before a breakdown
        if c1['close'] > c1['open']:
            body = abs(c1['close'] - c1['open'])
            rng  = c1['high'] - c1['low']
            if rng > 0 and body / rng >= 0.40:
                broke_down = (len(sub) > 0
                              and float(sub['low'].min()) < float(c1['open']))
                if broke_down:
                    z_lo, z_hi = float(c1['open']), float(c1['high'])
                    if min_price <= z_lo <= max_price:
                        candidates.append((z_lo, z_hi, 'bearish_ob'))

        # 3. IFVG: old bullish FVG (c1.high < c3.low) that price has since filled
        #    After being filled it becomes resistance — the "inverse" zone
        if c1['high'] < c3['low']:
            z_lo, z_hi = float(c1['high']), float(c3['low'])
            was_filled = len(sub) > 0 and float(sub['low'].min()) <= z_lo
            if was_filled and min_price <= z_lo <= max_price:
                candidates.append((z_lo, z_hi, 'ifvg'))

        # 4. Bearish BREAKER: a bullish (demand) candle that price LATER CLOSED below —
        #    the demand was violated, so the block flips polarity into resistance.
        if c1['close'] > c1['open']:
            body = abs(c1['close'] - c1['open'])
            rng  = c1['high'] - c1['low']
            if rng > 0 and body / rng >= 0.40:
                broke_below = len(sub) > 0 and float(sub['close'].min()) < float(c1['low'])
                if broke_below:
                    z_lo, z_hi = float(c1['low']), float(c1['high'])
                    if min_price <= z_lo <= max_price:
                        candidates.append((z_lo, z_hi, 'bearish_breaker'))

    # WIDTH FLOOR. A zone thinner than this is a LINE, not a zone: price crosses it
    # inside a single tick, so the tap either never registers or registers on noise, and
    # a structural stop placed against it sits inside the spread. Same rule
    # detect_displacement_fvg / find_bullish_fvg / find_bearish_fvg already enforce via
    # min_gap_abs ("A one-tick gap is a line, not a zone") — these two never did, and
    # they are what produce the AMD and trend-follow zones the bots mostly arm.
    #   2026-09-22: AAPL armed [bullish_fvg] 338.49-338.53 — FOUR CENTS on a $338 stock,
    #   0.012%, against 0.198%-2.319% for every other symbol that morning — and sat in
    #   ENTRY_WAIT on it for hours without ever filling.
    # Defaulted ON rather than opt-in: all eight live call sites across the two bots pass
    # no width argument, so a floor that had to be requested would have been missed at
    # every one of them.
    try:
        _min_width = float(min_width_pct) * float(current_price)
    except (TypeError, ValueError):
        return False, 0.0, 0.0, ''
    if _min_width != _min_width or _min_width < 0.0:      # NaN or nonsense price
        return False, 0.0, 0.0, ''
    candidates = [c for c in candidates if (c[1] - c[0]) >= _min_width]

    if not candidates:
        return False, 0.0, 0.0, ''

    # Nearest zone above price (lowest zone_low); tie-break by conviction
    # (a breaker = confirmed structural flip, higher conviction than a plain OB).
    _prio = {'bearish_breaker': 3, 'bearish_fvg': 2, 'ifvg': 1, 'bearish_ob': 0}
    candidates.sort(key=lambda x: (x[0], -_prio.get(x[2], 0)))
    z_lo, z_hi, z_type = candidates[0]
    # Measurement hook: hand back EVERY qualifying zone, not just the winner, so a
    # different selection policy can be scored against this one on the same candidates.
    # Off by default — the chosen zone is byte-for-byte what it was.
    if return_all:
        return True, z_lo, z_hi, z_type, [dict(lo=a, hi=b, kind=c) for a, b, c in candidates]
    return True, z_lo, z_hi, z_type


def find_demand_zone(df_htf, current_price, min_distance_pct=0.001, max_distance_pct=0.12,
                      max_age_bars=30, min_width_pct=0.0015, return_all=False):
    """
    Scans the 4H chart for demand zones (bullish imbalances) BELOW current price.
    Mirror of find_supply_zone for LONG setups.

    1. Unmitigated bullish FVG below price
    2. Bullish OB — bearish candle before a strong breakout up
    3. IFVG support — old bearish FVG that price filled; flips to support
    4. Bullish BREAKER — a bearish (supply) candle that price later CLOSED above, so the
       supply failed and the block flips polarity into support.

    max_distance_pct: ignore zones more than this % below price (default 12%).
    max_age_bars: see find_supply_zone — same recency bound, same reasoning (default 30).

    Returns: (found: bool, zone_low: float, zone_high: float, zone_type: str)
    """
    if df_htf is None or len(df_htf) < 5:
        return False, 0.0, 0.0, ''

    bars = df_htf.reset_index(drop=True)
    n    = len(bars)
    max_price = current_price * (1 - min_distance_pct)
    min_price = current_price * (1 - max_distance_pct)
    candidates = []

    scan_start = max(2, n - 1 - max_age_bars)
    for i in range(scan_start, n - 1):
        c1 = bars.iloc[i - 2]
        c2 = bars.iloc[i - 1]
        c3 = bars.iloc[i]
        sub = bars.iloc[i + 1:]

        # 1. Bullish FVG: c1.high < c3.low (price gaped UP)
        if c1['high'] < c3['low']:
            z_lo, z_hi = float(c1['high']), float(c3['low'])
            if min_price <= z_hi <= max_price:
                already_filled = len(sub) > 0 and float(sub['low'].min()) <= z_lo
                if not already_filled:
                    candidates.append((z_lo, z_hi, 'bullish_fvg'))

        # 2. Bullish OB: bearish c1 before breakout up
        if c1['close'] < c1['open']:
            body = abs(c1['close'] - c1['open'])
            rng  = c1['high'] - c1['low']
            if rng > 0 and body / rng >= 0.40:
                broke_up = (len(sub) > 0
                            and float(sub['high'].max()) > float(c1['open']))
                if broke_up:
                    z_lo, z_hi = float(c1['low']), float(c1['open'])
                    if min_price <= z_hi <= max_price:
                        candidates.append((z_lo, z_hi, 'bullish_ob'))

        # 3. IFVG support: old bearish FVG (c1.low > c3.high) price later filled
        if c1['low'] > c3['high']:
            z_lo, z_hi = float(c3['high']), float(c1['low'])
            was_filled = len(sub) > 0 and float(sub['high'].max()) >= z_hi
            if was_filled and min_price <= z_hi <= max_price:
                candidates.append((z_lo, z_hi, 'ifvg_support'))

        # 4. Bullish BREAKER: a bearish (supply) candle that price LATER CLOSED above —
        #    the supply was violated, so the block flips polarity into support.
        if c1['close'] < c1['open']:
            body = abs(c1['close'] - c1['open'])
            rng  = c1['high'] - c1['low']
            if rng > 0 and body / rng >= 0.40:
                broke_above = len(sub) > 0 and float(sub['close'].max()) > float(c1['high'])
                if broke_above:
                    z_lo, z_hi = float(c1['low']), float(c1['high'])
                    if min_price <= z_hi <= max_price:
                        candidates.append((z_lo, z_hi, 'bullish_breaker'))

    # Width floor — see the matching note in find_supply_zone. A zone thinner than this
    # is a line, not a zone; AAPL's 338.49-338.53 (0.012%) came through here.
    try:
        _min_width = float(min_width_pct) * float(current_price)
    except (TypeError, ValueError):
        return False, 0.0, 0.0, ''
    if _min_width != _min_width or _min_width < 0.0:      # NaN or nonsense price
        return False, 0.0, 0.0, ''
    candidates = [c for c in candidates if (c[1] - c[0]) >= _min_width]

    if not candidates:
        return False, 0.0, 0.0, ''

    # Nearest zone below price = highest zone_high; tie-break by conviction (breaker first).
    _prio = {'bullish_breaker': 3, 'bullish_fvg': 2, 'ifvg_support': 1, 'bullish_ob': 0}
    candidates.sort(key=lambda x: (-x[1], -_prio.get(x[2], 0)))
    z_lo, z_hi, z_type = candidates[0]
    # Measurement hook: hand back EVERY qualifying zone, not just the winner, so a
    # different selection policy can be scored against this one on the same candidates.
    # Off by default — the chosen zone is byte-for-byte what it was.
    if return_all:
        return True, z_lo, z_hi, z_type, [dict(lo=a, hi=b, kind=c) for a, b, c in candidates]
    return True, z_lo, z_hi, z_type

# ============================================================================
# CHART STRUCTURE — Flags, Channels
# ============================================================================

def flag_breakout_retest(df, is_long, max_shift=6, atr_mult=0.5,
                         pole_bars=10, flag_bars=8, min_pole_pct=0.04):
    """(found, retest_lo, retest_hi, stop_ref, target) — a broken flag worth retesting.

    detect_bull_flag refuses once `flag_high >= pole_high`, so the moment price breaks
    out of a flag the flag stops being detected. Asking "is there a flag AND has it
    broken out" in a single call is therefore impossible.

    This asks it as two questions against the same dataframe, holding no state: for each
    shift back, was there an INTACT flag as of that bar, and has price CLOSED through its
    edge since? The first (most recent) match wins. max_shift bounds how stale the
    breakout may be — beyond it, the "retest" is just a level from last week.

    The band returned is the retest of the broken edge (flag_high for a long, flag_low
    for a short), widened by atr_mult x ATR so the existing ENTRY_WAIT tap machinery has
    a real zone to work with. A dead ATR REFUSES rather than emitting a zero-width band:
    [x, x] is the four-cent AAPL zone again, crossed inside a single tick.

    MIND THE SIGNATURES — positions 1 and 2 swap meaning between the two detectors:
        detect_bull_flag -> (found, pole_low,  pole_high, flag_low, flag_high, target)
        detect_bear_flag -> (found, pole_high, pole_low,  flag_low, flag_high, target)
    """
    none = (False, None, None, None, None)
    try:
        n = len(df)
        closes = df["close"]
    except (TypeError, KeyError, AttributeError):
        return none
    if n < pole_bars + flag_bars + 1:
        return none

    atr = range_atr(df)
    if not atr or atr != atr or atr <= 0:
        return none                       # fail closed — no width, no zone

    detect = detect_bull_flag if is_long else detect_bear_flag
    for shift in range(1, int(max_shift) + 1):
        if n - shift < pole_bars + flag_bars:
            break
        window = df.iloc[:-shift]
        found, _a, _b, flag_low, flag_high, target = detect(
            window, pole_bars=pole_bars, flag_bars=flag_bars, min_pole_pct=min_pole_pct)
        if not found:
            continue

        since = closes.iloc[-shift:]
        if is_long:
            if float(since.max()) <= flag_high:
                continue                  # never broke out (or broke the wrong way)
            edge, stop_ref = flag_high, flag_low
        else:
            if float(since.min()) >= flag_low:
                continue
            edge, stop_ref = flag_low, flag_high

        half = atr * float(atr_mult)
        if half <= 0:
            return none
        return True, edge - half, edge + half, stop_ref, target

    return none


def detect_bull_flag(df, pole_bars=10, flag_bars=8, min_pole_pct=0.04):
    """
    Bull flag: sharp upward pole (≥ min_pole_pct move) followed by a tight
    downward/sideways flag. Flag must not retrace > 50% of the pole or exceed
    the pole high. Returns (found, pole_low, pole_high, flag_low, flag_high, measured_target).
    """
    if len(df) < pole_bars + flag_bars:
        return False, None, None, None, None, None

    pole_df = df.iloc[-(pole_bars + flag_bars):-flag_bars]
    flag_df = df.iloc[-flag_bars:]

    pole_low  = float(pole_df['low'].min())
    pole_high = float(pole_df['high'].max())
    pole_move = (pole_high - pole_low) / pole_low if pole_low > 0 else 0

    if pole_move < min_pole_pct:
        return False, None, None, None, None, None

    flag_high = float(flag_df['high'].max())
    flag_low  = float(flag_df['low'].min())
    pole_body = pole_high - pole_low
    retrace   = (pole_high - flag_low) / pole_body if pole_body > 0 else 1.0

    if retrace > 0.50 or flag_high >= pole_high:
        return False, None, None, None, None, None

    return True, pole_low, pole_high, flag_low, flag_high, flag_high + pole_body


def detect_bear_flag(df, pole_bars=10, flag_bars=8, min_pole_pct=0.04):
    """
    Bear flag: sharp downward pole (≥ min_pole_pct move) followed by a tight
    upward/sideways flag. Flag must not recover > 50% of the pole or break the
    pole low. Returns (found, pole_high, pole_low, flag_low, flag_high, measured_target).
    """
    if len(df) < pole_bars + flag_bars:
        return False, None, None, None, None, None

    pole_df = df.iloc[-(pole_bars + flag_bars):-flag_bars]
    flag_df = df.iloc[-flag_bars:]

    pole_high = float(pole_df['high'].max())
    pole_low  = float(pole_df['low'].min())
    pole_move = (pole_high - pole_low) / pole_high if pole_high > 0 else 0

    if pole_move < min_pole_pct:
        return False, None, None, None, None, None

    flag_high = float(flag_df['high'].max())
    flag_low  = float(flag_df['low'].min())
    pole_body = pole_high - pole_low
    retrace   = (flag_high - pole_low) / pole_body if pole_body > 0 else 1.0

    if retrace > 0.50 or flag_low <= pole_low:
        return False, None, None, None, None, None

    return True, pole_high, pole_low, flag_low, flag_high, flag_low - pole_body


def detect_channel(df, lookback=20):
    """
    Detects ascending or descending channel: ALL consecutive swing highs AND
    swing lows must trend in the same direction.
    Returns (channel_type, slope_pct) — channel_type is 'ascending', 'descending', or None.
    """
    if len(df) < lookback:
        return None, 0.0

    recent = df.tail(lookback).reset_index(drop=True)
    swing_highs, swing_lows = [], []

    for i in range(1, len(recent) - 1):
        h = float(recent.iloc[i]['high'])
        l = float(recent.iloc[i]['low'])
        if h >= float(recent.iloc[i-1]['high']) and h >= float(recent.iloc[i+1]['high']):
            swing_highs.append((i, h))
        if l <= float(recent.iloc[i-1]['low']) and l <= float(recent.iloc[i+1]['low']):
            swing_lows.append((i, l))

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None, 0.0

    highs_up   = all(swing_highs[i][1] > swing_highs[i-1][1] for i in range(1, len(swing_highs)))
    highs_down = all(swing_highs[i][1] < swing_highs[i-1][1] for i in range(1, len(swing_highs)))
    lows_up    = all(swing_lows[i][1]  > swing_lows[i-1][1]  for i in range(1, len(swing_lows)))
    lows_down  = all(swing_lows[i][1]  < swing_lows[i-1][1]  for i in range(1, len(swing_lows)))

    if highs_up and lows_up:
        slope = (swing_lows[-1][1] - swing_lows[0][1]) / swing_lows[0][1] if swing_lows[0][1] else 0
        return 'ascending', slope
    if highs_down and lows_down:
        slope = (swing_highs[-1][1] - swing_highs[0][1]) / swing_highs[0][1] if swing_highs[0][1] else 0
        return 'descending', slope

    return None, 0.0


# ============================================================================
# CANDLE PATTERN CLASSIFIER
# ============================================================================

def classify_candle(candle, prev_candle=None):
    """
    Identifies the candle pattern for a single OHLC bar.
    Returns a short label string — used in logs and as entry confirmation.

    Patterns detected:
      doji, gravestone_doji, dragonfly_doji   — indecision / reversal
      hammer, hanging_man                     — long lower wick
      shooting_star, inverted_hammer          — long upper wick
      bullish_engulfing, bearish_engulfing    — two-candle reversal
      marubozu_bull, marubozu_bear            — pure momentum, no wicks
      normal                                  — no notable pattern
    """
    o = float(candle['open'])
    h = float(candle['high'])
    l = float(candle['low'])
    c = float(candle['close'])

    rng = h - l
    if rng < 1e-10:
        return 'doji'

    body        = abs(c - o)
    upper_wick  = h - max(o, c)
    lower_wick  = min(o, c) - l
    body_pct    = body / rng
    is_bullish  = c >= o

    # ── Doji family (body < 10% of range) ────────────────────────────────────
    if body_pct < 0.10:
        if upper_wick > rng * 0.65 and lower_wick < rng * 0.15:
            return 'gravestone_doji'   # open≈close≈low, long upper wick → bearish
        if lower_wick > rng * 0.65 and upper_wick < rng * 0.15:
            return 'dragonfly_doji'   # open≈close≈high, long lower wick → bullish
        return 'doji'

    # ── Hammer / Hanging Man (small body near top, long lower wick ≥2× body) ─
    if lower_wick >= body * 2.0 and upper_wick <= body * 0.6:
        return 'hammer' if is_bullish else 'hanging_man'

    # ── Shooting Star / Inverted Hammer (small body near bottom, long upper wick)
    if upper_wick >= body * 2.0 and lower_wick <= body * 0.6:
        return 'inverted_hammer' if is_bullish else 'shooting_star'

    # ── Marubozu (≥85% body, almost no wicks — pure momentum) ───────────────
    if body_pct >= 0.85:
        return 'marubozu_bull' if is_bullish else 'marubozu_bear'

    # ── Two-candle engulfing (requires previous candle) ───────────────────────
    if prev_candle is not None:
        po = float(prev_candle['open'])
        pc = float(prev_candle['close'])
        if is_bullish and pc < po:           # previous was bearish
            if c > po and o < pc:            # current body fully engulfs previous
                return 'bullish_engulfing'
        if not is_bullish and pc > po:       # previous was bullish
            if c < po and o > pc:            # current body fully engulfs previous
                return 'bearish_engulfing'

    return 'normal'


def candle_confirms_bias(candle_type, bias):
    """
    Returns True if the candle pattern agrees with the intended trade direction.
    Used at ENTRY_WAIT to add one more confirmation layer.
    """
    bullish_patterns = {'hammer', 'dragonfly_doji', 'bullish_engulfing',
                        'inverted_hammer', 'marubozu_bull'}
    bearish_patterns = {'shooting_star', 'gravestone_doji', 'bearish_engulfing',
                        'hanging_man', 'marubozu_bear'}
    if bias == 'BULLISH':
        return candle_type in bullish_patterns
    if bias == 'BEARISH':
        return candle_type in bearish_patterns
    return False


# ============================================================================
# FIBONACCI RETRACEMENT - Optimal Trade Entry (OTE) Zone
# ============================================================================

def calculate_fib_levels(swing_low, swing_high):
    """
    Calculates Fibonacci retracement levels from swing low to swing high.
    Returns key levels: 0%, 23.6%, 38.2%, 50%, 61.8%, 78.6%, 100%
    The OTE (Optimal Trade Entry) zone is typically 38.2% to 61.8%.
    """
    difference = swing_high - swing_low
    
    fib_levels = {
        "0%": swing_high,
        "23.6%": swing_high - (difference * 0.236),
        "38.2%": swing_high - (difference * 0.382),
        "50%": swing_high - (difference * 0.50),
        "61.8%": swing_high - (difference * 0.618),
        "78.6%": swing_high - (difference * 0.786),
        "100%": swing_low
    }
    
    ote_zone = {
        "upper": swing_high - (difference * 0.382),
        "lower": swing_high - (difference * 0.618)
    }
    
    return fib_levels, ote_zone

def is_price_in_fib_ote(current_price, ote_zone):
    """
    Checks if current price is in the Fibonacci Optimal Trade Entry zone.
    This is where you wait for the retracement before entering.
    """
    return ote_zone["lower"] <= current_price <= ote_zone["upper"]

# ============================================================================
# FALLING WEDGE BREAKOUT
# ============================================================================

def detect_falling_wedge(df, lookback=60, swing_window=2, min_points=2):
    """
    Detect a falling wedge: descending swing highs converging with ascending
    swing lows. Returns (True, last_asc_low, projected_resistance) when the
    current close breaks above the projected descending-highs trendline.
    last_asc_low  = SL reference for the long retest entry.
    projected_resistance = the broken resistance level (now support).
    Returns (False, None, None) if no wedge or no breakout yet.
    """
    if df is None or len(df) < lookback:
        return False, None, None

    recent = df.tail(lookback)
    n      = len(recent)
    lows, highs = [], []

    for i in range(swing_window, n - swing_window):
        lo = float(recent['low'].iloc[i])
        hi = float(recent['high'].iloc[i])
        lo_nb = [float(recent['low'].iloc[j])
                 for j in range(i - swing_window, i + swing_window + 1) if j != i]
        hi_nb = [float(recent['high'].iloc[j])
                 for j in range(i - swing_window, i + swing_window + 1) if j != i]
        if lo < min(lo_nb):
            lows.append((i, lo))
        if hi > max(hi_nb):
            highs.append((i, hi))

    # Strictly ascending swing lows (higher lows = bullish support building)
    if len(lows) < min_points:
        return False, None, None
    asc = lows[-min_points:]
    if not all(asc[k][1] > asc[k - 1][1] for k in range(1, len(asc))):
        return False, None, None
    last_asc_low = float(asc[-1][1])

    # Strictly descending swing highs (lower highs = falling resistance)
    if len(highs) < min_points:
        return False, None, None
    desc = highs[-min_points:]
    if not all(desc[k][1] < desc[k - 1][1] for k in range(1, len(desc))):
        return False, None, None

    # Project the descending-highs line to the current bar
    i1, p1 = float(desc[-2][0]), float(desc[-2][1])
    i2, p2 = float(desc[-1][0]), float(desc[-1][1])
    if i2 == i1:
        return False, None, None
    slope     = (p2 - p1) / (i2 - i1)
    projected = p2 + slope * ((n - 1) - i2)

    # Breakout confirmed when current close is above the projected resistance
    if float(df['close'].iloc[-1]) <= projected:
        return False, None, None

    return True, last_asc_low, projected


# ============================================================================
# CHOCH / MARKET STRUCTURE SHIFT - Lower Timeframe Confirmation
# ============================================================================

def detect_choch(df, lookback=5):
    """
    Detects Change of Character (CHoCH) on lower timeframe.
    A break of the recent swing low/high confirms the structure shift.
    """
    if len(df) < lookback:
        return False, None
    
    recent = df.tail(lookback)
    latest = recent.iloc[-1]
    
    # Find recent swing high and low
    swing_high = recent['high'].iloc[:-1].max()
    swing_low = recent['low'].iloc[:-1].min()
    
    # CHoCH is a close beyond the swing point
    bullish_choch = latest['close'] > swing_high
    bearish_choch = latest['close'] < swing_low
    
    if bullish_choch:
        return True, "bullish"
    elif bearish_choch:
        return True, "bearish"
    
    return False, None

def daily_risk_remaining(risked_today, balance, daily_risk_pct):
    """Dollars of RISK still available today. Never negative.

    A daily cap is a sane idea — it stops one bad session compounding — but test_bot.py
    denominated it in DEPLOYED CAPITAL (DAILY_ACCOUNT_PCT = 10% of the account of
    notional, summed across all trades, never released on close). That silently undid
    risk-based sizing: on the $5,000 paper balance the $500/day notional ceiling meant a
    1%-risk trade actually risked $4 behind a 0.8% stop and $25 behind a 5% stop, so the
    TIGHTER and better the stop, the LESS money was at risk — the exact inversion
    risk-based sizing exists to prevent.

    Denominating the cap in the same unit the sizing measures fixes that: "no more than
    3% of the account lost in one day" is three full-size trades at 1%, whatever their
    stops happen to be, and it leaves position size alone.
    """
    try:
        budget = float(balance) * float(daily_risk_pct) - float(risked_today)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, budget)


def trim_qty_to_risk(qty, entry, stop, risk_remaining):
    """(qty, was_trimmed) — shrink qty so the trade risks at most `risk_remaining`.

    Floors rather than rounds: a trade must never end up risking a cent more than the
    day's budget allows, or the cap is not a cap. A zero-width stop is refused rather
    than treated as an unlimited position — a stop at the entry is a broken setup, not
    a free one.
    """
    try:
        qty = float(qty)
        risk_per_unit = abs(float(entry) - float(stop))
        risk_remaining = float(risk_remaining)
    except (TypeError, ValueError):
        return 0.0, True
    if risk_per_unit <= 0 or risk_remaining <= 0 or qty <= 0:
        return 0.0, True
    affordable = risk_remaining / risk_per_unit
    if qty <= affordable:
        return qty, False
    return math.floor(affordable * 1e6) / 1e6, True


def trend_filter_verdict(trend, direction):
    """(allowed, reason) — may this sweep be taken given the higher-timeframe trend?

    A LONG sweep is buy-the-dip and needs an uptrend; a SHORT sweep is sell-the-rip and
    needs a downtrend. Blocking the counter-trend side is what stops the bot fading a
    running move.

    UNREADABLE TRENDS ARE REFUSED, and that is the whole point of this function. The
    caller used to set `trend = None` on any exception during the 1h fetch and treat
    None as "allow". On 2026-09-18 that put a SHORT on ADA at $0.2291 — the exact 1H
    high — into a move that ran from $0.2210 to $0.2323, for a full-risk loss. Replayed
    afterwards against real data, the 4H trend was UP for the entire 24 hours before the
    entry: the gate could not have evaluated and permitted it, so it never evaluated.

    Without the read we do not know WHICH side is the counter-trend one, so neither side
    may pass. The next 1-minute tick tries again; declining late is recoverable in a way
    that fading a rocket is not. Same lesson as the AI gate: a check that cannot reach a
    verdict is not a verdict in favour.
    """
    t = trend.strip().upper() if isinstance(trend, str) else None
    d = direction.strip().upper() if isinstance(direction, str) else None
    if d not in ("LONG", "SHORT"):
        return False, f"unrecognised trade direction {direction!r} — standing aside"
    if t not in ("UP", "DOWN"):
        return False, ("could not read the higher-timeframe trend — standing aside "
                       "rather than risk fading a running move")
    if t == "UP" and d == "SHORT":
        return False, "fights the 4H UP trend (only longs with the trend)"
    if t == "DOWN" and d == "LONG":
        return False, "fights the 4H DOWN trend (only shorts with the trend)"
    return True, f"with the 4H {t} trend"


# A bar whose body is this small a fraction of its range is a fight, not a move: the
# doji / spinning-top / long-rejection-wick family.
INDECISION_BODY_FRAC = 0.35
# ...and this much body means the fight resolved.
MOMENTUM_BODY_FRAC = 0.50


# The rejection wick must be at least this much of the bar's range, and must dominate
# the wick on the other side. A gravestone at a swept high is the canonical shape: price
# pushed up, sellers slammed it back, and the upper wick is where that is recorded.
REJECTION_WICK_FRAC = 0.40
REJECTION_WICK_DOMINANCE = 1.5


def _bar_geometry(bar):
    """(body, rng, bullish, upper_wick, lower_wick) or None if the bar cannot be read."""
    try:
        o, c = float(bar["open"]), float(bar["close"])
        h, l = float(bar["high"]), float(bar["low"])
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    rng = h - l
    if rng <= 0:
        return None
    return abs(c - o), rng, c >= o, h - max(o, c), min(o, c) - l


def is_rejection_bar(bar, is_long, indecision_frac=None):
    """True when this bar is a small-bodied REJECTION of the level, the trade's way.

    Direction matters and the first version of this gate ignored it. A hammer at a swept
    high — tiny body, long LOWER wick — is buyers defending, and treating it as
    indecision before a SHORT reads the bar backwards. What confirms a short is a bar
    that pushed up and was pushed back: a gravestone, where the UPPER wick records the
    rejection. A long mirrors it.
    """
    geo = _bar_geometry(bar)
    if geo is None:
        return False
    body, rng, _bull, upper, lower = geo
    if body / rng > (INDECISION_BODY_FRAC if indecision_frac is None else indecision_frac):
        return False
    reject, opposite = (lower, upper) if is_long else (upper, lower)
    return (reject / rng >= REJECTION_WICK_FRAC
            and reject >= REJECTION_WICK_DOMINANCE * opposite)


def sweep_confirmation(candles, is_long, min_body_abs=0.0, lookback=4,
                       indecision_frac=INDECISION_BODY_FRAC,
                       momentum_frac=MOMENTUM_BODY_FRAC):
    """(ok, reason) — did the sweep produce INDECISION, then MOMENTUM the trade's way?

    A sweep on its own says only that a level was touched; price may simply keep going,
    which is what ADA did on 2026-09-18 while the bot was short. The two-bar pattern is
    what separates a reversal from a continuation:

      1. indecision AT the level — doji, spinning top, long rejection wick. The move
         that carried price in has stalled and both sides are fighting.
      2. momentum AWAY from it — a decisive body our way. The fight resolved, our way.

    Without (1) nothing was rejected. Without (2) there is a stall but no evidence
    anyone has taken the other side, and entering is guessing at the turn.

    The momentum bar must be the MOST RECENT one: stall, push, then three bars of chop
    is a stale setup, and taking it is the "enters after the big move" failure again.
    """
    if not isinstance(candles, (list, tuple)) or len(candles) < 2:
        return False, "not enough candles to see a sweep confirmation — standing aside"

    window = list(candles)[-max(2, lookback):]
    last = _bar_geometry(window[-1])
    if last is None:
        return False, "the latest candle could not be read — standing aside"

    body, rng, bullish = last[0], last[1], last[2]
    if body / rng < momentum_frac or body < min_body_abs or bullish != bool(is_long):
        return False, ("no momentum candle closing the trade's way yet — the sweep has "
                       "not resolved into a move")

    side = "lower" if is_long else "upper"
    for bar in reversed(window[:-1]):           # nearest first
        if is_rejection_bar(bar, is_long, indecision_frac):
            return True, (f"a {side}-wick rejection at the sweep, then momentum the "
                          f"trade's way")
    return False, (f"no rejection candle before the push — wanted a small body with a "
                   f"long {side} wick, showing the level was defended, not just passed "
                   f"through")


# ── Fibonacci: the real OTE, of a real leg, in the right direction ────────────
# calculate_fib_levels() above returns 38.2%-61.8% and calls it the Optimal Trade Entry.
# It is not: the OTE in ICT/SMC is 61.8%-78.6%. Measured on ADA's live 60-bar swing the
# two bands did not even overlap — drawn 0.22951-0.23130 against a true 0.22823-0.22951.
#
# Two more defects travelled with it. binance_bot anchored the fib to
# df_ltf['low'].tail(60).min() / ['high'].tail(60).max() — the extremes of an arbitrary
# 5-hour window, not an impulse leg — and calculate_fib_levels always measured DOWN from
# the high, with no way to express a retracement of a DOWN leg.
#
# The direction bug stayed hidden because 38.2-61.8 is symmetric: [hi-0.618d, hi-0.382d]
# from the high equals [lo+0.382d, lo+0.618d] from the low. The real OTE is not, so
# correcting the band alone would have switched a latent bug on.
OTE_LO_PCT = 0.618
OTE_HI_PCT = 0.786


def optimal_trade_entry(leg_low, leg_high, is_up_leg, lo_pct=OTE_LO_PCT, hi_pct=OTE_HI_PCT):
    """(ote_low, ote_high) — the 61.8-78.6% retracement of a leg, or None.

    An UP leg retraces DOWN from its high; a DOWN leg retraces UP from its low. These are
    different price bands and which one applies depends entirely on which end came last.
    """
    try:
        lo, hi = float(leg_low), float(leg_high)
    except (TypeError, ValueError):
        return None
    if hi <= lo:
        return None
    span = hi - lo
    if is_up_leg:                       # bought the impulse, wait for the pullback DOWN
        band = (hi - span * hi_pct, hi - span * lo_pct)
    else:                               # sold the impulse, wait for the pullback UP
        band = (lo + span * lo_pct, lo + span * hi_pct)
    return (min(band), max(band))


def find_swing_leg(df, lookback=60, swing_window=3):
    """(leg_low, leg_high, is_up_leg) for the most recent impulse leg, or None.

    The leg ENDS on the most recent swing pivot and STARTS on the most recent opposite
    pivot before it. A lone pivot is not a leg, and neither is the min/max of a window —
    that was the old anchor, and it slid around as the window rolled.
    """
    if df is None or not hasattr(df, "tail"):
        return None
    try:
        recent = df.tail(lookback).reset_index(drop=True)
    except Exception:
        return None
    n = len(recent)
    if n < (2 * swing_window + 1):
        return None

    highs, lows = [], []
    for i in range(swing_window, n - swing_window):
        window = range(i - swing_window, i + swing_window + 1)
        hv = float(recent.iloc[i]["high"])
        lv = float(recent.iloc[i]["low"])
        if all(hv > float(recent.iloc[j]["high"]) for j in window if j != i):
            highs.append((i, hv))
        if all(lv < float(recent.iloc[j]["low"]) for j in window if j != i):
            lows.append((i, lv))
    if not highs or not lows:
        return None

    last_high, last_low = highs[-1], lows[-1]
    if last_high[0] > last_low[0]:          # a high came last -> the leg ran UP into it
        starts = [p for p in lows if p[0] < last_high[0]]
        if not starts:
            return None
        return starts[-1][1], last_high[1], True
    starts = [p for p in highs if p[0] < last_low[0]]
    if not starts:
        return None
    return last_low[1], starts[-1][1], False


def zone_source_tf(htf_found, ltf_found, htf_name, ltf_name):
    """Which timeframe an armed zone came from, or None if neither had one.

    Mirrors the caller's own selection (`htf_value if is_htf else ltf_value`), so the
    label can never disagree with the data it describes. That mattered: BTC's armed zone
    of 82087-84753 was a REAL 6-hour FVG — c1.high 82087.11 to c3.low 84753.25, exact —
    drawn over a 5-minute chart where no such gap exists. Correct number, wrong canvas,
    and no way for anyone looking at it to tell.

    Names are passed in rather than hardcoded because the log already drifted once:
    tf_tag said "4H" while the bot was fetching "6h".
    """
    if htf_found:
        return htf_name
    if ltf_found:
        return ltf_name
    return None

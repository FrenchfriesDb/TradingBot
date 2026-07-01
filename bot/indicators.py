# bot/indicators.py
import pandas as pd

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


def detect_trendline(df, lookback=50, swing_window=2, min_points=3):
    """
    Detects the dominant DIAGONAL trendline so it can be drawn on the chart:
      • ascending  — ≥3 strictly higher swing LOWS  → rising support under price
      • descending — ≥3 strictly lower  swing HIGHS → falling resistance over price
    Diagonal structure the horizontal EQH/EQL detector can't see.

    Returns (found, kind, (t1, p1), (t2, p2)) — anchors as (unix_seconds, price) for the
    first and last swing on the line. (False, None, None, None) if no clean trendline.
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

    # Ascending support: the last min_points swing lows are strictly rising
    if len(lows) >= min_points:
        tail = lows[-min_points:]
        if all(tail[k][1] > tail[k - 1][1] for k in range(1, len(tail))):
            return True, 'ascending', tail[0], tail[-1]
    # Descending resistance: the last min_points swing highs are strictly falling
    if len(highs) >= min_points:
        tail = highs[-min_points:]
        if all(tail[k][1] < tail[k - 1][1] for k in range(1, len(tail))):
            return True, 'descending', tail[0], tail[-1]
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


def detect_displacement_fvg(df, lookback=20, window=10, min_body_pct=0.40, clearance=0.0015):
    """
    Finds the FVG left behind by the displacement that broke structure — the
    'CHoCH FVG'. This is the heart of the sweep → displacement → retest model:
    after a sweep, price reverses hard and breaks structure; that aggressive move
    leaves a 3-candle imbalance. The retest of THAT gap is the entry, and the gap
    itself IS the change of character — no separate CHoCH signal needed.

    A displacement candle (the middle of the 3) must:
      • have a body >= min_body_pct of its range (real momentum)
      • close beyond the prior swing by `clearance` (genuine structure break)
      • leave a gap: prior.high < next.low (bullish) / prior.low > next.high (bearish)

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
        if rng == 0 or body / rng < min_body_pct:
            continue

        prior  = recent.iloc[i - 1]
        nxt    = recent.iloc[i + 1]
        struct = recent.iloc[:i]
        if len(struct) < 3:
            continue
        swing_high = float(struct['high'].max())
        swing_low  = float(struct['low'].min())

        # Bullish displacement: strong up-close above structure that gapped up
        if (disp['close'] > disp['open']
                and disp['close'] > swing_high * (1 + clearance)
                and float(prior['high']) < float(nxt['low'])):
            return True, 'bullish', float(prior['high']), float(nxt['low']), swing_high

        # Bearish displacement: strong down-close below structure that gapped down
        if (disp['close'] < disp['open']
                and disp['close'] < swing_low * (1 - clearance)
                and float(prior['low']) > float(nxt['high'])):
            return True, 'bearish', float(nxt['high']), float(prior['low']), swing_low

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

def find_bullish_fvg(df, lookback=15):
    """
    Scans the last `lookback` bars for a bullish imbalance zone.
    Primary: true FVG (c1.low > c3.high — literal gap between wicks).
    Fallback: bullish order block (last bearish candle before a strong up move)
              which is the zone pros actually trade on intraday stock charts.
    Returns (found, bottom, top) where bottom < top is the entry zone.
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
        if rng == 0 or body / rng < 0.40:
            continue
        later  = df.iloc[i + 1: end + 1]
        breaks = later[later['close'] > candle['high']]
        if not breaks.empty:
            breakout_move = float(breaks.iloc[0]['close']) - float(candle['high'])
            if breakout_move >= body * 0.5:
                return True, float(candle['low']), float(candle['high'])

    return False, None, None

def find_bearish_fvg(df, lookback=15):
    """
    Scans the last `lookback` bars for a bearish imbalance zone.
    Primary: true FVG (gap down). Fallback: bearish order block.
    """
    if len(df) < 3:
        return False, None, None

    end   = len(df) - 1
    start = max(2, end - lookback)

    # 1. True FVG
    for i in range(end, start - 1, -1):
        c1, c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
        if c1['low'] > c3['high'] and c2['close'] < c2['open']:
            return True, float(c3['high']), float(c1['low'])

    # 2. Order block fallback — last bullish candle before a breakdown below its low.
    # OB candle body must be >40% of range, breakdown must have conviction.
    for i in range(end - 1, start - 1, -1):
        candle = df.iloc[i]
        if candle['close'] <= candle['open']:
            continue
        body = abs(candle['close'] - candle['open'])
        rng  = candle['high'] - candle['low']
        if rng == 0 or body / rng < 0.40:
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


def find_supply_zone(df_htf, current_price, min_distance_pct=0.001, max_distance_pct=0.12):
    """
    Scans the full 4H chart for supply zones (bearish imbalances) ABOVE current price.

    Checks in order of reliability:
    1. Unmitigated bearish FVG — genuine gap-down imbalance that hasn't been refilled
    2. Bearish OB — bullish candle before a confirmed breakdown (institutional selling)
    3. IFVG (Inverse FVG) — old bullish FVG that price has since filled; it flips to
       resistance on the next retest from below
    4. Bearish BREAKER — a bullish (demand) candle that price later CLOSED below, so the
       demand failed and the block flips polarity into resistance.

    max_distance_pct: ignore zones more than this % above price (default 12%)
                      prevents locking a supply zone $20k above BTC current price.

    Returns: (found: bool, zone_low: float, zone_high: float, zone_type: str)
    """
    if df_htf is None or len(df_htf) < 5:
        return False, 0.0, 0.0, ''

    bars = df_htf.reset_index(drop=True)
    n    = len(bars)
    min_price = current_price * (1 + min_distance_pct)
    max_price = current_price * (1 + max_distance_pct)
    candidates = []   # (zone_low, zone_high, zone_type)

    for i in range(2, n - 1):
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

    if not candidates:
        return False, 0.0, 0.0, ''

    # Nearest zone above price (lowest zone_low); tie-break by conviction
    # (a breaker = confirmed structural flip, higher conviction than a plain OB).
    _prio = {'bearish_breaker': 3, 'bearish_fvg': 2, 'ifvg': 1, 'bearish_ob': 0}
    candidates.sort(key=lambda x: (x[0], -_prio.get(x[2], 0)))
    z_lo, z_hi, z_type = candidates[0]
    return True, z_lo, z_hi, z_type


def find_demand_zone(df_htf, current_price, min_distance_pct=0.001, max_distance_pct=0.12):
    """
    Scans the full 4H chart for demand zones (bullish imbalances) BELOW current price.
    Mirror of find_supply_zone for LONG setups.

    1. Unmitigated bullish FVG below price
    2. Bullish OB — bearish candle before a strong breakout up
    3. IFVG support — old bearish FVG that price filled; flips to support
    4. Bullish BREAKER — a bearish (supply) candle that price later CLOSED above, so the
       supply failed and the block flips polarity into support.

    max_distance_pct: ignore zones more than this % below price (default 12%).

    Returns: (found: bool, zone_low: float, zone_high: float, zone_type: str)
    """
    if df_htf is None or len(df_htf) < 5:
        return False, 0.0, 0.0, ''

    bars = df_htf.reset_index(drop=True)
    n    = len(bars)
    max_price = current_price * (1 - min_distance_pct)
    min_price = current_price * (1 - max_distance_pct)
    candidates = []

    for i in range(2, n - 1):
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

    if not candidates:
        return False, 0.0, 0.0, ''

    # Nearest zone below price = highest zone_high; tie-break by conviction (breaker first).
    _prio = {'bullish_breaker': 3, 'bullish_fvg': 2, 'ifvg_support': 1, 'bullish_ob': 0}
    candidates.sort(key=lambda x: (-x[1], -_prio.get(x[2], 0)))
    z_lo, z_hi, z_type = candidates[0]
    return True, z_lo, z_hi, z_type

# ============================================================================
# CHART STRUCTURE — Flags, Channels
# ============================================================================

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
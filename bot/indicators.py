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

def find_support_resistance(df, lookback=50):
    """
    Identifies key support and resistance levels using recent swing points.
    Returns the two most recent significant levels.
    """
    if len(df) < lookback:
        return [], []
    
    recent = df.tail(lookback)
    highs = recent[recent['high'] == recent['high'].rolling(5, center=True).max()]['high'].unique()
    lows = recent[recent['low'] == recent['low'].rolling(5, center=True).min()]['low'].unique()
    
    resistance_levels = sorted(highs, reverse=True)[:3]
    support_levels = sorted(lows)[:3]
    
    return list(resistance_levels), list(support_levels)

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
        return False, None

    recent    = df.tail(lookback)
    structure = recent.iloc[:-3]
    if len(structure) < 5:
        return False, None

    swing_high = structure['high'].max()
    swing_low  = structure['low'].min()

    for i in range(-3, 0):
        candle = recent.iloc[i]
        body   = abs(candle['close'] - candle['open'])
        rng    = candle['high'] - candle['low']
        if rng == 0 or body / rng < 0.40:   # 40% body — real conviction without over-filtering
            continue
        if candle['close'] > swing_high * 1.0015:   # 0.15% clearance filters 1-tick false breaks
            return True, "bullish"
        if candle['close'] < swing_low  * 0.9985:
            return True, "bearish"

    return False, None

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
    for i in range(end, start - 1, -1):
        c1, c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
        if c1['low'] > c3['high'] and c2['close'] > c2['open']:
            return True, float(c3['high']), float(c1['low'])

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

    if not candidates:
        return False, 0.0, 0.0, ''

    # Nearest zone above price (lowest zone_low)
    candidates.sort(key=lambda x: x[0])
    z_lo, z_hi, z_type = candidates[0]
    return True, z_lo, z_hi, z_type


def find_demand_zone(df_htf, current_price, min_distance_pct=0.001, max_distance_pct=0.12):
    """
    Scans the full 4H chart for demand zones (bullish imbalances) BELOW current price.
    Mirror of find_supply_zone for LONG setups.

    1. Unmitigated bullish FVG below price
    2. Bullish OB — bearish candle before a strong breakout up
    3. IFVG support — old bearish FVG that price filled; flips to support

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

    if not candidates:
        return False, 0.0, 0.0, ''

    # Nearest zone below price (highest zone_high)
    candidates.sort(key=lambda x: x[2], reverse=True)
    z_lo, z_hi, z_type = candidates[0]
    return True, z_lo, z_hi, z_type

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
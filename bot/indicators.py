# bot/indicators.py
import pandas as pd
import numpy as np

# ============================================================================
# HIGHER TIMEFRAME (HTF) ANALYSIS - 4H Institutional Intent
# ============================================================================

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

def detect_displacement_bos(df, lookback=15, bos_window=10):
    """
    Detects a Break of Structure (bullish OR bearish) in the last bos_window candles.
    Bullish: any recent candle closed above the prior swing high with a strong body.
    Bearish: any recent candle closed below the prior swing low with a strong body.
    """
    if len(df) < lookback:
        return False, None

    recent = df.tail(lookback)

    for i in range(-bos_window, 0):
        candle = recent.iloc[i]
        prior = recent.iloc[:i] if i < -1 else recent.iloc[:-1]
        if len(prior) == 0:
            continue

        body_size = abs(candle['close'] - candle['open'])
        total_range = candle['high'] - candle['low']
        body_ratio = body_size / total_range if total_range > 0 else 0

        if body_ratio < 0.30:
            continue

        swing_high = prior['high'].max()
        swing_low = prior['low'].min()

        if candle['close'] > swing_high:
            return True, "bullish"
        if candle['close'] < swing_low:
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

def find_bullish_fvg(df):
    """
    Step 3 (Imbalance): Scans the last 3 completed candles to see 
    if a high-momentum gap was left behind by institutional buying.
    The FVG is the "Discount Box" where price will retrace to.
    """
    if len(df) < 3:
        return False, None, None
        
    c1 = df.iloc[-3]  # First candle in the sequence
    c2 = df.iloc[-2]  # The big momentum candle
    c3 = df.iloc[-1]  # Third candle in the sequence
    
    # Core FVG Condition: The low of Candle 1 is higher than the high of Candle 3
    if c1['low'] > c3['high'] and c2['close'] > c2['open']:
        fvg_bottom = c3['high']
        fvg_top = c1['low']
        return True, fvg_bottom, fvg_top
        
    return False, None, None

def find_bearish_fvg(df):
    """
    Bearish FVG: same gap structure as bullish but with a bearish displacement candle.
    c1 low > c3 high — gap down, c2 is bearish (the displacement).
    Price retracing UP into [c3 high, c1 low] is the short entry zone.
    """
    if len(df) < 3:
        return False, None, None

    c1 = df.iloc[-3]
    c2 = df.iloc[-2]
    c3 = df.iloc[-1]

    if c1['low'] > c3['high'] and c2['close'] < c2['open']:
        fvg_top    = c1['low']
        fvg_bottom = c3['high']
        return True, fvg_bottom, fvg_top

    return False, None, None

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
# Trading Bot System Design

## Overview

This is an institutional **Smart Money Concepts (SMC)** trading bot that trades crypto (24/7) and stocks (NYSE hours) using a four-step algorithmic entry strategy. It runs on simulated paper trading with realistic leverage, position sizing, and risk controls.

**Core principle:** Follow institutional smart money (accumulation, manipulation, distribution) rather than retail sentiment (moving averages, oscillators). Institutions hunt retail stops, create liquidity voids (FVGs), and rig daily bias (BOS). This bot detects those rigs and enters at the exact moment of institutional displacement.

---

## Smart Money Concepts (SMC) Psychology

### The Institutional Setup (4 Steps)

Every trade follows this universal institutional pattern:

#### 1. **Daily Bias (4-Hour Timeframe)**
Institutions choose a direction on the daily/4H chart to carry the day's volume. This is determined by a **Break of Structure (BOS)** — a candle that closes beyond the previous swing high or low.

- **BULLISH bias:** 4H candle closes above the prior swing high → market hunts **below** the swing low for retail stops
- **BEARISH bias:** 4H candle closes below the prior swing low → market hunts **above** the swing high for retail stops
- **No bias:** Consolidating; no clear direction yet

**Why:** Institutions need liquidity. Retail traders place protective stops at obvious swing levels. Institutions use directional bias as a map to where retail trapped capital sits.

#### 2. **Liquidity Sweep (5-Minute Timeframe)**
After bias is set, the market executes a wick below (bearish bias) or above (bullish bias) the relevant swing extreme to harvest those retail stops. This is the **sweep**. You see it as a sharp wick that rejects, then price reverses violently.

**Mechanics:**
- Count the number of **Equal Lows (EQL)** or **Equal Highs (EQH)** at a support/resistance level
- The more equals, the more retail stops are stacked there
- Institutions wick through, grab those stops as liquidity, then reverse
- The bot detects this sweep via the LTF (5-minute) wick pattern

**Psychology:** Retail sees a wick and thinks "the market is rejecting support/resistance here, I should short." Institutions just collected their liquidity.

#### 3. **Fair Value Gap (FVG) Lock**
After sweeping liquidity, institutions enter large positions. These create an **imbalance** on the candles — a gap where no trades occurred. This is a Fair Value Gap (FVG). The market has "left behind" price levels it will hunt to fill later.

**Example:**
- Sweep happens: large sellers appear, price drops 5%
- Next candle opens way below, gap down (bullish FVG from institutional short entry)
- The market will eventually **fill** this gap when institutions want to take profit

**Why:** Institutions mark their entry zone by leaving an imbalance. The FVG is their "bookmark."

#### 4. **Sniper Entry (10-Second Precision Loop)**
After institutions are positioned, price moves *away* from the zone (into the direction of bias). Retail gets excited and chases. Then institutions rig a **Change of Character (CHoCH)** — a fake break that reverses hard and re-enters the original FVG zone to shake out chasing retail.

**The sniper fires when:**
- Price re-enters the FVG zone (after the initial sweep and push away)
- A candle closes inside the zone with proper confirmation (bullish/bearish structure)
- The bot has pre-calculated SL (just outside the zone) and TP (risk × R:R)

**Why 10-second precision:** The entry window is tiny. A 5-minute cycle would miss the zone entirely. The bot arms (pre-calculates SL/TP/qty) on the 5-minute cycle, then a 10-second loop fires the entry the instant price taps the zone boundary. This fills at the exact institutional entry price, not 5+ minutes late.

---

## Entry & Exit Strategy

### State Machine (Per Symbol)

Each symbol follows a strict state progression:

```
IDLE
  ↓ [4H BOS detected + LTF setup]
SWEEP_HUNT
  ↓ [wick sweeps retail stops + FVG forms]
ENTRY_WAIT
  ↓ [sniper armed on first bar of zone wait]
POSITION_OPEN
  ↓ [entry triggered by 10-second loop]
[POSITION_OPEN state during hold, with scale-out logic]
  ↓ [SL or TP crossed, or stale timeout]
IDLE
```

### Entry Mechanics

**Sniper Arming (5-Minute Cycle):**
```python
when state == ENTRY_WAIT and bars_in_zone == 1:
    # Pre-calculate everything while we have the most recent data
    SL = zone_edge ± (ATR × SL_multiplier)
    SL = max(SL, structural_swing_guard)  # never place inside recent swing
    TP = entry + (risk × min_R_R)  # e.g. if risk=$5 and R:R=1:2, TP=$10
    qty = (position_margin × leverage) / entry_price
    
    if qty > dust_threshold and reward > min_reward_threshold:
        sniper_armed = True
        [log "🔫 Sniper armed"]
```

**Sniper Firing (10-Second Loop):**
```python
every 10 seconds while sniper_armed:
    current_price = exchange.fetch_ticker()
    
    if current_price enters FVG zone:
        # Fill at actual current price, not zone edge
        entry_fill = current_price
        
        # Sanity check: price can't be past its own SL/TP already
        if entry_fill is past SL or past TP:
            [abort trade — market moved too far]
            continue
        
        # Recalculate qty based on real fill
        qty = margin × leverage / entry_fill
        
        [place order]
        state = POSITION_OPEN
        entry_time = now
        entry_price = entry_fill
        [log "⚡ SNIPER ENTRY — [LONG/SHORT] (10-sec precision)"]
```

### Exit Mechanics

#### Scale-Out (50% at 50% to TP)
When price reaches halfway between entry and TP:
```python
if price has moved 50% of the way to TP:
    sell 50% of position at market
    [log "💰 SCALED OUT 50% @ $price  +$profit"]
    # remaining 50% continues to TP or SL
```

**Why:** Locks in profit on half the position. If the trade turns, you've already won on 50%. The remaining 50% still has full upside.

#### Profit Lock Trail (80% to TP)
When price reaches 80% of the way to TP:
```python
if price has moved 80% of the way to TP:
    risk = abs(entry_price - original_SL)
    move SL to entry ± (0.5 × risk)   # +0.5R profit lock
    [log "🛡 80% to target — SL trailed to +0.5R lock"]
    # a full reversal now still exits at a REAL profit, not a scratch
```

**History:** this used to be a break-even trail (entry + 0.1%) at 60% progress. Live ledger data showed it inverted the realized R:R — every retracing winner closed at ~+$1 while losers took the full -1R stop. The +0.5R lock at 80% keeps meaningful profit on near-miss trades.

#### SL/TP Exits
Checked every 10 seconds by the SL/TP watcher:
```python
if price closes candle at or past SL:
    [close remaining position at SL]
    pnl = negative
    state = IDLE
    
if price closes candle at or past TP:
    [close remaining position at TP]
    pnl = positive
    state = IDLE
```

**Note:** Uses candle *close*, not wick. Institutions use wicks to fake stops — they wick below SL, then close the candle back above. Only the close counts.

#### Stale Exit (12-Hour Timeout)
If a trade sits open for 12+ hours:
```python
if now - entry_time > 12 hours:
    [close position at market]
    [log "⏱️  STALE EXIT — 12h timeout"]
    state = IDLE
```

**Why:** Intraday SMC setups are designed for 5 min – 4 hour holds. A 12-hour-old zone isn't institutional anymore; it's cold.

---

## Risk Management

### Leverage & Position Sizing

**Crypto:** 10× simulated leverage
- margin_required = (entry_price × qty) / 10
- If entry = $100, qty = 10, margin = $100
- This simulates what you'd need for 10× leverage (real money)
- P&L scales as if you held 10× the nominal position

**Stocks:** 4× simulated leverage
- Same concept; more conservative for equity volatility

### Daily Risk Cap

**Per Symbol:**
- Risk = abs(entry - SL) × qty
- Max risk per trade = 2% of balance (crypto bot) or 0.5% (stock bot)

**Daily Total:**
- All symbols combined can risk max 3% of account per day
- Resets at midnight UTC
- If daily cap is hit, bot stops opening new positions until reset

### Per-Trade Risk Calculation

```python
risk_distance = abs(entry_price - stop_loss)
max_qty_for_risk = (account_balance × max_risk_pct) / risk_distance
qty = min(max_qty_for_risk, qty_from_margin_calc)
```

**Example (crypto):**
- Balance: $10,000 | Max risk: 2% = $200 per trade
- Entry: $100 | SL: $90 | Risk distance: $10
- Max qty = $200 / $10 = 20 units
- If margin calculation says we can afford 25 units, we cap at 20

---

## AMD Phase Scoring

**AMD** = Accumulation, Manipulation, Distribution. It's the phase of the institutional cycle.

### Accumulation
Institutions are *buying* quietly (on bears, no one cares):
- Price near the low of the range
- Volume below average (they don't want to push price up yet)
- Multiple liquidity sweeps lower (hunting retail shorts)
- **Entry quality:** Very good — you're buying where institutions started

### Manipulation
Institutions have accumulated and now *pump* to fake out retail:
- Price moves up sharply
- Retail gets excited, fomo buys at the top
- But institutions haven't sold yet
- **Entry quality:** Mediocre — you're late; institutions will soon distribute

### Distribution
Institutions are *selling* to retail buyers:
- Price near the high of the range
- Volume high (they're pushing out)
- Candles are topping out (shooting stars, dojis)
- **Entry quality:** Bad — you're on the wrong side

**Bot behavior:**
- Heavily favors Accumulation setups (AMD score `accumulation`)
- Approves Manipulation setups but flags them lower
- Skips Distribution phases when possible

---

## Orthogonal Technical Elements (Confirmations)

While SMC is the primary driver, the bot also tracks:

### Break of Structure (BOS)
A candle that closes beyond the prior swing. Signals a change in momentum. The bot resets confirmation timers when BOS occurs.

### Change of Character (CHoCH)
A shift in the pattern of candle wicks. If bullish candles suddenly reverse to bearish patterns, that's a CHoCH — often signals the end of a move.

### Trendline
Drawn from two pivot points. The bot draws trendlines on 4H/1H to visualize the HTF trend. TradingView shows this visually on the live chart.

### Equal Lows / Equal Highs (EQL / EQH)
Counts how many times price has tagged the same level without breaking it. More touches = more retail stops stacked there.

---

## Reliability & Edge Cases

### Orphan Position Guard
If the bot restarts while holding a position:
1. On startup, fetch all open positions from the broker/paper account
2. Compare against internal state machine
3. If position exists but state == IDLE, restore state = POSITION_OPEN with the tracked SL/TP
4. Resume monitoring that position immediately

**Why:** Network glitches, restarts, or power cycles might lose the in-memory state. This ensures no position trades without protection.

### Concurrent Process Dedup
If two bot instances start (old process + new process):
- Both try to log the same trade row to Google Sheets
- Sheets logger checks: does a row with this (Entry Time + Ticker) already exist in the last 10 rows?
- If yes, skip the duplicate write
- Prevents double-logging and sheet corruption

### Wicked SL/TP (Wick-Through Bug Fix)
**Old behavior:** If a wick touched TP/SL, the trade closed even if candle closed back above/below.
- Example: SHORT with SL=$100. Candle wicks down to $99.50 (touches SL), then closes at $101. Trade exited at a loss on the wick alone.

**New behavior:** Only candle *close* triggers exit.
- Example: Same scenario. Wick touches $99.50, but candle closes at $101. Trade remains open because candle close is above SL.

### Real Fill Price (Not Zone Edge)
**Old behavior:** On entry, filled the order at the zone *edge* price, not the actual live price.
- Example: Zone is $100–$105. Sniper fires when price reaches $100. Order placed at $100, but actual market price was $101. Paper position filled at $100, giving fake instant profit.

**New behavior:** Fill at the current live price (what the exchange actually traded).
- Example: Zone is $100–$105. Sniper fires when price is $101. Order fills at $101. Real market price.

### Missing Candle Data (Fallback)
If historical bars can't be fetched (exchange down, network error):
- Skip chart generation (log a warning)
- Trade still closes and logs to sheet with no chart image
- Fallback: next trade will try again; transient errors don't block trading

---

## Chart Generation & Logging

### Crypto Charts (Lightweight-Charts Engine)
1. **Local fallback:** If rendering or GitHub upload fail, save chart to `charts/` folder locally
2. **Coordinate engine:** Uses TradingView's exact price/time algorithms so lines are pixel-perfect to their scale
3. **Overlays:** Entry (blue dashed), Stop Loss (red), Take Profit (teal), entry marker (dotted vertical line)
4. **Retina resolution:** 2360×1120 pixels at 2× DPI for crisp rendering

### Stock Charts (TradingView Live Screenshots)
1. **Headless Chrome:** Loads real TradingView chart page (actual market data, not simulated)
2. **Interval auto-pick:** 5m for trades <4 hours, 15m for <24 hours, 1H for longer
3. **Coordinate mapping:** Uses TradingView's internal price/time scale APIs (no screenshots of our own lines — their lines on their data)
4. **Clickable formula:** Sheets cell uses `=HYPERLINK(url, IMAGE(url))` so inline thumbnail is clickable for full resolution

### Google Sheets Integration
- **Ledger columns:** Entry Time, Exit Time, Ticker, Side, Entry, SL, TP, Exit, Size, Margin, Notional, Leverage, P&L, Reason, Chart
- **Chart cell:** If URL is http(s), wraps in clickable HYPERLINK+IMAGE formula; if local path, stores as plain text
- **Duplicate guard:** Checks last 10 rows for same (Entry Time + Ticker) to prevent restart re-logs
- **Color scale format:** P&L column is conditional; white at $0, red for losses, green for profits

---

## Configuration & Watchlists

### Crypto (Binance via CCXT)
```python
BTC/USD, ETH/USD, SOL/USD, XRP/USD, AVAX/USD, DOGE/USD, POL/USD, ADA/USD
```

### Stock (Alpaca)
```python
AAPL, QQQ, SPY, NVDA, TSLA, GOOGL, META, MSFT
```

### Settings (config.py)
- `PAPER_LEVERAGE`: 10 (crypto) or 4 (stock)
- `SL_ATR_MULT`: 1.5 (stop loss = ATR × 1.5 from zone edge)
- `MIN_AI_RR`: 2.0 (minimum risk:reward — was 3.0, lowered because no trade ever reached a 3R target before management clipped it)
- `BINANCE_CASH_AT_RISK`: 2% (per-trade max risk)
- `STALE_TRADE_HOURS`: 6 (exit if open longer than 6 hours in crypto)

---

## Summary

This bot is built around **institutional behavior:** they run the same pattern on every timeframe and every pair. By detecting that pattern (bias → sweep → FVG → CHoCH re-entry), you catch the exact moment they accumulate. The 10-second sniper precision turns a 5-minute lag into a fill at their entry price, not 5 minutes late.

All exits are automated: scale-out locks profit, break-even trail removes risk, SL/TP close at the level, and stale timeout exits cold trades. Risk is capped daily and per-symbol so no single trade or bad session can blow the account.

The bot logs every trade to Google Sheets with real-time charts, making it easy to review what worked and what didn't.

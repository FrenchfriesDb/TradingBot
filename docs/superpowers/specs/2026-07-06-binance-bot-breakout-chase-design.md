# binance_bot.py: Breakout-Chase Entry Mode

## Context

`binance_bot.py`'s `process_symbol` only enters on a **retest**: it arms a zone
(FVG/AMD/OB), then waits for price to tap back into it with a confirming candle or
15m CHoCH. If price breaks out and never retraces — a clean V-shaped reversal that
melts straight up/down — the existing "zone abandoned" check
(`binance_bot.py:1218-1226`) fires once price runs ≥1.5% from the zone-arm price
without tapping, and unconditionally resets to IDLE. The bot never trades that move.

This was observed live: BTC swept a low, V-recovered, and ran straight through a
bullish FVG zone without ever retracing into it — daily-trend-driven re-arming
happened (a fresh bearish zone got set at a higher level), but the original bullish
continuation was never traded.

This spec adds a **breakout-chase** entry: when the abandon-check fires, check whether
the breakout still shows genuine continuation strength before giving up on it. If so,
enter directly instead of resetting.

## Goals

- Trade a subset of the moves that currently get fully skipped because they never
  retrace into a zone.
- Reuse the exact same risk-management tail (position sizing, liquidation guard,
  minimum-reward gate, AI confirmation) that retest entries already use, via an
  extraction — not a duplicate copy that can drift out of sync with the original.
- Make the higher risk of a worse average entry price visible in the code (half size)
  and in the logs/alerts (`🚀 CHASE LONG`/`🚀 CHASE SHORT` tags), not silent.

## Non-goals

- No change to the retest path's own confirmation logic (ATR gate, reversal veto,
  candle/CHoCH tap confirmation) — chase is a separate trigger with its own,
  simpler confirmation (momentum-intact check), not a relaxation of the retest rules.
- No new UI/chart changes.

## Design

### 1. New `SymbolState` field

`self.is_chase = False` added to `SymbolState.__init__` (`binance_bot.py:526-558`).
`reset()` already just calls `__init__()` again, so no separate reset logic needed.

### 2. Chase eligibility check (new pure function)

```python
def check_chase_continuation(df_ltf, bias):
    """Returns (eligible: bool, reason: str). Momentum must still be intact in the
    breakout direction — no reversal candle just printed, and price hasn't stalled."""
    is_long = bias == "BULLISH"
    last, prev = df_ltf.iloc[-1], df_ltf.iloc[-2]
    candle = indicators.classify_candle(last, prev)
    reversal_vs_long  = {"shooting_star", "gravestone_doji", "bearish_engulfing",
                         "hanging_man", "marubozu_bear"}
    reversal_vs_short = {"hammer", "dragonfly_doji", "bullish_engulfing",
                         "inverted_hammer", "marubozu_bull"}
    reversal_set = reversal_vs_long if is_long else reversal_vs_short
    no_reversal_candle = candle not in reversal_set

    close_now  = float(df_ltf['close'].iloc[-1])
    close_prev = float(df_ltf['close'].iloc[-3])
    momentum_intact = (close_now > close_prev) if is_long else (close_now < close_prev)

    if no_reversal_candle and momentum_intact:
        return True, f"candle={candle}, momentum intact"
    return False, f"candle={candle}, momentum_intact={momentum_intact}"
```

### 3. Hook: the "zone abandoned" check becomes a fork

Replace `binance_bot.py:1218-1226`:

```python
if state.zone_set_price:
    ran_away = ((state.bias == "BEARISH" and price < state.zone_set_price * 0.985) or
                (state.bias == "BULLISH" and price > state.zone_set_price * 1.015))
    if ran_away:
        moved = abs(price - state.zone_set_price) / state.zone_set_price
        chase_ok, chase_reason = check_chase_continuation(df_ltf, state.bias)
        if not chase_ok:
            print(f"[{base}] ⚠️ Zone abandoned — price ran {moved:.1%} from setup "
                  f"(${state.zone_set_price:,.2f}→${price:,.2f}) without tapping; "
                  f"re-hunting the move.")
            state.reset()
            return price

        print(f"[{base}] 🚀 Chasing breakout — price ran {moved:.1%} without tapping "
              f"(${state.zone_set_price:,.2f}→${price:,.2f}), momentum intact "
              f"({chase_reason}) — entering directly.")
        is_long = state.bias == "BULLISH"
        state.is_chase = True
        state.stop_loss = (float(df_ltf['low'].tail(SWING_LOOKBACK).min()) * 0.999 if is_long
                           else float(df_ltf['high'].tail(SWING_LOOKBACK).max()) * 1.001)
        risk_amt = abs(price - state.stop_loss)
        if risk_amt <= 0:
            print(f"[{base}] ⏭ Chase skipped — swing SL invalid (no room).")
            state.reset()
            return price
        _rng = df_htf['high'] - df_htf['low']
        entry_atr = max(_rng.rolling(14).mean().iloc[-1], _rng.rolling(3).mean().iloc[-1])
        execute_confirmed_entry(symbol, base, state, paper, is_long, price, risk_amt,
                                 df_ltf, df_htf, daily_trend, entry_atr, now, risk_fraction)
        return price
```

Note: no ATR "market dead" gate and no reversal veto for the chase path — both are
retest-specific checks that don't apply here (the market clearly isn't dead if it just
ran ≥1.5%, and the reversal veto exists to stop *fading* a fresh reclaim, which is the
opposite of what a same-direction chase does).

### 4. Extraction: `execute_confirmed_entry`

Everything from "find the nearest 4H liquidity pool" through the final trade
open/reject print (currently `binance_bot.py:1428-1547`, the tail of the retest path)
becomes a standalone function, called by **both** paths. `state.stop_loss` must
already be set by the caller — retest computes it via the existing zone-edge + ATR-cap
+ swing-guard-widen logic (`binance_bot.py:1376-1426`, unchanged), chase computes it
directly from the swing high/low (step 3 above).

```python
def execute_confirmed_entry(symbol, base, state, paper, is_long, price, risk_amt,
                             df_ltf, df_htf, daily_trend, entry_atr, now, risk_fraction):
    """Shared AI-confirmation + position-sizing + execution tail for both the
    retest-entry and breakout-chase paths. Caller must already have set
    state.stop_loss (and state.is_chase, if this is a chase entry)."""
    bias_str = "bullish" if is_long else "bearish"
    side_label = "LONG" if is_long else "SHORT"
    label = f"🚀 CHASE {side_label}" if state.is_chase else side_label

    pool_tp = indicators.find_next_liquidity_target(df_htf, price, bias_str)

    if state.is_chase:
        amd_phase = 'breakout_chase'
        zone_type = state.amd_zone_type
        ref_level = state.zone_set_price
        zone_lo, zone_hi = min(state.stop_loss, price), max(state.stop_loss, price)
    else:
        amd_phase = state.amd_phase
        zone_type = state.amd_zone_type
        ref_level = state.sweep_low or (state.fvg_high if not is_long else state.fvg_low)
        zone_lo, zone_hi = state.fvg_low, state.fvg_high

    confirm, rr_actual, ai_reason = get_ai_confirmation(
        symbol, price, daily_trend, bias_str,
        zone_lo, zone_hi, ref_level,
        state.stop_loss, risk_amt, pool_tp, df_ltf, df_htf,
        amd_phase=amd_phase, zone_type=zone_type,
    )

    reward      = risk_amt * rr_actual
    min_reward  = risk_amt * MIN_AI_RR
    max_offset  = max(0.0, reward - min_reward)
    tp_offset   = min(0.05 * entry_atr, 0.25 * reward, max_offset)
    state.take_profit = (price + reward - tp_offset if is_long
                         else price - reward + tp_offset)
    icon = "✅ YES" if confirm else "❌ NO"
    print(f"[{base}] 🤖 AI Bot Approval: {icon}  R:R=1:{rr_actual:.1f}  {ai_reason[:140]}")

    if not confirm:
        if state.is_chase:
            # Chasing is a one-shot, time-sensitive opportunity — unlike a retest zone
            # that stays valid to re-check next cycle, a rejected chase has no reason
            # to linger: price will only be further away next time. Reset immediately.
            print(f"[{base}] AI rejected chase entry — abandoning (no zone to keep watching).")
            state.reset()
        else:
            state.ai_reject_count += 1
            if state.ai_reject_count >= 3:
                print(f"[{base}] AI rejected setup {state.ai_reject_count}× — zone abandoned, resetting to IDLE.")
                state.reset()
            else:
                print(f"[{base}] AI rejected setup ({state.ai_reject_count}/3) — staying in ENTRY_WAIT")
        return

    effective_fraction = risk_fraction * 0.5 if (state.ranging_mode or state.is_chase) else risk_fraction
    # ... margin sizing, daily cap, liquidation guard, min-reward gate, buy/sell,
    #     trade_print + alert — all unchanged from the current retest-path tail,
    #     just relocated into this shared function and reading `label` for display.
```

(The full margin-sizing-through-alert body is the existing code at
`binance_bot.py:1456-1538`, relocated verbatim except for using the new `label`
variable in place of the old local `label` that the zone-edge SL section used to set.)

### 5. Call site update in the retest path

Where the retest path currently falls through from its confirmation gate
(`binance_bot.py:1370-1374`) into SL calc and then the old inline tail, it keeps the
existing zone-edge SL calc (`1376-1426`) unchanged, then calls:

```python
execute_confirmed_entry(symbol, base, state, paper, is_long, price, risk_amt,
                         df_ltf, df_htf, daily_trend, entry_atr, now, risk_fraction)
return price
```

`entry_atr` here is the value already computed earlier in the retest path's ATR gate
check (`binance_bot.py:1279-1281`) — no new computation needed, just passed through.

### 6. New AI-prompt branch for chase entries

`get_ai_confirmation` (`binance_bot.py:621`) gets one new `amd_phase` branch,
`'breakout_chase'`, alongside the existing `manipulation_up`/`manipulation_down`/
`trend_follow` branches:

```python
elif amd_phase == 'breakout_chase':
    amd_context = (
        f"AMD Phase    : BREAKOUT_CHASE (half size — momentum-continuation, no retest)\n"
        f"Narrative    : Price broke toward the {side} continuation and never retraced to\n"
        f"               tap the original zone — it ran away without giving a retest entry.\n"
        f"               This is a direct momentum-chase: no structural entry zone, the risk\n"
        f"               band below is the swing-based invalidation stop to current price.\n"
        f"               Original setup armed at ${sweep_level:,.4f}, now ${price:,.4f}.\n"
        f"               Chase risk band: ${fvg_low:,.4f} – ${fvg_high:,.4f}  SL: ${sl:,.4f}"
    )
    amd_question = (
        f"- Does the 4H chart show genuine fresh displacement/momentum still supporting "
        f"{side}, or does this look already extended/exhausted?\n"
        f"- Is the daily trend aligned with chasing this {side}?\n"
        f"- Is there still enough room to the next liquidity pool to justify a 1:3.5+ R:R "
        f"after chasing an already-extended move?"
    )
```

This gives the AI honest context (a real risk band, not a stale faraway zone) and
explicitly asks it to judge whether the move is already exhausted — the AI is the
backstop against chasing a top/bottom tick, on top of the momentum-intact check in
step 2.

## Risk summary (explicit, not hidden)

- Half size (`risk_fraction * 0.5`) — same mechanism already used for `ranging_mode`.
- Same `MIN_AI_RR` floor and minimum-reward gate as every other entry.
- Rejected chases abandon immediately rather than lingering in `ENTRY_WAIT`.
- Tagged `🚀 CHASE LONG`/`🚀 CHASE SHORT` everywhere (console, `trade_print`, `alert`)
  so these trades are never confused with retest entries when reviewing performance.

## Testing / Verification

- No automated test suite exists for `binance_bot.py` (matches its existing untested
  status — `process_symbol` talks directly to live exchange APIs).
- Manual verification: `python3 -m py_compile binance_bot.py`, then a manual review
  read-through confirming the retest path's behavior is byte-for-byte unchanged (same
  SL calc, same confirmation gate, same execution tail just relocated) — the safest
  check available given there's no existing harness to run this against, since the
  risk being managed here is regressing the already-working retest path while adding
  the new one.

from lumibot.strategies import Strategy
from lumibot.entities import Asset, Order
from bot import indicators
from finbert_utils import estimate_sentiment
from config import NVIDIA_API_KEY, API_KEY as ALPACA_API_KEY, API_SECRET as ALPACA_API_SECRET, BASE_URL as ALPACA_BASE_URL
import json
import logging
import os
import re
from datetime import datetime, timezone

STRATEGY_STATE_FILE = "strategy_state.json"

MIN_AI_RR = 2.0   # hard floor — 1:2 minimum keeps TP reachable intraday on 15m entries
MAX_AI_RR = 15.0  # sanity ceiling — guards against a hallucinated target

# ── Simulated leverage (paper perps / margin mode) ────────────────────────────
# 1  = pure spot / unlevered (default — Alpaca paper account, normal shares)
# 2  = Reg-T intraday margin (legal limit for US stock day-trading accounts)
# 4  = Pattern Day Trader intraday limit (US regulated, requires $25k account)
# 5+ = futures-style (ES/NQ micro contracts, or if porting to a futures broker)
# The bot multiplies qty by this factor; Alpaca paper account tracks the P&L
# on the full position naturally. A liquidation warning fires in logs if the
# loss would exceed the margin posted on a real leveraged account.
PAPER_LEVERAGE = 4   # change to 2, 3, or 4 to simulate leveraged returns


def get_ai_confirmation(symbol, price, daily_trend, bos_dir,
                        fvg_low, fvg_high, sweep_level,
                        sl, risk_amt, pool_tp, ltf_df, htf_df=None,
                        amd_phase=None, zone_type=None):
    """
    Asks Llama 3.3 70B (via NVIDIA API) whether this SMC setup is worth taking,
    and lets it pick the R:R target itself (we only enforce a 1:{MIN_AI_RR} floor).
    Returns (confirm: bool, rr: float, reason: str).
    Defaults to (True, MIN_AI_RR, ...) on any failure — an API hiccup should
    never block a trade, it just falls back to the minimum acceptable R:R.
    """
    if not NVIDIA_API_KEY:
        return True, MIN_AI_RR, "no_nvidia_key — proceeding at minimum 1:3.5 R:R"

    side = "LONG" if bos_dir == "bullish" else "SHORT"
    pool_line = (f"Nearest structural target: ${pool_tp:,.4f}  "
                 f"(implies 1:{abs(pool_tp - price) / risk_amt:.1f} R:R)"
                 if pool_tp else "Nearest structural target: none found")

    # Full 4H chart context — 20 candles so the AI can see the sweep, BOS, FVG, and AMD phase
    if htf_df is not None and len(htf_df) >= 5:
        htf_rows = htf_df.tail(20)
        swing_high = float(htf_df['high'].tail(50).max())
        swing_low  = float(htf_df['low'].tail(50).min())
        htf_block = "4H candles (oldest → newest):\n" + "\n".join(
            f"  {i+1:2d}. O:{r['open']:,.2f} H:{r['high']:,.2f} "
            f"L:{r['low']:,.2f} C:{r['close']:,.2f}"
            for i, (_, r) in enumerate(htf_rows.iterrows())
        ) + f"\n50-bar structure: Low ${swing_low:,.2f}  High ${swing_high:,.2f}"
    else:
        htf_block = "(4H data unavailable)"

    # Last 5 LTF candles for execution precision
    ltf_recent = ltf_df.tail(5)
    ltf_candles = "  ".join(
        f"O:{r['open']:.2f} H:{r['high']:.2f} L:{r['low']:.2f} C:{r['close']:.2f}"
        for _, r in ltf_recent.iterrows()
    )

    prompt = f"""You are an expert institutional SMC (Smart Money Concepts) trade analyst.

{htf_block}

SETUP SUMMARY:
Symbol       : {symbol}
Direction    : {side}
Current Price: ${price:,.4f}
Daily Trend  : {(daily_trend or 'UNCLEAR').upper()}
4H BOS       : {(bos_dir or 'NONE').upper()}
Swept level  : ${sweep_level:,.4f}  (liquidity grab)
FVG / OB zone: ${fvg_low:,.4f} – ${fvg_high:,.4f}  (entry zone, price is inside)
Stop Loss    : ${sl:,.4f}  (risk = ${risk_amt:,.4f} per share)
{pool_line}
Last 5 × 15m candles: {ltf_candles}

Using the full 4H chart above, analyze this SMC setup:
- Did a genuine liquidity sweep occur at the swept level?
- Is the FVG/OB entry zone structurally valid?
- Does AMD (Accumulation → Manipulation → Distribution) context support this {side}?
- How much room does price have before the next opposing liquidity pool?

Pick a risk:reward ratio of AT LEAST 3.5 — go higher only if structure genuinely supports it.

Reply in EXACTLY this format, one field per line, nothing else:
DECISION: YES or NO
RR: a number >= 3.5
REASON: one concise sentence"""

    try:
        from openai import OpenAI
        client = OpenAI(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key=NVIDIA_API_KEY,
        )
        resp = client.chat.completions.create(
            model="meta/llama-3.3-70b-instruct",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=120,
            timeout=15,
        )
        text = resp.choices[0].message.content.strip()

        decision_m = re.search(r"DECISION:\s*(YES|NO)", text, re.IGNORECASE)
        rr_m       = re.search(r"RR:\s*([\d.]+)", text, re.IGNORECASE)
        reason_m   = re.search(r"REASON:\s*(.+)", text, re.IGNORECASE | re.DOTALL)

        confirm = bool(decision_m) and decision_m.group(1).upper() == "YES"
        rr      = float(rr_m.group(1)) if rr_m else MIN_AI_RR
        rr      = max(MIN_AI_RR, min(rr, MAX_AI_RR))
        reason  = reason_m.group(1).strip() if reason_m else text

        if decision_m is None:
            reason = f"(unparsed AI response, defaulting approve) {text}"
            confirm = True

        return confirm, rr, reason
    except Exception as e:
        return True, MIN_AI_RR, f"API error ({e}) — proceeding at minimum 1:4 R:R"

if not os.path.exists("./logs"):
    os.makedirs("./logs")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("./logs/bot_activity.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

STALE_TRADE_HOURS = 6   # intraday SMC: close stale positions that haven't resolved in 6h


class DebbieLaSMC(Strategy):
    """
    Multi-asset Debbie-La Institutional Setup Bot.
    Each symbol runs an independent state machine with:
    - Time-based exit: closes stale trades after STALE_TRADE_HOURS
    - News-optional sentiment: proceeds if no news is available
    - Startup sync: re-syncs state from live positions on restart
    """

    def initialize(self, symbols: list = None, cash_at_risk: float = 0.03,
                   timeframe_htf: str = "4 hours", timeframe_ltf: str = "15 minutes"):
        self.symbols = symbols or ["AAPL", "QQQ", "SPY", "NVDA", "TSLA", "GOOGL"]
        self.sleeptime = "15M"
        self.timeframe_htf = timeframe_htf
        self.timeframe_ltf = timeframe_ltf
        self.cash_at_risk_per_symbol = cash_at_risk / len(self.symbols)

        self.state              = {s: "IDLE" for s in self.symbols}
        self.bias               = {s: None   for s in self.symbols}
        self.sweep_low          = {s: None   for s in self.symbols}
        self.sweep_high         = {s: None   for s in self.symbols}
        self.sweep_hunt_iter    = {s: 0      for s in self.symbols}
        self.fvg_low            = {s: None   for s in self.symbols}
        self.fvg_high           = {s: None   for s in self.symbols}
        self.fvg_set_iter       = {s: 0      for s in self.symbols}
        self.ote_zone           = {s: None   for s in self.symbols}
        self.entry_price        = {s: None   for s in self.symbols}
        self.entry_qty          = {s: 0      for s in self.symbols}
        self.stop_loss          = {s: None   for s in self.symbols}
        self.take_profit        = {s: None   for s in self.symbols}
        self.entry_time         = {s: None   for s in self.symbols}
        self.sl_order           = {s: None   for s in self.symbols}
        self.tp_order           = {s: None   for s in self.symbols}
        self.oco_ids            = {s: []     for s in self.symbols}  # broker OCO order IDs to cancel on reset
        self.bracket_active     = {s: False  for s in self.symbols}  # True when entry went in as a bracket
        self.entry_order        = {s: None   for s in self.symbols}
        self.ranging_mode       = {s: False  for s in self.symbols}
        self.amd_phase          = {s: None   for s in self.symbols}
        self.amd_zone_type      = {s: None   for s in self.symbols}
        self.zone_set_price     = {s: None   for s in self.symbols}  # price when trend_follow zone armed
        self._iter_count        = 0

    def on_bot_start(self):
        """Kill the two stale BRACKET orders that spam WARNING every iteration."""
        stale_ids = [
            "cea85ee0-f74f-40b5-a9ac-a5740c5adfdb",
            "e6e0b8c0-1d28-4056-8069-62b6bd0ce8ab",
        ]
        try:
            from alpaca.trading.client import TradingClient
            client = TradingClient(ALPACA_API_KEY, ALPACA_API_SECRET, paper=True)
            for oid in stale_ids:
                try:
                    client.cancel_order_by_id(oid)
                    self.log_message(f"Cancelled stale order {oid}", color="cyan")
                except Exception as e:
                    self.log_message(f"Stale order {oid} already gone: {e}", color="yellow")
        except Exception as e:
            self.log_message(f"Stale order cleanup skipped: {e}", color="yellow")

    def _save_state(self):
        """Persist SL/TP for every open position so restarts don't lose protection."""
        try:
            data = {}
            for s in self.symbols:
                if self.state[s] == "POSITION_OPEN":
                    _qty   = self.entry_qty.get(s) or 0
                    _entry = self.entry_price[s] or 0
                    data[s] = {
                        "state":       self.state[s],
                        "bias":        self.bias[s],
                        "entry_price": _entry,
                        "stop_loss":   self.stop_loss[s],
                        "take_profit": self.take_profit[s],
                        "entry_time":  (self.entry_time[s].isoformat()
                                        if self.entry_time[s] else None),
                        "oco_ids":     self.oco_ids.get(s, []),
                        "margin":      abs(_qty) * _entry / PAPER_LEVERAGE if _entry else None,
                        "leverage":    PAPER_LEVERAGE,
                    }
            with open(STRATEGY_STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.log_message(f"[STATE] save failed: {e}", color="red")

    def _load_state(self):
        """Restore SL/TP from last save so Python-side monitoring resumes correctly."""
        if not os.path.exists(STRATEGY_STATE_FILE):
            return
        try:
            with open(STRATEGY_STATE_FILE) as f:
                data = json.load(f)
            for s, saved in data.items():
                if s not in self.symbols:
                    continue
                self.state[s]       = saved.get("state", "IDLE")
                self.bias[s]        = saved.get("bias")
                self.entry_price[s] = saved.get("entry_price")
                self.stop_loss[s]   = saved.get("stop_loss")
                self.take_profit[s] = saved.get("take_profit")
                self.oco_ids[s]     = saved.get("oco_ids", [])
                raw_time            = saved.get("entry_time")
                self.entry_time[s]  = (datetime.fromisoformat(raw_time)
                                       if raw_time else None)
                _mgn = saved.get("margin")
                _ep  = saved.get("entry_price")
                _lev = saved.get("leverage", PAPER_LEVERAGE)
                if _mgn and _ep:
                    self.entry_qty[s] = int(_mgn * _lev / _ep)
                if self.state[s] == "POSITION_OPEN":
                    self.log_message(
                        f"[{s}] Restored POSITION_OPEN  SL={self.stop_loss[s]}  TP={self.take_profit[s]}",
                        color="yellow"
                    )
        except Exception as e:
            self.log_message(f"[STATE] load failed: {e}", color="red")

    def before_starting_trading(self):
        """Re-sync state from live positions and saved SL/TP on startup.
        Also detects overnight gaps that already violated the SL (the broker stop
        should have fired, but covers the case where it didn't or the bot restarts
        mid-gap before the fill clears)."""
        self._load_state()
        try:
            for symbol in self.symbols:
                asset    = self._make_asset(symbol)
                position = self.get_position(asset)
                if position is not None and self.state[symbol] != "POSITION_OPEN":
                    self.state[symbol]      = "POSITION_OPEN"
                    self.entry_time[symbol] = datetime.now(timezone.utc)
                    self.log_message(
                        f"[{symbol}] Startup sync: live position found → POSITION_OPEN "
                        f"SL={self.stop_loss[symbol]}  TP={self.take_profit[symbol]}",
                        color="yellow"
                    )
                elif position is None and self.state[symbol] == "POSITION_OPEN":
                    # Position closed while bot was offline (broker stop fired) — clean up
                    self._reset(symbol)
                    continue

                # ── Gap-open check ────────────────────────────────────────────
                # If the position survived overnight but the open price has already
                # blown past our SL (e.g., broker stop fill delayed or bot restarted
                # mid-gap), close immediately at market rather than ride it further.
                if self.state[symbol] == "POSITION_OPEN" and position is not None:
                    sl  = self.stop_loss.get(symbol)
                    is_long = float(position.quantity) > 0
                    try:
                        bar = self.get_last_price(asset)
                        open_price = float(bar) if bar else None
                    except Exception:
                        open_price = None
                    if sl and open_price:
                        gap_violated = (is_long and open_price < sl) or (not is_long and open_price > sl)
                        if gap_violated:
                            self.log_message(
                                f"[{symbol}] ⚠️ GAP OPEN: open ${open_price:.2f} has blown past "
                                f"SL ${sl:.2f} — closing immediately at market.",
                                color="red")
                            self._cancel_oco(symbol)
                            close_qty  = abs(float(position.quantity))
                            close_side = (Order.OrderSide.BUY if not is_long
                                          else Order.OrderSide.SELL)
                            self.submit_order(self.create_order(asset, close_qty, close_side))
                            self._reset(symbol)
        except Exception as e:
            self.log_message(f"Startup sync skipped (broker not ready): {e}", color="red")

    def _cancel_oco(self, symbol):
        """Cancel the broker-side OCO legs we posted for this symbol so they never
        linger as orphans (which would later trigger 'potential wash trade' rejections).
        IDs are captured in on_filled_order from the raw Alpaca REST response."""
        ids = self.oco_ids.get(symbol) or []
        if not ids:
            return
        try:
            import requests as _req
            for oid in ids:
                try:
                    _req.delete(
                        f"{ALPACA_BASE_URL}/v2/orders/{oid}",
                        headers={
                            "APCA-API-KEY-ID":     ALPACA_API_KEY,
                            "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
                        },
                        timeout=10,
                    )
                except Exception:
                    pass  # already filled/cancelled → 404/422, safe to ignore
            self.log_message(f"[{symbol}] 🧹 Cancelled {len(ids)} broker OCO order(s).", color="cyan")
        finally:
            self.oco_ids[symbol] = []

    def _ensure_protection(self, symbol, position, is_long, sl, tp):
        """Guarantee a live position always has a broker-side stop. If none is found on the
        book (legs expired at the close, were cancelled, or lost across a restart), re-post a
        GTC OCO so the position is never left naked overnight."""
        if not (sl and tp):
            return
        hdr = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_API_SECRET}
        try:
            import requests as _req
            r = _req.get(f"{ALPACA_BASE_URL}/v2/orders",
                         params={"status": "open", "symbols": symbol, "nested": "true"},
                         headers=hdr, timeout=10)
            orders = r.json() if r.status_code == 200 else []
            def _has_stop(o):
                return bool(o.get("stop_price")) or any(l.get("stop_price") for l in (o.get("legs") or []))
            if any(_has_stop(o) for o in orders):
                return  # already protected — nothing to do

            qty = int(abs(position.quantity))
            if qty <= 0:
                return
            body = {"symbol": symbol, "qty": str(qty),
                    "side": "sell" if is_long else "buy", "type": "limit",
                    "time_in_force": "gtc", "order_class": "oco",
                    "take_profit": {"limit_price": str(round(tp, 2))},
                    "stop_loss":   {"stop_price":  str(round(sl, 2))}}
            resp = _req.post(f"{ALPACA_BASE_URL}/v2/orders", json=body, headers=hdr, timeout=10)
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            ids = [data["id"]] if data.get("id") else []
            ids += [l["id"] for l in (data.get("legs") or []) if l.get("id")]
            self.oco_ids[symbol]      = ids
            self.bracket_active[symbol] = False
            self.log_message(f"[{symbol}] 🛡 Re-attached protection — position was NAKED, "
                             f"posted GTC OCO (SL {sl:.2f} / TP {tp:.2f}).", color="yellow")
        except Exception as e:
            self.log_message(f"[{symbol}] ⚠️ Couldn't verify/re-attach protection: {e}", color="red")

    def _reset(self, symbol):
        # Cancel the broker-side OCO/bracket legs (TP/SL) so they don't linger as orphans
        self._cancel_oco(symbol)
        self.bracket_active[symbol] = False

        # Legacy Lumibot-tracked SL/TP orders (vestigial — OCO is posted via REST now)
        for order_attr in ("sl_order", "tp_order"):
            order = getattr(self, order_attr).get(symbol)
            if order is not None:
                try:
                    self.cancel_order(order)
                except Exception:
                    pass
                getattr(self, order_attr)[symbol] = None

        self.entry_order[symbol]     = None
        self.state[symbol]           = "IDLE"
        self.bias[symbol]            = None
        self.sweep_low[symbol]       = None
        self.sweep_high[symbol]      = None
        self.sweep_hunt_iter[symbol] = 0
        self.fvg_low[symbol]         = None
        self.fvg_high[symbol]        = None
        self.fvg_set_iter[symbol]    = 0
        self.ote_zone[symbol]        = None
        self.entry_price[symbol]     = None
        self.stop_loss[symbol]       = None
        self.take_profit[symbol]     = None
        self.entry_time[symbol]      = None
        self.ranging_mode[symbol]    = False
        self.amd_phase[symbol]       = None
        self.amd_zone_type[symbol]   = None
        self.zone_set_price[symbol]  = None
        self._save_state()

    def _make_asset(self, symbol):
        """Override in subclasses to change asset type (e.g. CRYPTO)."""
        return Asset(symbol, asset_type=Asset.AssetType.STOCK)

    def position_sizing(self, symbol, sl_price=None):
        """
        Risk-based sizing: risk exactly cash_at_risk_per_symbol of account on this trade.
        quantity = risk_dollars / sl_distance
        Caps at 20% of cash so one trade can never blow the account.
        Falls back to cash-allocation if no SL is available yet.
        """
        cash = self.get_cash()
        last_price = self.get_last_price(symbol)
        if not last_price:
            return cash, last_price, 0

        if sl_price and abs(last_price - sl_price) > 0:
            risk_dollars = cash * self.cash_at_risk_per_symbol
            sl_distance  = abs(last_price - sl_price)
            quantity     = risk_dollars / sl_distance
        else:
            quantity = (cash * self.cash_at_risk_per_symbol) / last_price

        # Leverage: multiply controlled qty while margin posted = position/leverage.
        # Max-qty cap applies to margin, not to the full leveraged position, so the
        # cap stays sane regardless of leverage level.
        max_qty  = (cash * 0.20) / last_price   # 20% of cash as margin per trade
        quantity = max(0, round(min(quantity, max_qty), 0))
        quantity = int(quantity * PAPER_LEVERAGE)
        if PAPER_LEVERAGE > 1 and quantity > 0:
            margin    = quantity * last_price / PAPER_LEVERAGE
            self.log_message(
                f"[leverage] {PAPER_LEVERAGE}x — controlling {quantity} shares "
                f"(${quantity*last_price:,.0f}) on ${margin:,.0f} margin. "
                f"Liq if price moves {1/PAPER_LEVERAGE:.0%} against.",
                color="yellow")
        return cash, last_price, quantity

    def get_daily_trend(self, symbol):
        """Daily EMA20 vs EMA50 alignment — must agree with the 4H BOS or we skip."""
        asset = self._make_asset(symbol)
        try:
            bars = self.get_historical_prices(asset, 100, "1 day")
            if bars is None:
                return None
            return indicators.get_daily_trend(bars.pandas_df)
        except Exception as e:
            self.log_message(f"[{symbol}] Daily trend error: {e}")
            return None

    def get_htf_bias(self, symbol):
        asset = self._make_asset(symbol)
        try:
            bars    = self.get_historical_prices(asset, 200, self.timeframe_htf)
            bars_1h = self.get_historical_prices(asset, 200, "1 hour")
            if bars is None:
                return None
            df    = bars.pandas_df
            df_1h = bars_1h.pandas_df if bars_1h is not None else None

            is_consolidating = indicators.detect_consolidation(df, lookback=20)

            # Dual HTF BOS: 4H = full conviction, 1H = faster signal at half size
            is_bos_4h, direction_4h, bos_lvl_4h = indicators.detect_displacement_bos(df,    lookback=15)
            is_bos_1h, direction_1h, bos_lvl_1h = (
                indicators.detect_displacement_bos(df_1h, lookback=15)
                if df_1h is not None else (False, None, None)
            )
            is_bos    = is_bos_4h or is_bos_1h
            direction = direction_4h if is_bos_4h else direction_1h
            bos_tf    = "4H" if is_bos_4h else ("1H" if is_bos_1h else "—")
            bos_level = bos_lvl_4h  if is_bos_4h else bos_lvl_1h
            is_1h_only = is_bos_1h and not is_bos_4h

            # Multi-touch S/R clustering — only levels price has revisited ≥2×
            resistance, support = indicators.find_support_resistance(df, lookback=50)
            all_sr = resistance + support

            # Is the BOS displacement candle cutting through a known S/R level?
            bos_near_sr, bos_sr_level = (
                indicators.is_near_sr_level(bos_level, all_sr)
                if bos_level else (False, None)
            )

            is_sweep_htf, _, sweep_wick_htf = indicators.check_liquidity_sweep(df, sweep_window=5)

            # Full AMD cycle: accumulation box → manipulation spike → distribution
            amd_phase, amd_info = indicators.detect_amd_phase(df)

            # Flag and channel structure on the HTF chart
            bull_flag, *_bfd = indicators.detect_bull_flag(df)
            bear_flag, *_brd = indicators.detect_bear_flag(df)
            channel, channel_slope = indicators.detect_channel(df)

            # ATR for consolidation guard (stocks: 0.4% threshold vs crypto's 2.5%)
            atr_14  = (df['high'] - df['low']).rolling(14).mean().iloc[-1]
            atr_pct = float(atr_14 / df['close'].iloc[-1]) if df['close'].iloc[-1] else 0.0

            return {
                "consolidating":  is_consolidating,
                "bos":            is_bos,
                "bos_4h":         is_bos_4h,
                "bos_1h":         is_bos_1h,
                "is_1h_only":     is_1h_only,
                "bos_tf":         bos_tf,
                "direction":      direction,
                "bos_level":      bos_level,
                "bos_near_sr":    bos_near_sr,
                "bos_sr_level":   bos_sr_level,
                "resistance":     resistance,
                "support":        support,
                "sweep_htf":      is_sweep_htf,
                "sweep_wick_htf": sweep_wick_htf,
                "amd_phase":      amd_phase,
                "amd_info":       amd_info,
                "bull_flag":      bull_flag,
                "bear_flag":      bear_flag,
                "channel":        channel,
                "channel_slope":  channel_slope,
                "atr_pct":        atr_pct,
                "df":             df,
            }
        except Exception as e:
            self.log_message(f"[{symbol}] HTF error: {e}")
            return None

    def get_ltf_technicals(self, symbol):
        asset = self._make_asset(symbol)
        try:
            bars = self.get_historical_prices(asset, 50, self.timeframe_ltf)
            if bars is None:
                return None
            df = bars.pandas_df
            is_sweep, support_level, sweep_wick_low = indicators.check_liquidity_sweep(df)
            is_mss, swing_high_broken = indicators.check_market_structure_shift(df)
            is_fvg_bull, fvg_bottom, fvg_top = indicators.find_bullish_fvg(df)
            is_fvg_bear, fvg_bear_bottom, fvg_bear_top = indicators.find_bearish_fvg(df)
            is_choch, choch_direction = indicators.detect_choch(df, lookback=5)
            return {
                "sweep": is_sweep, "sweep_wick_low": sweep_wick_low,
                "support_level": support_level,
                "mss": is_mss, "swing_high": swing_high_broken,
                "fvg_bull": is_fvg_bull, "fvg_bottom": fvg_bottom, "fvg_top": fvg_top,
                "fvg_bear": is_fvg_bear, "fvg_bear_bottom": fvg_bear_bottom,
                "fvg_bear_top": fvg_bear_top,
                "choch": is_choch, "choch_direction": choch_direction,
                "df": df,
            }
        except Exception as e:
            self.log_message(f"[{symbol}] LTF error: {e}")
            return None

    def _get_sentiment(self, symbol):
        """
        Returns (confirm: bool, label: str, prob: float).
        Always returns True if no news is available — avoids the "no news = no trade" trap.
        """
        try:
            news_items = self.get_news(symbol)
        except Exception:
            news_items = None

        if not news_items:
            return True, "no_news", 0.0

        probability, sentiment = estimate_sentiment(news_items)
        confirm = not (sentiment == "negative" and probability >= 0.60)
        return confirm, sentiment, probability

    def _process_symbol(self, symbol):
        current_price = self.get_last_price(symbol)
        if not current_price:
            return
        asset = self._make_asset(symbol)
        position = self.get_position(asset)

        # ── STATE 4: POSITION_OPEN ─────────────────────────────────────────────
        # Check this first so we don't re-enter while a trade is live.
        if self.state[symbol] == "POSITION_OPEN":
            if position is None:
                self.log_message(f"[{symbol}] Position closed. Resetting.", color="cyan")
                self._reset(symbol)
                return

            is_long = self.bias[symbol] == "BULLISH"
            sl = self.stop_loss[symbol]
            tp = self.take_profit[symbol]

            # SAFETY NET: a live position must ALWAYS have a broker-side stop. If the protective
            # orders vanished (legs expired at the close, cancelled, or lost across a restart),
            # re-attach a fresh GTC OCO. Without this, a held position can sit naked overnight.
            self._ensure_protection(symbol, position, is_long, sl, tp)

            # Manual SL/TP exit — Python-side check so we don't rely on Alpaca bracket parsing
            hit = None
            if sl and tp:
                if is_long and current_price <= sl:
                    hit = "SL"
                elif is_long and current_price >= tp:
                    hit = "TP"
                elif not is_long and current_price >= sl:
                    hit = "SL"
                elif not is_long and current_price <= tp:
                    hit = "TP"

            if hit:
                close_qty  = abs(position.quantity)
                close_side = Order.OrderSide.SELL if is_long else Order.OrderSide.BUY
                self.submit_order(self.create_order(asset, close_qty, close_side))
                entry_p = self.entry_price[symbol] or current_price
                pnl = ((current_price - entry_p) * close_qty if is_long
                       else (entry_p - current_price) * close_qty)
                lev_note = ""
                if PAPER_LEVERAGE > 1:
                    margin = close_qty * entry_p / PAPER_LEVERAGE
                    if pnl < 0 and abs(pnl) >= margin:
                        lev_note = f"  ⚡ LIQUIDATED at {PAPER_LEVERAGE}x (loss > margin ${margin:,.0f})"
                    else:
                        lev_note = f"  [{PAPER_LEVERAGE}x leveraged — unlevered P&L: ${pnl/PAPER_LEVERAGE:+.2f}]"
                self.log_message(
                    f"[{symbol}] {'🟢' if pnl >= 0 else '🔴'} {hit} hit "
                    f"@ {current_price:.4f} | P&L: {pnl:+.2f}{lev_note}",
                    color="green" if pnl >= 0 else "red"
                )
                self._reset(symbol)  # also cancels broker SL/TP orders + saves state
                return

            # Time-based exit: close stale trades after STALE_TRADE_HOURS
            if self.entry_time[symbol]:
                now = datetime.now(timezone.utc)
                entry = self.entry_time[symbol]
                if entry.tzinfo is None:
                    entry = entry.replace(tzinfo=timezone.utc)
                elapsed_hours = (now - entry).total_seconds() / 3600
                if elapsed_hours >= STALE_TRADE_HOURS:
                    close_qty  = abs(position.quantity)
                    close_side = Order.OrderSide.SELL if is_long else Order.OrderSide.BUY
                    self.submit_order(self.create_order(asset, close_qty, close_side))
                    self.log_message(
                        f"[{symbol}] ⏰ Stale exit after {elapsed_hours:.1f}h — closing position.",
                        color="red"
                    )
                    self._reset(symbol)
            return

        # ── FETCH DATA ─────────────────────────────────────────────────────────
        htf = self.get_htf_bias(symbol)
        if htf is None:
            return
        ltf = self.get_ltf_technicals(symbol)
        if ltf is None:
            return
        daily_trend = self.get_daily_trend(symbol)

        zone_tag = ""
        if self.state[symbol] == "ENTRY_WAIT" and self.fvg_low[symbol] and self.fvg_high[symbol]:
            tag = "AMD" if self.amd_phase[symbol] else "FVG"
            in_zone = self.fvg_low[symbol] <= current_price <= self.fvg_high[symbol]
            zone_tag = (f"  [{tag} {self.fvg_low[symbol]:.2f}–{self.fvg_high[symbol]:.2f} "
                        f"{'✅IN' if in_zone else '⏳waiting'}]")
        candle_type = indicators.classify_candle(ltf["df"].iloc[-1], ltf["df"].iloc[-2])
        ch = htf.get("channel")
        struct_tag = (f"▲ch({ch})" if ch == "ascending"
                      else f"▼ch({ch})" if ch == "descending"
                      else ("🚩bull_flag" if htf.get("bull_flag")
                            else ("🚩bear_flag" if htf.get("bear_flag") else "—")))
        amd_tag = htf.get("amd_phase", "—")
        sr_tag  = (f"SR✅{htf['bos_sr_level']:.2f}" if htf.get("bos_near_sr")
                   else f"res={htf['resistance'][0]:.2f}" if htf.get("resistance") else "—")
        self.log_message(
            f"[{symbol}] state={self.state[symbol]} price={current_price:.4f} "
            f"daily={daily_trend} bos={htf['bos']}({htf['direction']}/{htf['bos_tf']}) "
            f"amd={amd_tag}  struct={struct_tag}  sr={sr_tag}  "
            f"sweep={ltf['sweep']} fvg_bull={ltf['fvg_bull']} fvg_bear={ltf['fvg_bear']}  "
            f"candle={candle_type}"
            f"{zone_tag}"
        )

        # ── STATE 1: IDLE — daily trend + 4H BOS must agree ──────────────────────
        AMD_ENTRY_WAIT_ITERS = 96  # AMD zones can take many iterations to reach
        MIN_ATR_PCT = 0.004       # 0.4% 4H ATR floor for stocks — filters flat/dead days
        if self.state[symbol] == "IDLE" and position is None:
            atr_pct = htf.get("atr_pct", 1.0)

            # ── FIX #2: Accumulation guard runs FIRST, blocking every path below ──────
            # A tight HTF range = chop / institutions accumulating. Don't trade either
            # side until it breaks. Without this first, a BOS printed inside the range
            # whipsaws us. Log the range and bail for this iteration.
            if htf.get("amd_phase") == "accumulation":
                ai = htf.get("amd_info", {})
                self.log_message(
                    f"[{symbol}] 📦 Accumulation: {ai.get('range_low', 0):.2f}–"
                    f"{ai.get('range_high', 0):.2f} ({ai.get('range_pct', 0):.1%} wide) "
                    f"— chop, no entry until breakout/sweep", color="cyan"
                )
                return

            # ── FIX #1: BOS handling — trade the displacement retest, don't demand a sweep ──
            # When a BOS leaves a displacement FVG, lock that gap and go straight to
            # ENTRY_WAIT for the retest. This is how clean breakdowns/breakouts get traded:
            # an impulsive move has no opposing sweep, so requiring one made the bot blind
            # to its best continuation setups. Fall back to SWEEP_HUNT only when no gap.
            bos_aligned = htf["bos"] and htf["direction"] and (
                daily_trend == htf["direction"] or daily_trend is None
            )
            bos_counter = (htf["bos"] and htf["direction"] and daily_trend
                           and daily_trend != htf["direction"])

            if bos_aligned and atr_pct < MIN_ATR_PCT:
                self.log_message(
                    f"[{symbol}] BOS skip — flat market (ATR={atr_pct:.2%} < {MIN_ATR_PCT:.2%})",
                    color="yellow"
                )
            elif bos_aligned:
                direction = htf["direction"]
                self.bias[symbol]         = direction.upper()
                self.ranging_mode[symbol] = (daily_trend is None) or htf["is_1h_only"]
                size_tag = "HALF" if self.ranging_mode[symbol] else "FULL"
                sr_conf  = (f"  ✅ cuts S/R @ {htf['bos_sr_level']:.2f}"
                            if htf.get("bos_near_sr") else "")
                ch_tag   = f"  [{htf['channel']} channel]" if htf.get("channel") else ""
                color    = "green" if direction == "bullish" else "red"
                d_found, d_dir, d_lo, d_hi, _ = indicators.detect_displacement_fvg(htf["df"])
                if d_found and d_dir == direction:
                    self.fvg_low[symbol]       = d_lo
                    self.fvg_high[symbol]      = d_hi
                    self.amd_zone_type[symbol] = "choch_fvg"     # displacement gap = the CHoCH zone
                    self.state[symbol]         = "ENTRY_WAIT"
                    self.fvg_set_iter[symbol]  = self._iter_count
                    self.log_message(
                        f"[{symbol}] STEP 1+2: {htf['bos_tf']} BOS ({direction}) + displacement FVG "
                        f"{d_lo:.2f}–{d_hi:.2f} → ENTRY_WAIT retest [{size_tag}]{sr_conf}{ch_tag}",
                        color=color
                    )
                else:
                    self.state[symbol]           = "SWEEP_HUNT"
                    self.sweep_hunt_iter[symbol] = self._iter_count
                    self.log_message(
                        f"[{symbol}] STEP 1: {htf['bos_tf']} BOS ({direction}) → "
                        f"hunting sweep [{size_tag}]{sr_conf}{ch_tag}", color=color
                    )
            elif bos_counter:
                self.log_message(
                    f"[{symbol}] {htf['bos_tf']} BOS {htf['direction']} skipped — "
                    f"daily trend is {daily_trend}.", color="yellow"
                )

            # ── AMD cycle: use detect_amd_phase() for full A→M→D awareness ────────
            # (Accumulation already filtered at the top of IDLE — returns early — so any
            #  symbol reaching here is NOT in a tight range.)
            if self.state[symbol] == "IDLE":
                amd_phase = htf.get("amd_phase", "unknown")
                amd_info  = htf.get("amd_info", {})

                if atr_pct >= MIN_ATR_PCT:
                    # manipulation_up = stop hunt of lows completed, price bleeding up
                    # → real move will be DOWN — SHORT from supply zone above
                    if amd_phase == "manipulation_up" and daily_trend == "bearish":
                        found, sup_lo, sup_hi, sup_type = indicators.find_supply_zone(
                            htf["df"], current_price
                        )
                        if found:
                            self.bias[symbol]          = "BEARISH"
                            self.sweep_low[symbol]     = amd_info.get("sweep_wick", htf.get("sweep_wick_htf"))
                            self.amd_phase[symbol]     = "manipulation_up"
                            self.amd_zone_type[symbol] = sup_type
                            self.fvg_low[symbol]       = sup_lo
                            self.fvg_high[symbol]      = sup_hi
                            self.ranging_mode[symbol]  = False
                            self.state[symbol]         = "ENTRY_WAIT"
                            self.fvg_set_iter[symbol]  = self._iter_count
                            self.log_message(
                                f"[{symbol}] 🎯 AMD manipulation_up: swept ${amd_info.get('swept_level', 0):.2f} → "
                                f"SHORT from [{sup_type}] {sup_lo:.2f}–{sup_hi:.2f}  "
                                f"target≈${amd_info.get('manipulation_target', 0):.2f}",
                                color="red"
                            )

                    # manipulation_down = stop hunt of highs completed, price pushed down
                    # → real move will be UP — LONG from demand zone below
                    elif amd_phase == "manipulation_down" and daily_trend == "bullish":
                        found, dem_lo, dem_hi, dem_type = indicators.find_demand_zone(
                            htf["df"], current_price
                        )
                        if found:
                            self.bias[symbol]          = "BULLISH"
                            self.sweep_low[symbol]     = amd_info.get("sweep_wick", htf.get("sweep_wick_htf"))
                            self.amd_phase[symbol]     = "manipulation_down"
                            self.amd_zone_type[symbol] = dem_type
                            self.fvg_low[symbol]       = dem_lo
                            self.fvg_high[symbol]      = dem_hi
                            self.ranging_mode[symbol]  = False
                            self.state[symbol]         = "ENTRY_WAIT"
                            self.fvg_set_iter[symbol]  = self._iter_count
                            self.log_message(
                                f"[{symbol}] 🎯 AMD manipulation_down: swept ${amd_info.get('swept_level', 0):.2f} → "
                                f"LONG from [{dem_type}] {dem_lo:.2f}–{dem_hi:.2f}  "
                                f"target≈${amd_info.get('manipulation_target', 0):.2f}",
                                color="green"
                            )

            # ── Trend-zone fallback: daily trend clear but no fresh BOS/sweep ──
            if self.state[symbol] == "IDLE":
                if atr_pct >= MIN_ATR_PCT:
                    if daily_trend == "bullish":
                        found, dem_lo, dem_hi, dem_type = indicators.find_demand_zone(
                            htf["df"], current_price, max_distance_pct=0.08
                        )
                        if found:
                            self.bias[symbol]            = "BULLISH"
                            self.amd_phase[symbol]       = "trend_follow"
                            self.amd_zone_type[symbol]   = dem_type
                            self.fvg_low[symbol]         = dem_lo
                            self.fvg_high[symbol]        = dem_hi
                            self.ranging_mode[symbol]    = False
                            self.state[symbol]           = "ENTRY_WAIT"
                            self.fvg_set_iter[symbol]    = self._iter_count
                            self.zone_set_price[symbol]  = current_price
                            self.log_message(
                                f"[{symbol}] 📊 Trend zone: daily BULLISH → [{dem_type}] "
                                f"{dem_lo:.2f}–{dem_hi:.2f} → LONG on pullback", color="green"
                            )
                    elif daily_trend == "bearish":
                        found, sup_lo, sup_hi, sup_type = indicators.find_supply_zone(
                            htf["df"], current_price, max_distance_pct=0.08
                        )
                        if found:
                            self.bias[symbol]            = "BEARISH"
                            self.amd_phase[symbol]       = "trend_follow"
                            self.amd_zone_type[symbol]   = sup_type
                            self.fvg_low[symbol]         = sup_lo
                            self.fvg_high[symbol]        = sup_hi
                            self.ranging_mode[symbol]    = False
                            self.state[symbol]           = "ENTRY_WAIT"
                            self.fvg_set_iter[symbol]    = self._iter_count
                            self.zone_set_price[symbol]  = current_price
                            self.log_message(
                                f"[{symbol}] 📊 Trend zone: daily BEARISH → [{sup_type}] "
                                f"{sup_lo:.2f}–{sup_hi:.2f} → SHORT on rally", color="red"
                            )

        # ── STATE 2: SWEEP_HUNT ───────────────────────────────────────────────────
        # Real SMC discipline: require a liquidity sweep before looking for an FVG.
        # After 30 iterations (~30 min) without a sweep the BOS signal is stale — reset.
        SWEEP_PATIENCE = 30
        if self.state[symbol] == "SWEEP_HUNT" and position is None:
            iters_hunting = self._iter_count - self.sweep_hunt_iter[symbol]

            if ltf["sweep"] and ltf["sweep_wick_low"]:
                self.sweep_low[symbol] = ltf["sweep_wick_low"]
                self.log_message(
                    f"[{symbol}] Sweep detected @ {ltf['sweep_wick_low']:.4f} — now hunting FVG/OB.",
                    color="cyan"
                )

            # Expire the BOS signal if no sweep in SWEEP_PATIENCE iterations
            if iters_hunting > SWEEP_PATIENCE and not self.sweep_low[symbol]:
                self.log_message(
                    f"[{symbol}] No sweep after {SWEEP_PATIENCE} iters — BOS stale. Resetting.",
                    color="yellow"
                )
                self._reset(symbol)
                return

            # Only proceed to ENTRY_WAIT once a sweep has been recorded
            if not self.sweep_low[symbol]:
                return

            # Prefer the post-sweep displacement FVG (the CHoCH gap) — that's the gap the
            # reversal left behind. Fall back to a generic FVG/OB only with no clean gap.
            want = "bullish" if self.bias[symbol] == "BULLISH" else "bearish"
            d_found, d_dir, d_lo, d_hi, _ = indicators.detect_displacement_fvg(ltf["df"])
            if d_found and d_dir == want:
                self.fvg_low[symbol]       = d_lo
                self.fvg_high[symbol]      = d_hi
                self.amd_zone_type[symbol] = "choch_fvg"
                self.fvg_set_iter[symbol]  = self._iter_count
                self.state[symbol]         = "ENTRY_WAIT"
                self.log_message(
                    f"[{symbol}] STEP 2: 🎯 CHoCH FVG (displacement) locked | "
                    f"{d_lo:.4f}-{d_hi:.4f} | sweep={self.sweep_low[symbol]:.4f}",
                    color="green" if want == "bullish" else "red"
                )

            elif self.bias[symbol] == "BULLISH" and ltf["fvg_bull"]:
                self.fvg_low[symbol]      = ltf["fvg_bottom"]
                self.fvg_high[symbol]     = ltf["fvg_top"]
                self.fvg_set_iter[symbol] = self._iter_count
                self.state[symbol]        = "ENTRY_WAIT"
                self.log_message(
                    f"[{symbol}] STEP 2: Bullish OB/FVG locked | "
                    f"{self.fvg_low[symbol]:.4f}-{self.fvg_high[symbol]:.4f} | "
                    f"sweep={self.sweep_low[symbol]:.4f}  mss={ltf['mss']}",
                    color="green"
                )

            elif self.bias[symbol] == "BEARISH" and ltf["fvg_bear"]:
                self.fvg_low[symbol]      = ltf["fvg_bear_bottom"]
                self.fvg_high[symbol]     = ltf["fvg_bear_top"]
                self.fvg_set_iter[symbol] = self._iter_count
                self.state[symbol]        = "ENTRY_WAIT"
                self.log_message(
                    f"[{symbol}] STEP 2: Bearish OB/FVG locked | "
                    f"{self.fvg_low[symbol]:.4f}-{self.fvg_high[symbol]:.4f} | "
                    f"sweep={self.sweep_low[symbol]:.4f}  mss={ltf['mss']}",
                    color="red"
                )

        # ── STATE 3: ENTRY_WAIT ────────────────────────────────────────────────
        FVG_EXPIRY_ITERS = 20   # standard FVG: ~20 iterations
        AMD_ENTRY_WAIT_ITERS = 96  # AMD zone: up to 8h for price to reach supply/demand
        if self.state[symbol] == "ENTRY_WAIT" and position is None:
            expiry = AMD_ENTRY_WAIT_ITERS if self.amd_phase[symbol] else FVG_EXPIRY_ITERS
            if self._iter_count - self.fvg_set_iter[symbol] > expiry:
                tag = "AMD zone" if self.amd_phase[symbol] else "FVG"
                self.log_message(
                    f"[{symbol}] {tag} expired after {expiry} iters — resetting to IDLE.",
                    color="yellow"
                )
                self._reset(symbol)
                return

            # Stale trend-zone invalidation: a "trend_follow" zone is a wait-for-the-pullback
            # setup. Abandon it only if price RUNS AWAY ≥1.5% in our direction from where the
            # zone was armed (the move left without us). Measured vs the arm-price, NOT a
            # displacement — a trend always has a recent displacement, which would abandon the
            # zone the instant it's armed (an infinite set/abandon thrash).
            zsp = self.zone_set_price[symbol]
            if self.amd_phase[symbol] == "trend_follow" and zsp:
                bias = self.bias[symbol]
                ran_away = ((bias == "BEARISH" and current_price < zsp * 0.985) or
                            (bias == "BULLISH" and current_price > zsp * 1.015))
                if ran_away:
                    moved = abs(current_price - zsp) / zsp
                    self.log_message(
                        f"[{symbol}] ⚠️ Trend zone abandoned — price ran {moved:.1%} from setup "
                        f"({zsp:.2f}→{current_price:.2f}) without tapping; re-hunting.", color="yellow"
                    )
                    self._reset(symbol)
                    return

            in_fvg = (self.fvg_low[symbol] is not None and
                      self.fvg_high[symbol] is not None and
                      self.fvg_low[symbol] <= current_price <= self.fvg_high[symbol])

            if in_fvg:
                # ATR gate at execution: skip if market gone dead since zone was set
                entry_atr_pct = htf.get("atr_pct", 1.0)
                if entry_atr_pct < 0.004:
                    self.log_message(
                        f"[{symbol}] ⏸ Entry skipped — flat at tap "
                        f"(ATR={entry_atr_pct:.2%} < 0.4%)", color="yellow"
                    )
                    return

                # ── Reversal veto: never fade a fresh opposite-side reversal ──────────
                # Same Debbie-La rule as the crypto bot: a swept LOW that gets reclaimed hard
                # is a bullish reversal (don't short it); a swept HIGH that flushes is bearish.
                # The reclaim threshold scales with the instrument's ATR — stocks travel far
                # less than crypto, so a fixed % would never trip here. Stand aside if price
                # has V-recovered off the window extreme and now sits near the opposite end.
                _is_long = self.bias[symbol] == "BULLISH"
                _rev = ltf["df"].tail(48)
                _wlo, _whi = float(_rev["low"].min()), float(_rev["high"].max())
                _rng = _whi - _wlo
                _thresh = max(0.012, 2.5 * entry_atr_pct)   # ≥1.2% or 2.5×ATR
                if _rng > 0 and not _is_long:
                    _age = len(_rev) - 1 - int(_rev["low"].values.argmin())
                    _rec = (current_price - _wlo) / _wlo
                    if _rec >= _thresh and (current_price - _wlo) / _rng >= 0.55 and _age >= 6:
                        self.log_message(
                            f"[{symbol}] 🚫 SHORT vetoed — price V-recovered {_rec:+.1%} off swept "
                            f"low {_wlo:.2f} and sits near the highs = bullish reversal; standing aside.",
                            color="yellow")
                        return
                elif _rng > 0 and _is_long:
                    _age = len(_rev) - 1 - int(_rev["high"].values.argmax())
                    _drop = (_whi - current_price) / _whi
                    if _drop >= _thresh and (_whi - current_price) / _rng >= 0.55 and _age >= 6:
                        self.log_message(
                            f"[{symbol}] 🚫 LONG vetoed — price V-dropped {_drop:+.1%} off swept "
                            f"high {_whi:.2f} and sits near the lows = bearish reversal; standing aside.",
                            color="yellow")
                        return

                # Candle pattern confirmation (informational — AI still decides)
                tap_candle = indicators.classify_candle(ltf["df"].iloc[-1], ltf["df"].iloc[-2])
                confirms   = indicators.candle_confirms_bias(tap_candle, self.bias[symbol])
                self.log_message(
                    f"[{symbol}] 🕯 Zone tap — candle: "
                    f"{'✅' if confirms else '⚠️'} {tap_candle}",
                    color="cyan"
                )

                # S/R confluence at the entry zone
                sr_near, sr_level = indicators.is_near_sr_level(
                    current_price,
                    htf.get("resistance", []) + htf.get("support", [])
                )
                if sr_near:
                    self.log_message(
                        f"[{symbol}] 📊 S/R confluence at zone tap: {sr_level:.2f}", color="cyan"
                    )
                else:
                    self.log_message(
                        f"[{symbol}] ⚠️ No S/R confluence at tap (nearest S/R may be far)", color="yellow"
                    )

                confirm, sentiment, prob = self._get_sentiment(symbol)
                if prob > 0:
                    self.log_message(
                        f"[{symbol}] AI: {sentiment.upper()} ({prob*100:.1f}%) → {'✅' if confirm else '❌'}",
                        color="purple"
                    )
                else:
                    self.log_message(f"[{symbol}] No news — proceeding on technicals.", color="purple")

                if confirm:
                    is_long = self.bias[symbol] == "BULLISH"
                    side    = Order.OrderSide.BUY if is_long else Order.OrderSide.SELL

                    # SL at the structural invalidation point (OB/FVG boundary).
                    # Cap at 3× LTF (15m) ATR: the HTF zone can be many ATRs wide which
                    # makes the stop swing-sized on a day-trade timeframe.
                    _ltf_rng = ltf["df"]["high"] - ltf["df"]["low"]
                    _ltf_atr = float(_ltf_rng.rolling(14).mean().iloc[-1])
                    _max_sl_dist = 3.0 * _ltf_atr
                    if is_long:
                        sl = float(f"{self.fvg_low[symbol] * 0.997:.2f}")
                        if current_price - sl > _max_sl_dist:
                            sl = round(current_price - _max_sl_dist, 2)
                            self.log_message(
                                f"[{symbol}] 📏 SL capped at 3×15m ATR (${_ltf_atr:.2f}) → SL ${sl:.2f}",
                                color="yellow")
                    else:
                        sl = float(f"{self.fvg_high[symbol] * 1.003:.2f}")
                        if sl - current_price > _max_sl_dist:
                            sl = round(current_price + _max_sl_dist, 2)
                            self.log_message(
                                f"[{symbol}] 📏 SL capped at 3×15m ATR (${_ltf_atr:.2f}) → SL ${sl:.2f}",
                                color="yellow")

                    # Liquidation guard: at PAPER_LEVERAGE x, a 1/L adverse move
                    # wipes the margin. Cap SL inside 90% of that distance.
                    if PAPER_LEVERAGE > 1:
                        _liq_dist = current_price / PAPER_LEVERAGE
                        if is_long and (current_price - sl) >= _liq_dist:
                            sl = round(current_price - _liq_dist * 0.90, 2)
                            self.log_message(
                                f"[{symbol}] ⚡ SL auto-capped at 90% liq distance "
                                f"({PAPER_LEVERAGE}x → liq in {1/PAPER_LEVERAGE:.0%}) → SL ${sl:.2f}",
                                color="yellow")
                        elif not is_long and (sl - current_price) >= _liq_dist:
                            sl = round(current_price + _liq_dist * 0.90, 2)
                            self.log_message(
                                f"[{symbol}] ⚡ SL auto-capped at 90% liq distance "
                                f"({PAPER_LEVERAGE}x → liq in {1/PAPER_LEVERAGE:.0%}) → SL ${sl:.2f}",
                                color="yellow")

                    risk_amt = abs(current_price - sl)
                    bias_str = "bullish" if is_long else "bearish"
                    pool_tp  = indicators.find_next_liquidity_target(htf["df"], current_price, bias_str)

                    ai_confirm, rr_actual, ai_reason = get_ai_confirmation(
                        symbol, current_price, daily_trend, bias_str,
                        self.fvg_low[symbol], self.fvg_high[symbol],
                        self.sweep_low[symbol] or 0, sl, risk_amt, pool_tp,
                        ltf["df"], htf["df"],
                        amd_phase=self.amd_phase[symbol],
                        zone_type=self.amd_zone_type[symbol],
                    )
                    self.log_message(
                        f"[{symbol}] 🤖 AI Bot Approval: {'✅ YES' if ai_confirm else '❌ NO'}  "
                        f"R:R=1:{rr_actual:.1f}  {ai_reason}",
                        color="purple"
                    )

                    if ai_confirm:
                        cash, last_price, quantity = self.position_sizing(symbol, sl_price=sl)
                        if self.ranging_mode[symbol]:
                            quantity = max(1, round(quantity * 0.5))

                        if quantity > 0 and cash > last_price:
                            # Final guard: never stack onto an existing position. Covers a
                            # stale/None get_position from the top of the iteration, or a
                            # prior fill Alpaca hasn't reflected during the ~15s AI call.
                            # If we can't verify the position, we do NOT enter.
                            try:
                                live = self.get_position(asset)
                            except Exception as e:
                                self.log_message(
                                    f"[{symbol}] ⛔ Entry aborted — couldn't verify position ({e}).",
                                    color="red"
                                )
                                return
                            if live is not None and abs(live.quantity) > 0:
                                self.log_message(
                                    f"[{symbol}] ⛔ Entry aborted — position already open "
                                    f"({live.quantity}). Syncing to POSITION_OPEN.", color="red"
                                )
                                self.state[symbol] = "POSITION_OPEN"
                                self._save_state()
                                return

                            self.stop_loss[symbol]   = sl
                            self.take_profit[symbol] = round(
                                current_price + risk_amt * rr_actual if is_long
                                else current_price - risk_amt * rr_actual, 2
                            )
                            tp = self.take_profit[symbol]

                            # Try a BRACKET order first — Alpaca/TradingView render its legs
                            # as a green TP / red SL pair with proper labels. If Alpaca rejects
                            # it (wash-trade, etc.), fall back to a plain market entry whose OCO
                            # is attached in on_filled_order. Either way the trade gets in.
                            # TIF=day is fine: the bot flattens at the close (before_closing_bell),
                            # so the protective legs only need to last the session.
                            self.bracket_active[symbol] = False
                            try:
                                import requests as _req
                                _resp = _req.post(
                                    f"{ALPACA_BASE_URL}/v2/orders",
                                    json={
                                        "symbol":        symbol,
                                        "qty":           str(int(quantity)),
                                        "side":          "buy" if is_long else "sell",
                                        "type":          "market",
                                        "time_in_force": "gtc",   # GTC so the SL/TP legs DON'T expire at the close (a held position must stay protected overnight)
                                        "order_class":   "bracket",
                                        "take_profit":   {"limit_price": str(round(tp, 2))},
                                        "stop_loss":     {"stop_price":  str(round(sl, 2))},
                                    },
                                    headers={
                                        "APCA-API-KEY-ID":     ALPACA_API_KEY,
                                        "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
                                    },
                                    timeout=10,
                                )
                                _resp.raise_for_status()
                                data = _resp.json() if _resp.content else {}
                                ids  = []
                                if isinstance(data, dict):
                                    if data.get("id"):
                                        ids.append(data["id"])
                                    for leg in (data.get("legs") or []):
                                        if isinstance(leg, dict) and leg.get("id"):
                                            ids.append(leg["id"])
                                self.oco_ids[symbol]        = ids
                                self.bracket_active[symbol] = True
                                _bkt_val = int(quantity) * current_price
                                _bkt_mgn = _bkt_val / PAPER_LEVERAGE
                                self.log_message(
                                    f"[{symbol}] 🎯 BRACKET entry ({int(quantity)} sh) | "
                                    f"margin=${_bkt_mgn:,.2f} → controls ${_bkt_val:,.2f} | "
                                    f"TP {tp:.2f} / SL {sl:.2f} | R:R 1:{rr_actual:.1f}", color="green"
                                )
                            except Exception as e:
                                # Fallback: plain market entry; OCO attaches in on_filled_order
                                self.log_message(
                                    f"[{symbol}] Bracket rejected ({e}) — falling back to market + OCO",
                                    color="yellow"
                                )
                                order = self.create_order(asset, quantity, side)
                                self.entry_order[symbol] = order
                                self.submit_order(order)

                            self.state[symbol]       = "POSITION_OPEN"
                            self.entry_price[symbol] = current_price
                            self.entry_qty[symbol]   = quantity
                            self.entry_time[symbol]  = datetime.now(timezone.utc)
                            self._save_state()

                            pos_value = quantity * current_price
                            margin    = pos_value / PAPER_LEVERAGE
                            if PAPER_LEVERAGE > 1:
                                liq_price = (current_price * (1 - 1/PAPER_LEVERAGE) if is_long
                                             else current_price * (1 + 1/PAPER_LEVERAGE))
                                lev_line = (f"\n           💹 {PAPER_LEVERAGE}x LEVERAGE  "
                                            f"margin=${margin:,.2f} controls ${pos_value:,.2f}  "
                                            f"({quantity} shares)  "
                                            f"liq if price hits ${liq_price:,.2f}")
                            else:
                                lev_line = ""

                            self.log_message(
                                f"[{symbol}] ✅ {'LONG' if is_long else 'SHORT'} ENTRY | "
                                f"Price: {current_price:.4f} | "
                                f"margin: ${margin:,.2f} → controls ${pos_value:,.2f} | "
                                f"SL: {self.stop_loss[symbol]:.4f} | TP: {self.take_profit[symbol]:.4f} | "
                                f"Risk: ${risk_amt*quantity:.2f} | Reward: ${risk_amt*quantity*rr_actual:.2f}"
                                f"{lev_line}",
                                color="blue"
                            )

    def on_filled_order(self, position, order, price, quantity, multiplier):
        """Fires when entry fills. Posts a proper OCO directly to Alpaca REST API
        so TradingView shows SL/TP lines. Python-side monitoring in _process_symbol
        acts as the real exit trigger regardless of broker-side order state."""
        asset  = order.asset
        symbol = getattr(asset, "symbol", str(asset))
        if symbol not in self.symbols:
            return
        if order is not self.entry_order.get(symbol):
            return
        self.entry_order[symbol] = None

        # If the entry went in as a bracket, TP/SL are already attached — don't double up.
        if self.bracket_active.get(symbol):
            return

        is_long = self.bias.get(symbol) == "BULLISH"
        sl      = self.stop_loss.get(symbol)
        tp      = self.take_profit.get(symbol)

        # Match the OCO size to the ACTUAL live position (covers partial fills), falling
        # back to the fill quantity if the position isn't readable yet.
        try:
            live    = self.get_position(asset)
            oco_qty = int(abs(live.quantity)) if live is not None else int(abs(quantity))
        except Exception:
            oco_qty = int(abs(quantity))

        if sl and tp and oco_qty > 0:
            try:
                import requests as _req
                _resp = _req.post(
                    f"{ALPACA_BASE_URL}/v2/orders",
                    json={
                        "symbol":        symbol,
                        "qty":           str(oco_qty),
                        "side":          "buy" if not is_long else "sell",
                        "type":          "limit",
                        "time_in_force": "gtc",
                        "order_class":   "oco",
                        "take_profit":   {"limit_price": str(round(tp, 2))},
                        "stop_loss":     {"stop_price":  str(round(sl, 2))},
                    },
                    headers={
                        "APCA-API-KEY-ID":     ALPACA_API_KEY,
                        "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
                    },
                    timeout=10,
                )
                _resp.raise_for_status()

                # Capture the OCO order IDs (parent + legs) so _reset can cancel them
                # later instead of leaving orphans on the book.
                data = _resp.json() if _resp.content else {}
                ids  = []
                if isinstance(data, dict):
                    if data.get("id"):
                        ids.append(data["id"])
                    for leg in (data.get("legs") or []):
                        if isinstance(leg, dict) and leg.get("id"):
                            ids.append(leg["id"])
                self.oco_ids[symbol] = ids
                self._save_state()  # persist IDs so a restart can still cancel them

                self.log_message(
                    f"[{symbol}] 🛑🎯 OCO posted ({oco_qty} sh, {len(ids)} legs) — "
                    f"SL @ {sl:.4f}  TP @ {tp:.4f}  (lines on TradingView)",
                    color="yellow"
                )
            except Exception as e:
                self.log_message(
                    f"[{symbol}] OCO API failed: {e} — Python-side SL/TP monitoring still active",
                    color="red"
                )

    def on_trading_iteration(self):
        self._iter_count += 1
        for symbol in self.symbols:
            self._process_symbol(symbol)

    def before_closing_bell(self):
        for symbol in self.symbols:
            asset = self._make_asset(symbol)
            position = self.get_position(asset)
            if position is not None:
                # Cancel OCO FIRST so the TP/SL legs don't race the closing market order
                # and cause a wash-trade or an orphan fill on the wrong side.
                self._cancel_oco(symbol)
                close_qty  = abs(position.quantity)
                close_side = (Order.OrderSide.BUY if position.quantity < 0
                              else Order.OrderSide.SELL)
                order = self.create_order(asset, close_qty, close_side)
                self.submit_order(order)
                self.log_message(
                    f"[{symbol}] EOD close: cancelled OCO + submitted market close "
                    f"({close_qty} shares).", color="cyan")
                self._reset(symbol)

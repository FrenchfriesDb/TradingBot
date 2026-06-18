"""
CCXT bot — Debbie-La SMC state machine with paper trading.
Uses Kraken public data feed by default (no API key needed).
Set BINANCE_API_KEY/SECRET in .env to switch to real Binance trading.
"""

import time
import math
import os
import pandas as pd
from datetime import datetime, timezone

# ── Patch ccxt _version.py bug BEFORE importing ccxt ──────────────────────────
# ccxt 4.5.x crashes on import due to int(None) in toolz/_version.py.
# We locate the file via importlib (no import needed) and patch it on disk first.
def _patch_ccxt():
    try:
        import importlib.util
        spec = importlib.util.find_spec("ccxt")
        if spec is None:
            return
        ccxt_dir = os.path.dirname(spec.origin)
        vpath = os.path.join(ccxt_dir, "static_dependencies", "toolz", "_version.py")
        if not os.path.exists(vpath):
            return
        txt = open(vpath).read()
        patched = txt.replace(
            'pieces["distance"] = int(count_out)',
            'pieces["distance"] = int(count_out) if count_out is not None else 0'
        )
        if patched != txt:
            open(vpath, "w").write(patched)
            print("ccxt patched.")
    except Exception as e:
        print(f"ccxt patch skipped: {e}")

_patch_ccxt()
import ccxt

from config import BINANCE_API_KEY, BINANCE_SECRET, BINANCE_TESTNET, BINANCE_CASH_AT_RISK, NVIDIA_API_KEY
from bot import indicators
from finbert_utils import estimate_sentiment

SLEEP_SECONDS = 5 * 60
STALE_TRADE_HOURS = 72  # 1:6 targets need up to 3 days to develop on crypto
FVG_EXPIRY_BARS     = 12   # reset ENTRY_WAIT if price hasn't tapped FVG within this many iterations
AMD_ENTRY_WAIT_BARS = 96   # AMD supply/demand zones can take up to 8h to reach — longer patience
PAPER_BALANCE = 10_000.0

DEFAULT_SYMBOLS  = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "XRP/USD", "AVAX/USD", "POL/USD", "ADA/USD"]
CRYPTO_STATE_FILE = "crypto_state.json"


# ── Paper trader ───────────────────────────────────────────────────────────────

class PaperTrader:
    """
    Simulates long AND short trades against real price data.
    Positions dict: positive qty = long, negative qty = short.
    Entry prices tracked separately for short P&L calculation.
    """

    def __init__(self, balance: float = PAPER_BALANCE):
        self.balance = balance
        self.positions: dict = {}      # symbol -> qty (neg = short)
        self.entry_prices: dict = {}   # symbol -> avg entry price
        self.trade_count = 0

    def get_position(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def buy(self, symbol: str, qty: float, price: float):
        """Open long, or cover an existing short."""
        held = self.positions.get(symbol, 0.0)
        if held < 0:
            # Cover short: P&L = (entry - exit) * qty
            cover = min(qty, abs(held))
            pnl = (self.entry_prices.get(symbol, price) - price) * cover
            self.balance += pnl
            new_held = held + cover
            if abs(new_held) < 1e-9:
                self.positions.pop(symbol, None)
                self.entry_prices.pop(symbol, None)
            else:
                self.positions[symbol] = new_held
        else:
            # Open long
            cost = qty * price
            if cost > self.balance:
                qty = math.floor((self.balance * 0.95 / price) * 1e6) / 1e6
                cost = qty * price
            if qty <= 0:
                return None
            self.balance -= cost
            self.positions[symbol] = held + qty
            self.entry_prices[symbol] = price
        self.trade_count += 1
        return {"id": self.trade_count, "qty": qty, "price": price}

    def sell(self, symbol: str, qty: float, price: float):
        """Close an existing long, or open a short."""
        held = self.positions.get(symbol, 0.0)
        if held > 0:
            # Close long
            qty = min(qty, held)
            if qty <= 0:
                return None
            self.balance += qty * price
            new_held = held - qty
            if new_held < 1e-9:
                self.positions.pop(symbol, None)
                self.entry_prices.pop(symbol, None)
            else:
                self.positions[symbol] = new_held
        else:
            # Open short (paper — no margin needed, P&L settled on cover)
            self.positions[symbol] = -qty
            self.entry_prices[symbol] = price
        self.trade_count += 1
        return {"id": self.trade_count, "qty": qty, "price": price}


# ── Exchange connection ────────────────────────────────────────────────────────

def connect_exchange():
    """
    Coinbase public feed if no API key — no account needed.
    Falls back to Binance (testnet or live) when BINANCE_API_KEY is set in .env.
    """
    if BINANCE_API_KEY:
        ex = ccxt.binance({
            "apiKey": BINANCE_API_KEY,
            "secret": BINANCE_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })
        if BINANCE_TESTNET:
            ex.set_sandbox_mode(True)
        mode = "Binance testnet" if BINANCE_TESTNET else "Binance live"
    else:
        ex = ccxt.coinbase({"enableRateLimit": True})
        mode = "Coinbase (public data — paper trades only)"
    print(f"Exchange: {mode}  |  Chart display: Coinbase")
    return ex


def connect_htf_exchange():
    """
    Bybit public API for 4H candles — no account needed, supports 4H granularity.
    Coinbase only goes up to 1H and 6H; Bybit has true 4H which is the standard
    SMC institutional timeframe for BOS detection.
    """
    try:
        ex = ccxt.bybit({"enableRateLimit": True})
        ex.load_markets()
        print("HTF data:  Bybit public (4H candles)")
        return ex
    except Exception as e:
        print(f"HTF data:  Bybit unavailable ({e}) — falling back to 6H on main exchange")
        return None


# ── Helpers ────────────────────────────────────────────────────────────────────

def ohlcv_to_df(ohlcv):
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


def save_crypto_state(paper: "PaperTrader", states: dict, symbols: list, prices: dict):
    """Persist the full bot state to JSON — positions, balances, and all state machine fields."""
    import json as _json
    try:
        positions = {}
        for sym, qty in paper.positions.items():
            if abs(qty) < 1e-9:
                continue
            entry  = paper.entry_prices.get(sym, 0.0)
            cur    = prices.get(sym, 0.0)
            st     = states.get(sym)
            sl     = st.stop_loss   if st else None
            tp     = st.take_profit if st else None
            upnl   = (cur - entry) * qty if qty > 0 else (entry - cur) * abs(qty)
            risk   = abs(entry - sl) * abs(qty) if sl and entry else None
            reward = abs(tp - entry) * abs(qty) if tp and entry else None
            positions[sym] = {
                "qty":            qty,
                "side":           "LONG" if qty > 0 else "SHORT",
                "entry_price":    entry,
                "current_price":  cur,
                "stop_loss":      sl,
                "take_profit":    tp,
                "unrealized_pnl": upnl,
                "risk_dollars":   risk,
                "reward_dollars": reward,
            }

        # Serialize full SymbolState so restarts resume correctly
        state_machine = {}
        for s in symbols:
            st = states[s]
            state_machine[s] = {
                "state":              st.state,
                "bias":               st.bias,
                "sweep_low":          st.sweep_low,
                "sweep_hunt_bar":     st.sweep_hunt_bar,
                "fvg_low":            st.fvg_low,
                "fvg_high":           st.fvg_high,
                "entry_price":        st.entry_price,
                "stop_loss":          st.stop_loss,
                "take_profit":        st.take_profit,
                "bars_in_entry_wait": st.bars_in_entry_wait,
                "entry_time":         st.entry_time.isoformat() if st.entry_time else None,
                "amd_phase":          st.amd_phase,
                "amd_zone_type":      st.amd_zone_type,
            }

        data = {
            "last_updated":  datetime.now(timezone.utc).isoformat(),
            "balance":       paper.balance,
            "start_balance": PAPER_BALANCE,
            "trade_count":   paper.trade_count,
            "state_machine": state_machine,
            "positions":     positions,
        }
        with open(CRYPTO_STATE_FILE, "w") as fh:
            _json.dump(data, fh, indent=2)
    except Exception as e:
        print(f"[STATE] save failed: {e}")


def load_crypto_state(paper: "PaperTrader", states: dict, symbols: list):
    """Restore paper trader and all state machine fields from the last save."""
    import json as _json
    if not os.path.exists(CRYPTO_STATE_FILE):
        return
    try:
        with open(CRYPTO_STATE_FILE) as fh:
            data = _json.load(fh)

        paper.balance     = data.get("balance", PAPER_BALANCE)
        paper.trade_count = data.get("trade_count", 0)

        # Restore positions and entry prices from the serialized positions block
        paper.positions    = {}
        paper.entry_prices = {}
        for sym, pos in data.get("positions", {}).items():
            paper.positions[sym]    = pos["qty"]
            paper.entry_prices[sym] = pos["entry_price"]

        # Restore full SymbolState per symbol
        for s in symbols:
            saved = data.get("state_machine", {}).get(s)
            if not saved:
                continue
            st = states[s]
            # Handle old format where state was saved as a plain string, not a dict
            if isinstance(saved, str):
                st.state = saved
                continue
            st.state              = saved.get("state", "IDLE")
            st.bias               = saved.get("bias")
            st.sweep_low          = saved.get("sweep_low")
            st.sweep_hunt_bar     = saved.get("sweep_hunt_bar", 0)
            st.fvg_low            = saved.get("fvg_low")
            st.fvg_high           = saved.get("fvg_high")
            st.entry_price        = saved.get("entry_price")
            st.stop_loss          = saved.get("stop_loss")
            st.take_profit        = saved.get("take_profit")
            st.bars_in_entry_wait = saved.get("bars_in_entry_wait", 0)
            st.amd_phase          = saved.get("amd_phase")
            st.amd_zone_type      = saved.get("amd_zone_type")
            raw_time              = saved.get("entry_time")
            st.entry_time         = (datetime.fromisoformat(raw_time).replace(tzinfo=timezone.utc)
                                     if raw_time else None)
            if st.state != "IDLE":
                print(f"[STATE] Restored {s}: {st.state}  bias={st.bias}  "
                      f"SL={st.stop_loss}  TP={st.take_profit}")

        # Auto-recover orphaned positions: position exists but state reset to IDLE
        for sym, pos in data.get("positions", {}).items():
            if sym not in states:
                continue
            st  = states[sym]
            qty = paper.positions.get(sym, 0)
            if abs(qty) < 1e-6 or st.state != "IDLE":
                continue
            st.state       = "POSITION_OPEN"
            st.entry_price = pos.get("entry_price")
            st.bias        = "BULLISH" if qty > 0 else "BEARISH"
            st.stop_loss   = pos.get("stop_loss")
            st.take_profit = pos.get("take_profit")
            entry = st.entry_price or 0
            if entry and not st.stop_loss:
                if qty > 0:
                    st.stop_loss   = round(entry * 0.985, 6)
                    st.take_profit = round(entry * (1 + 0.015 * 3), 6)
                else:
                    st.stop_loss   = round(entry * 1.015, 6)
                    st.take_profit = round(entry * (1 - 0.015 * 3), 6)
            print(f"[STATE] Auto-recovered {sym} → POSITION_OPEN  "
                  f"entry=${entry:,.4f}  SL={st.stop_loss}  TP={st.take_profit}")
    except Exception as e:
        print(f"[STATE] load failed (starting fresh): {e}")


def get_sentiment(headlines: list):
    if not headlines:
        return True, "no_news", 0.0
    try:
        prob, sentiment = estimate_sentiment(headlines)
        confirm = not (sentiment == "negative" and prob >= 0.60)
        return confirm, sentiment, prob
    except Exception:
        return True, "error", 0.0


# ── Per-symbol state ───────────────────────────────────────────────────────────

class SymbolState:
    def __init__(self):
        self.state = "IDLE"
        self.bias = None
        self.sweep_low = None
        self.sweep_hunt_bar = 0   # iteration when SWEEP_HUNT started
        self.fvg_low = None
        self.fvg_high = None
        self.entry_price = None
        self.stop_loss = None
        self.take_profit = None
        self.entry_time = None
        self.bars_in_entry_wait = 0
        self.ranging_mode = False  # True when daily trend is unclear — use half size
        self.amd_phase = None      # 'manipulation_up'|'manipulation_down' when AMD override active
        self.amd_zone_type = None  # 'bearish_fvg'|'bearish_ob'|'ifvg'|'bullish_fvg'|...

    def reset(self):
        self.__init__()


# ── NVIDIA AI trade confirmation ───────────────────────────────────────────────

MIN_AI_RR = 3.5   # hard floor — enforced in code regardless of what the model says
MAX_AI_RR = 15.0  # sanity ceiling — guards against a hallucinated target

def get_ai_confirmation(symbol, price, daily_trend, bos_dir,
                        fvg_low, fvg_high, sweep_level,
                        sl, risk_amt, pool_tp, df_ltf, df_htf=None,
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
    pool_line = (f"Nearest 4H liquidity pool target: ${pool_tp:,.4f}  "
                 f"(implies 1:{abs(pool_tp - price) / risk_amt:.1f} R:R)"
                 if pool_tp else "Nearest 4H liquidity pool target: none found")

    # Full 4H chart context — 20 candles so the AI can see the sweep, BOS, FVG, and AMD phase
    if df_htf is not None and len(df_htf) >= 5:
        htf_rows = df_htf.tail(20)
        swing_high = float(df_htf['high'].tail(50).max())
        swing_low  = float(df_htf['low'].tail(50).min())
        htf_block = "4H candles (oldest → newest):\n" + "\n".join(
            f"  {i+1:2d}. O:{r['open']:,.2f} H:{r['high']:,.2f} "
            f"L:{r['low']:,.2f} C:{r['close']:,.2f}"
            for i, (_, r) in enumerate(htf_rows.iterrows())
        ) + f"\n50-bar structure: Low ${swing_low:,.2f}  High ${swing_high:,.2f}"
    else:
        htf_block = "(4H data unavailable)"

    # Last 5 LTF candles for execution precision
    ltf_recent = df_ltf.tail(5)
    ltf_candles = "  ".join(
        f"O:{r['open']:.2f} H:{r['high']:.2f} L:{r['low']:.2f} C:{r['close']:.2f}"
        for _, r in ltf_recent.iterrows()
    )

    # AMD-specific narrative so the AI understands the counter-setup context
    if amd_phase == 'manipulation_up':
        amd_context = (
            f"AMD Phase    : MANIPULATION_UP (bot-detected)\n"
            f"Zone Type    : {zone_type or 'supply'}\n"
            f"Narrative    : Lows were swept (stop hunt complete). Price is now BLEEDING UP\n"
            f"               into the {zone_type or 'supply'} zone above — this is the manipulation\n"
            f"               leg inducing late longs before distribution DOWN.\n"
            f"               IFVG / supply zone: ${fvg_low:,.4f} – ${fvg_high:,.4f} is the SHORT entry."
        )
        amd_question = (
            f"- Confirm: does the 4H chart show a brutal sweep of lows followed by a bleed-up?\n"
            f"- Is the {zone_type or 'supply'} zone at ${fvg_low:,.4f}–${fvg_high:,.4f} a valid "
            f"IFVG / distribution area?\n"
            f"- Does the daily trend support a SHORT from this supply zone?"
        )
    elif amd_phase == 'manipulation_down':
        amd_context = (
            f"AMD Phase    : MANIPULATION_DOWN (bot-detected)\n"
            f"Zone Type    : {zone_type or 'demand'}\n"
            f"Narrative    : Highs were swept (stop hunt complete). Price is BLEEDING DOWN\n"
            f"               into the {zone_type or 'demand'} zone below — manipulation leg\n"
            f"               inducing late shorts before accumulation UP.\n"
            f"               IFVG / demand zone: ${fvg_low:,.4f} – ${fvg_high:,.4f} is the LONG entry."
        )
        amd_question = (
            f"- Confirm: does the 4H chart show a brutal sweep of highs followed by a bleed-down?\n"
            f"- Is the {zone_type or 'demand'} zone at ${fvg_low:,.4f}–${fvg_high:,.4f} a valid "
            f"IFVG / accumulation area?\n"
            f"- Does the daily trend support a LONG from this demand zone?"
        )
    else:
        amd_context  = f"AMD Phase    : standard BOS-based setup"
        amd_question = (
            f"- Did a genuine liquidity sweep occur at ${sweep_level:,.4f}?\n"
            f"- Does AMD (Accumulation → Manipulation → Distribution) context support this {side}?\n"
            f"- How much room does price have before the next opposing liquidity pool?"
        )

    prompt = f"""You are an expert institutional SMC (Smart Money Concepts) trade analyst.
You deeply understand AMD cycles, IFVGs (Inverse Fair Value Gaps), and how smart money
engineers sweeps to induce retail before the real distribution/accumulation move.

{htf_block}

SETUP SUMMARY:
Symbol       : {symbol}
Direction    : {side}
Current Price: ${price:,.4f}
Daily Trend  : {(daily_trend or 'UNCLEAR').upper()}
4H BOS       : {(bos_dir or 'NONE').upper()}
Swept level  : ${sweep_level:,.4f}
{amd_context}
Stop Loss    : ${sl:,.4f}  (risk = ${risk_amt:,.4f} per unit)
{pool_line}
Last 5 execution candles: {ltf_candles}

Analyze using the full 4H chart above:
{amd_question}

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

        import re
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


# ── Core strategy loop per symbol ─────────────────────────────────────────────

def process_symbol(exchange, paper: PaperTrader, symbol: str,
                   state: SymbolState, risk_fraction: float,
                   htf_exchange=None):
    base = symbol.split("/")[0]
    now = datetime.now(timezone.utc)

    try:
        df_ltf   = ohlcv_to_df(exchange.fetch_ohlcv(symbol, "5m", limit=55))
        df_daily = ohlcv_to_df(exchange.fetch_ohlcv(symbol, "1d", limit=60))
        # Bybit uses USDT pairs (BTC/USDT) — convert from USD for the HTF fetch
        if htf_exchange:
            bybit_sym = symbol.replace("/USD", "/USDT")
            df_htf = ohlcv_to_df(htf_exchange.fetch_ohlcv(bybit_sym, "4h", limit=200))
        else:
            df_htf = ohlcv_to_df(exchange.fetch_ohlcv(symbol, "6h", limit=200))
    except Exception as e:
        print(f"[{base}] Data error: {e}")
        return None

    price = float(df_ltf["close"].iloc[-1])
    held  = paper.get_position(symbol)
    has_position = abs(held) > 1e-6

    daily_trend  = indicators.get_daily_trend(df_daily)
    is_bos,  direction   = indicators.detect_displacement_bos(df_htf, lookback=15)

    # Check sweep + FVG on BOTH 5m (precise entry) and 4H (macro setup)
    # 4H signals fire first when the entire setup is on the higher timeframe
    is_sweep_ltf, _, sweep_wick_ltf = indicators.check_liquidity_sweep(df_ltf)
    is_sweep_htf, _, sweep_wick_htf = indicators.check_liquidity_sweep(df_htf, sweep_window=5)
    is_sweep   = is_sweep_ltf or is_sweep_htf
    sweep_wick = sweep_wick_htf if is_sweep_htf else sweep_wick_ltf

    is_fvg_bull_ltf, fvg_bot_ltf, fvg_top_ltf = indicators.find_bullish_fvg(df_ltf)
    is_fvg_bear_ltf, fvg_bear_bot_ltf, fvg_bear_top_ltf = indicators.find_bearish_fvg(df_ltf)
    is_fvg_bull_htf, fvg_bot_htf, fvg_top_htf = indicators.find_bullish_fvg(df_htf)
    is_fvg_bear_htf, fvg_bear_bot_htf, fvg_bear_top_htf = indicators.find_bearish_fvg(df_htf)

    # Prefer 4H FVG when available — it's the zone that matters on macro setups
    is_fvg_bull    = is_fvg_bull_htf or is_fvg_bull_ltf
    fvg_bot        = fvg_bot_htf  if is_fvg_bull_htf else fvg_bot_ltf
    fvg_top        = fvg_top_htf  if is_fvg_bull_htf else fvg_top_ltf
    is_fvg_bear    = is_fvg_bear_htf or is_fvg_bear_ltf
    fvg_bear_bot   = fvg_bear_bot_htf if is_fvg_bear_htf else fvg_bear_bot_ltf
    fvg_bear_top   = fvg_bear_top_htf if is_fvg_bear_htf else fvg_bear_top_ltf

    tf_tag = "4H" if (is_sweep_htf or is_fvg_bull_htf or is_fvg_bear_htf) else "5m"
    zone_tag = ""
    if state.state == "ENTRY_WAIT" and state.fvg_low and state.fvg_high:
        tag = "AMD" if state.amd_phase else "FVG"
        in_zone = state.fvg_low <= price <= state.fvg_high
        zone_tag = (f"  [{tag} zone ${state.fvg_low:,.2f}–${state.fvg_high:,.2f} "
                    f"{'✅IN' if in_zone else '⏳waiting'}]")
    print(f"[{base}] {now.strftime('%H:%M')} ${price:,.2f}  "
          f"daily={daily_trend}  state={state.state}  bos={is_bos}({direction})  "
          f"sweep={is_sweep}({tf_tag})  fvg_bull={is_fvg_bull}  fvg_bear={is_fvg_bear}"
          f"{zone_tag}")

    # ── POSITION_OPEN ──────────────────────────────────────────────────────────
    if state.state == "POSITION_OPEN":
        if not has_position:
            print(f"[{base}] Position gone. Resetting.")
            state.reset()
            return price

        is_long = held > 0
        close_qty = abs(held)

        def close_position(reason):
            if is_long:
                paper.sell(symbol, close_qty, price)
                pnl = (price - state.entry_price) * close_qty
            else:
                paper.buy(symbol, close_qty, price)
                pnl = (state.entry_price - price) * close_qty
            direction_label = "LONG" if is_long else "SHORT"
            print(f"[{base}] {reason} ${price:,.2f}  {direction_label}  "
                  f"P&L: ${pnl:+.2f}  Balance: ${paper.balance:,.2f}")
            state.reset()

        # SL: long exits below SL, short exits above SL
        sl_hit = (is_long and price <= state.stop_loss) or \
                 (not is_long and price >= state.stop_loss)
        tp_hit = (is_long and price >= state.take_profit) or \
                 (not is_long and price <= state.take_profit)

        if sl_hit:
            close_position("🔴 SL hit")
            return price
        if tp_hit:
            close_position("🟢 TP hit")
            return price
        if state.entry_time:
            elapsed = (now - state.entry_time).total_seconds() / 3600
            if elapsed >= STALE_TRADE_HOURS:
                close_position(f"⏰ Stale {elapsed:.1f}h")
        return price

    # ── IDLE: HTF BOS must align with daily trend ─────────────────────────────
    # A 4H BOS against the daily trend is just a pullback — institutional money
    # won't sustain it. Only enter when daily and 4H agree on direction.
    # Exception: when daily is unclear/choppy (None), follow the 4H alone at
    # half position size — ranging markets still have tradeable SMC setups.
    if state.state == "IDLE" and not has_position:
        if is_bos and direction == "bullish" and daily_trend == "bullish":
            state.bias            = "BULLISH"
            state.ranging_mode    = False
            state.state           = "SWEEP_HUNT"
            state.sweep_hunt_bar  = 0
            print(f"[{base}] STEP 1: Daily BULLISH + 4H BOS → hunting sweep then FVG.")
        elif is_bos and direction == "bearish" and daily_trend == "bearish":
            state.bias            = "BEARISH"
            state.ranging_mode    = False
            state.state           = "SWEEP_HUNT"
            state.sweep_hunt_bar  = 0
            print(f"[{base}] STEP 1: Daily BEARISH + 4H BOS → hunting sweep then FVG.")
        elif is_bos and direction and daily_trend is None:
            state.bias            = direction.upper()
            state.ranging_mode    = True
            state.state           = "SWEEP_HUNT"
            state.sweep_hunt_bar  = 0
            print(f"[{base}] STEP 1 (ranging): 4H BOS {direction.upper()}, daily=unclear → "
                  f"hunting sweep at HALF size.")
        elif is_bos and direction and daily_trend and direction != daily_trend.lower():
            print(f"[{base}] 4H BOS {direction} blocked — daily trend is {daily_trend} (counter-trend skip)")

        # ── AMD counter-setup: reuse already-computed 4H sweep instead of a
        # separate detect_amd_phase() call — same signal, no disagreement.
        # Sweep of lows on 4H + bearish daily = stop hunt → bleed up → SHORT from supply.
        # Sweep of lows on 4H + bullish daily = standard pullback, handled by BOS path above.
        if state.state == "IDLE":
            if is_sweep_htf and daily_trend == 'bearish':
                found, sup_lo, sup_hi, sup_type = indicators.find_supply_zone(df_htf, price)
                if found:
                    state.bias               = "BEARISH"
                    state.sweep_low          = sweep_wick_htf
                    state.amd_phase          = 'manipulation_up'
                    state.amd_zone_type      = sup_type
                    state.fvg_low            = sup_lo
                    state.fvg_high           = sup_hi
                    state.ranging_mode       = False
                    state.state              = "ENTRY_WAIT"
                    state.bars_in_entry_wait = 0
                    print(f"[{base}] 🎯 AMD: 4H sweep ${sweep_wick_htf:,.4f} + daily BEARISH → "
                          f"[{sup_type}] ${sup_lo:,.2f}–${sup_hi:,.2f} → waiting for SHORT entry")

    # ── SWEEP_HUNT: require a liquidity sweep before FVG entry ────────────────
    # Crypto sweeps on 5m can take up to 4h to develop — allow 48 bars patience.
    # After that the BOS signal is stale and we reset to IDLE.
    SWEEP_PATIENCE = 48
    if state.state == "SWEEP_HUNT" and not has_position:
        state.sweep_hunt_bar += 1

        if is_sweep and sweep_wick:
            state.sweep_low = sweep_wick
            src = "4H" if is_sweep_htf else "5m"
            print(f"[{base}] Sweep confirmed [{src}] @ ${sweep_wick:,.4f} — hunting FVG/OB.")

        if state.sweep_hunt_bar > SWEEP_PATIENCE and not state.sweep_low:
            print(f"[{base}] No sweep in {SWEEP_PATIENCE} bars — BOS stale. Resetting.")
            state.reset()
            return price

        # Only advance to ENTRY_WAIT once sweep is confirmed
        if not state.sweep_low:
            return price

        if state.bias == "BULLISH" and is_fvg_bull:
            state.fvg_low  = fvg_bot
            state.fvg_high = fvg_top
            state.state    = "ENTRY_WAIT"
            print(f"[{base}] STEP 2: Bullish OB/FVG locked  "
                  f"${state.fvg_low:,.4f}-${state.fvg_high:,.4f}  "
                  f"sweep=${state.sweep_low:,.4f}")

        elif state.bias == "BEARISH" and is_fvg_bear:
            state.fvg_low  = fvg_bear_bot
            state.fvg_high = fvg_bear_top
            state.state    = "ENTRY_WAIT"
            print(f"[{base}] STEP 2: Bearish OB/FVG locked  "
                  f"${state.fvg_low:,.4f}-${state.fvg_high:,.4f}  "
                  f"sweep=${state.sweep_low:,.4f}")

    # ── ENTRY_WAIT: enter when price taps into FVG / supply / demand zone ───────
    if state.state == "ENTRY_WAIT" and not has_position:
        state.bars_in_entry_wait += 1
        expiry = AMD_ENTRY_WAIT_BARS if state.amd_phase else FVG_EXPIRY_BARS
        if state.bars_in_entry_wait > expiry:
            tag = "AMD zone" if state.amd_phase else "FVG"
            print(f"[{base}] {tag} expired after {expiry} bars — resetting.")
            state.reset()
            return price

        in_fvg = (state.fvg_low is not None and
                  state.fvg_low <= price <= state.fvg_high)

        if in_fvg:
            is_long = state.bias == "BULLISH"

            # 1. Calculate SL and risk distance
            if is_long:
                state.stop_loss = state.fvg_low * 0.997
                risk_amt        = price - state.stop_loss
                label = "LONG"
            else:
                state.stop_loss = state.fvg_high * 1.003
                risk_amt        = state.stop_loss - price
                label = "SHORT"

            # 2. Find the nearest 4H liquidity pool — informs the AI's R:R choice,
            #    but the AI (floored at 1:4) has final say on the actual target.
            bias_str = "bullish" if is_long else "bearish"
            pool_tp  = indicators.find_next_liquidity_target(df_htf, price, bias_str)

            # 3. Ask NVIDIA AI — give it the complete setup, let it pick R:R + approve/reject
            confirm, rr_actual, ai_reason = get_ai_confirmation(
                symbol, price, daily_trend, direction,
                state.fvg_low, state.fvg_high, state.sweep_low or 0,
                state.stop_loss, risk_amt, pool_tp, df_ltf, df_htf,
                amd_phase=state.amd_phase, zone_type=state.amd_zone_type,
            )
            state.take_profit = (price + risk_amt * rr_actual if is_long
                                 else price - risk_amt * rr_actual)
            icon = "✅ YES" if confirm else "❌ NO"
            print(f"[{base}] 🤖 AI Bot Approval: {icon}  R:R=1:{rr_actual:.1f}  {ai_reason[:140]}")

            # 4. Execute only if AI confirms
            if confirm:
                effective_fraction = risk_fraction * 0.5 if state.ranging_mode else risk_fraction
                risk_dollars  = paper.balance * effective_fraction
                qty_from_risk = risk_dollars / risk_amt if risk_amt > 0 else 0
                max_qty       = (paper.balance * 0.90) / price
                qty = math.floor(min(qty_from_risk, max_qty) * 1e6) / 1e6

                if qty > 0:
                    trade = paper.buy(symbol, qty, price) if is_long else paper.sell(symbol, qty, price)

                    if trade:
                        state.state       = "POSITION_OPEN"
                        state.entry_price = price
                        state.entry_time  = now
                        pos_value = qty * price
                        print(f"[{base}] ✅ PAPER {label}  ${price:,.2f}  qty={qty:.6f}  "
                              f"pos=${pos_value:,.2f}  risk=${risk_dollars:.2f}  "
                              f"SL=${state.stop_loss:,.2f}  TP=${state.take_profit:,.2f}  "
                              f"R:R=1:{rr_actual:.1f}  Balance: ${paper.balance:,.2f}")
            else:
                print(f"[{base}] AI rejected setup — staying in ENTRY_WAIT")

    return price


# ── Main ───────────────────────────────────────────────────────────────────────

def run():
    symbols_env = os.getenv("BINANCE_SYMBOL", "")
    symbols = ([s.strip() for s in symbols_env.split(",")]
               if "," in symbols_env else
               [symbols_env] if symbols_env else DEFAULT_SYMBOLS)

    print("=" * 70)
    print("DEBBIE-LA CCXT BOT — PAPER TRADING (real Kraken data)")
    print(f"  Symbols:  {', '.join(symbols)}")
    print(f"  Balance:  ${PAPER_BALANCE:,.0f} USDT (paper)")
    print(f"  Risk:     {BINANCE_CASH_AT_RISK*100:.1f}% per symbol | Interval: 5m | LTF: 5m | HTF: 6h")
    print("=" * 70)

    exchange     = connect_exchange()
    htf_exchange = connect_htf_exchange()
    paper  = PaperTrader(PAPER_BALANCE)
    states = {s: SymbolState() for s in symbols}
    load_crypto_state(paper, states, symbols)  # resume from last save if available

    prices = {}
    while True:
        print(f"\n{'─'*60}")
        print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"{'─'*60}")
        for symbol in symbols:
            try:
                result = process_symbol(exchange, paper, symbol, states[symbol],
                                        BINANCE_CASH_AT_RISK, htf_exchange=htf_exchange)
                if result is not None:
                    prices[symbol] = result
            except Exception as e:
                print(f"[{symbol}] Error: {e}")

        positions_str = (
            "  ".join(f"{k.split('/')[0]}={v:.4f}" for k, v in paper.positions.items())
            or "none"
        )
        print(f"\n  Balance: ${paper.balance:,.2f}  |  Positions: {positions_str}")
        save_crypto_state(paper, states, symbols, prices)
        print(f"  Sleeping {SLEEP_SECONDS // 60}m …\n")
        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    run()

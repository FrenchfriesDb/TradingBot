"""
CCXT bot — Debbie-La SMC state machine with paper trading.
Uses Kraken public data feed by default (no API key needed).
Set BINANCE_API_KEY/SECRET in .env to switch to real Binance trading.
"""

import time
import math
import os
import threading
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

from config import BINANCE_API_KEY, BINANCE_SECRET, BINANCE_TESTNET, BINANCE_CASH_AT_RISK, NVIDIA_API_KEY

# ── Background library loader ─────────────────────────────────────────────────
# macOS Gatekeeper rescans every .so file on the first import after a reboot —
# pandas (~16 min) + ccxt (~2 min) block the main thread if imported at the top.
# We load them in a daemon thread so the bot prints its banner and stays alive
# immediately. run() waits with a visible progress counter until ready.
pd          = None   # set by loader thread
ccxt        = None   # set by loader thread
indicators  = None   # set by loader thread
estimate_sentiment = None  # lazy-loaded on first trade (avoids FinBERT scan at startup)
_libs_ready = threading.Event()
_libs_start = time.time()

def _load_heavy_libs():
    global pd, ccxt, indicators
    _patch_ccxt()
    import pandas as _pd;  pd = _pd
    import ccxt as _ccxt;  ccxt = _ccxt
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "bot_indicators",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot", "indicators.py")
    )
    _m = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_m)
    indicators = _m
    _libs_ready.set()
    elapsed = (time.time() - _libs_start) / 60
    print(f"\n✅  All libraries ready ({elapsed:.1f} min) — starting trading loop\n", flush=True)

threading.Thread(target=_load_heavy_libs, daemon=True, name="lib-loader").start()

SLEEP_SECONDS = 5 * 60
STALE_TRADE_HOURS = 6   # intraday SMC: if trade hasn't resolved in 6h, setup is stale — exit
FVG_EXPIRY_BARS     = 12   # reset ENTRY_WAIT if price hasn't tapped FVG within this many iterations
AMD_ENTRY_WAIT_BARS = 96   # AMD supply/demand zones can take up to 8h to reach — longer patience
PAPER_BALANCE = 10_000.0
TRADE_ALERTS = True   # macOS sound + desktop notification + spoken alert on entry/exit


def alert(title, message, sound="Glass", speak=None):
    """Fire a macOS desktop notification + sound (+ optional spoken alert). Non-blocking
    (Popen, never waits) and wrapped so it can never crash or slow the trading loop."""
    if not TRADE_ALERTS:
        return
    try:
        import subprocess
        safe_msg   = message.replace('"', "'")
        safe_title = title.replace('"', "'")
        snd = f"/System/Library/Sounds/{sound}.aiff"
        # Sound FIRST — afplay needs no permissions (unlike notifications) so it always
        # rings. Play it twice back-to-back in a detached shell so it's hard to miss.
        subprocess.Popen(["sh", "-c", f"afplay '{snd}' 2>/dev/null; sleep 0.4; afplay '{snd}' 2>/dev/null"])
        # Desktop notification (needs Terminal/iTerm notification permission to appear).
        subprocess.Popen(["osascript", "-e",
            f'display notification "{safe_msg}" with title "{safe_title}" sound name "{sound}"'])
        if speak:
            subprocess.Popen(["say", speak])
    except Exception:
        pass

def trade_print(symbol: str, event: str, price: float,
                pnl: float = None, balance: float = None, extra: str = ""):
    """Print a visually prominent banner for trade events so they stand out in the log."""
    bar = "━" * 58
    lines = ["", bar, f"  {event} — {symbol} @ ${price:,.4f}"]
    if pnl is not None:
        row = f"  P&L: ${pnl:+.2f}"
        if balance is not None:
            row += f"   |   Balance: ${balance:,.2f}"
        lines.append(row)
    if extra:
        lines.append(f"  {extra}")
    lines += [bar, ""]
    print("\n".join(lines))


DEFAULT_SYMBOLS  = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "XRP/USD", "AVAX/USD", "POL/USD", "ADA/USD"]
CRYPTO_STATE_FILE = "crypto_state.json"

# Per-asset minimum 4H ATR to treat a market as "live" enough to trade.
# Large caps move slower in % terms — a ~1% 4H range on BTC is genuine volatility,
# while smaller alts need a higher bar to filter out chop/dead ranges. Alt floor set to
# 2.0% to match the current low-vol regime (alts running ~2.0–2.4% were all being skipped
# even on clear breakdowns); paired with the max(14-bar,3-bar) impulse override above.
ATR_GATE = {
    "BTC/USD": 0.010,
    "ETH/USD": 0.012,
}
ATR_GATE_DEFAULT = 0.020

def atr_gate_for(symbol: str) -> float:
    return ATR_GATE.get(symbol, ATR_GATE_DEFAULT)

# Reversal veto: how far price must have V-recovered off a swept extreme (over the
# last REVERSAL_WINDOW 5m bars) before we refuse to fade it. A swept low reclaimed by
# this much = bullish reversal → never short it; mirror for a swept high.
REVERSAL_RECLAIM_PCT = 0.035   # 3.5% reclaim off the window extreme
REVERSAL_WINDOW      = 54      # ~4.5h on 5m — wide enough to hold a multi-hour V

# SL cap: the structural SL (FVG edge + buffer) is correct for direction but is sized
# for the HTF zone, which on a 5m day trade can be 8-10× ATR away. Cap at 3× the
# current 5m ATR so the stop is in the trade's own timeframe and position sizing
# stays sane. 3× ATR gives plenty of room for normal 5m volatility without letting
# the HTF zone width dictate a swing-sized stop on an intraday trade.
SL_ATR_MULT     = 1.5   # SL placed 1.5× ATR outside the FVG edge (dynamic breathing room)
MAX_SL_ATR_MULT = 3.0   # hard cap: SL never more than 3× ATR from entry

# ── Simulated leverage (paper perps mode) ──────────────────────────────────────
# Set PAPER_LEVERAGE > 1 to simulate perpetual futures returns WITHOUT real
# liquidation risk. The paper trader multiplies P&L by this factor, and also
# tracks whether a loss would have liquidated a real perp position (warning only).
# Keep at 1 for pure spot simulation. Recommended progression: 1 → 3 → 5 → 10.
PAPER_LEVERAGE = 10   # 1 = spot (default). Change to 3, 5, or 10 to simulate perps.


# ── Paper trader ───────────────────────────────────────────────────────────────

class PaperTrader:
    """
    Simulates long AND short trades against real price data.
    Positions dict: positive qty = long, negative qty = short.
    Entry prices tracked separately for short P&L calculation.
    When PAPER_LEVERAGE > 1, P&L is multiplied to simulate perp returns,
    and a liquidation warning fires if a real exchange would have margin-called.
    """

    def __init__(self, balance: float = PAPER_BALANCE):
        self.balance = balance
        self.positions: dict = {}      # symbol -> qty (neg = short)
        self.entry_prices: dict = {}   # symbol -> avg entry price
        self.margin_used: dict = {}    # symbol -> margin posted (for liq tracking)
        self.trade_count = 0
        self.daily_margin = 0.0        # total margin deployed today (reset each UTC day)
        self.daily_date   = None       # UTC date of last reset

    def check_daily_cap(self, margin_needed: float, daily_cap_frac: float = 0.05) -> float:
        """Return how much margin is actually available under the daily cap (5% of balance)."""
        today = datetime.now(timezone.utc).date()
        if self.daily_date != today:
            self.daily_margin = 0.0
            self.daily_date   = today
        cap = self.balance * daily_cap_frac
        return max(0.0, cap - self.daily_margin)

    def record_margin(self, margin: float):
        """Call after an entry is confirmed to count margin against today's cap."""
        self.daily_margin += margin

    def get_position(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def buy(self, symbol: str, qty: float, price: float):
        """Open long, or cover an existing short."""
        held = self.positions.get(symbol, 0.0)
        if held < 0:
            # Cover short: return margin + leveraged P&L
            cover = min(qty, abs(held))
            frac  = cover / abs(held)
            full_margin     = self.margin_used.get(symbol, abs(held) * self.entry_prices.get(symbol, price) / PAPER_LEVERAGE)
            margin_returned = full_margin * frac
            raw_pnl = (self.entry_prices.get(symbol, price) - price) * cover
            # qty already = margin × PAPER_LEVERAGE / price, so raw_pnl IS the leveraged P&L
            if raw_pnl < 0 and abs(raw_pnl) >= margin_returned:
                print(f"[PAPER] ⚡ LIQUIDATION (simulated) — loss ${abs(raw_pnl):.2f} "
                      f"exceeds margin ${margin_returned:.2f} at {PAPER_LEVERAGE}x leverage. "
                      f"Real perp would be liquidated here.")
                self.balance += 0   # margin already gone; no recovery
            else:
                self.balance += margin_returned + raw_pnl
            new_held = held + cover
            if abs(new_held) < 1e-9:
                self.positions.pop(symbol, None)
                self.entry_prices.pop(symbol, None)
                self.margin_used.pop(symbol, None)
            else:
                self.positions[symbol] = new_held
                self.margin_used[symbol] = full_margin * (1 - frac)   # pro-rate remaining margin
        else:
            # Open long — margin = cost/leverage, full position controlled
            cost = qty * price / PAPER_LEVERAGE   # only post margin
            if cost > self.balance:
                qty = math.floor((self.balance * 0.95 * PAPER_LEVERAGE / price) * 1e6) / 1e6
                cost = qty * price / PAPER_LEVERAGE
            if qty <= 0:
                return None
            self.balance -= cost
            self.positions[symbol] = held + qty
            self.entry_prices[symbol] = price
            self.margin_used[symbol] = cost
        self.trade_count += 1
        return {"id": self.trade_count, "qty": qty, "price": price}

    def sell(self, symbol: str, qty: float, price: float):
        """Close an existing long, or open a short."""
        held = self.positions.get(symbol, 0.0)
        if held > 0:
            # Close long: return entry-price margin + leveraged P&L
            qty = min(qty, held)
            if qty <= 0:
                return None
            frac  = qty / held
            full_margin     = self.margin_used.get(symbol, held * self.entry_prices.get(symbol, price) / PAPER_LEVERAGE)
            margin_returned = full_margin * frac
            raw_pnl = (price - self.entry_prices.get(symbol, price)) * qty
            # qty already = margin × PAPER_LEVERAGE / price, so raw_pnl IS the leveraged P&L
            if raw_pnl < 0 and abs(raw_pnl) >= margin_returned:
                print(f"[PAPER] ⚡ LIQUIDATION (simulated) — loss ${abs(raw_pnl):.2f} "
                      f"exceeds margin ${margin_returned:.2f} at {PAPER_LEVERAGE}x leverage.")
                self.balance += 0   # margin lost; no recovery
            else:
                self.balance += margin_returned + raw_pnl
            new_held = held - qty
            if new_held < 1e-9:
                self.positions.pop(symbol, None)
                self.entry_prices.pop(symbol, None)
                self.margin_used.pop(symbol, None)
            else:
                self.positions[symbol] = new_held
                self.margin_used[symbol] = full_margin * (1 - frac)   # pro-rate remaining margin
        else:
            # Open short (paper) — margin posted = position_value / leverage
            margin = qty * price / PAPER_LEVERAGE
            if margin > self.balance:
                qty = math.floor((self.balance * 0.95 * PAPER_LEVERAGE / price) * 1e6) / 1e6
                margin = qty * price / PAPER_LEVERAGE
            if qty <= 0:
                return None
            self.balance -= margin
            self.positions[symbol] = -qty
            self.entry_prices[symbol] = price
            self.margin_used[symbol] = margin
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
        ex = ccxt.coinbase({"enableRateLimit": True, "timeout": 8000})
        mode = "Coinbase (public data — paper trades only)"
    print(f"Exchange: {mode}  |  Chart display: Coinbase")
    return ex


def connect_htf_exchange():
    """
    Bybit public API for 4H candles — no account needed, supports 4H granularity.
    Coinbase only goes up to 1H and 6H; Bybit has true 4H which is the standard
    SMC institutional timeframe for BOS detection.
    Short timeout (4s) so US-based geo-blocks fail fast instead of hanging 30+ min.
    """
    try:
        ex = ccxt.bybit({"enableRateLimit": True, "timeout": 4000})
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


def fetch_ohlcv_retry(exchange, symbol, timeframe, limit, attempts=3, backoff=1.0):
    """Fetch OHLCV with retries. The FIRST request after the 5-min sleep often lands on
    a stale keep-alive socket (it's always the first symbol — BTC — that takes the hit),
    fails once, then the connection re-establishes. A quick retry recovers it instead of
    skipping the symbol for the whole cycle."""
    last_err = None
    for i in range(attempts):
        try:
            return exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(backoff * (i + 1))   # 1s, then 2s
    raise last_err


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
            margin = paper.margin_used.get(sym, abs(qty) * entry / PAPER_LEVERAGE)
            entry_time = states[sym].entry_time
            positions[sym] = {
                "qty":            qty,
                "side":           "LONG" if qty > 0 else "SHORT",
                "entry_price":    entry,
                "entry_time":     entry_time.isoformat() if entry_time else None,
                "current_price":  cur,
                "stop_loss":      sl,
                "take_profit":    tp,
                "unrealized_pnl": upnl,
                "risk_dollars":   risk,
                "reward_dollars": reward,
                "margin":         margin,
                "leverage":       PAPER_LEVERAGE,
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
                "partial_taken":      st.partial_taken,
                "breakeven_moved":    st.breakeven_moved,
                "zone_set_price":     st.zone_set_price,
                "eql_level":          st.eql_level,
                "eql_touch":          st.eql_touch,
                "eqh_level":          st.eqh_level,
                "eqh_touch":          st.eqh_touch,
                "trendline":          st.trendline,
                "sniper_armed":       st.sniper_armed,
                "sniper_sl":          st.sniper_sl,
                "sniper_tp":          st.sniper_tp,
                "sniper_qty":         st.sniper_qty,
                "sniper_margin":      st.sniper_margin,
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
        paper.margin_used  = {}
        for sym, pos in data.get("positions", {}).items():
            paper.positions[sym]    = pos["qty"]
            paper.entry_prices[sym] = pos["entry_price"]
            # Restore actual margin posted so leverage changes between restarts
            # don't cause wrong margin returns on close.
            if pos.get("margin") is not None:
                paper.margin_used[sym] = pos["margin"]

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
            st.partial_taken      = saved.get("partial_taken", False)
            st.breakeven_moved    = saved.get("breakeven_moved", False)
            st.zone_set_price     = saved.get("zone_set_price")
            raw_time              = saved.get("entry_time")
            st.entry_time         = (datetime.fromisoformat(raw_time).replace(tzinfo=timezone.utc)
                                     if raw_time else None)
            st.sniper_armed       = saved.get("sniper_armed", False)
            st.sniper_sl          = saved.get("sniper_sl")
            st.sniper_tp          = saved.get("sniper_tp")
            st.sniper_qty         = saved.get("sniper_qty")
            st.sniper_margin      = saved.get("sniper_margin")
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
        global estimate_sentiment
        if estimate_sentiment is None:
            from finbert_utils import estimate_sentiment as _es
            estimate_sentiment = _es
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
        self.partial_taken = False    # True after scaling out the first 50% tranche
        self.breakeven_moved = False  # True after SL trailed to break-even
        self.zone_set_price = None    # price when a trend_follow zone was armed (for stale-zone invalidation)
        self.eql_level = None; self.eql_touch = 0   # latest equal-lows pool (for chart overlay)
        self.eqh_level = None; self.eqh_touch = 0   # latest equal-highs pool (for chart overlay)
        self.trendline = None    # {kind,t1,p1,t2,p2} diagonal trendline (for chart overlay)
        self.ai_reject_count = 0  # consecutive AI rejections; reset to IDLE at threshold
        self.last_tap_candle    = None  # suppress repeated zone-tap prints
        self.last_choch_aligned = None  # suppress repeated CHoCH-waiting prints
        # ── 10-second entry sniper ──────────────────────────────────────────────
        # Armed by 5-min cycle when zone is identified; fired by 10-sec loop
        # the instant price enters the zone (no waiting for next 5-min tick).
        self.sniper_armed  = False   # True = ready to fire on zone touch
        self.sniper_sl     = None    # pre-calculated SL at arm time
        self.sniper_tp     = None    # pre-calculated TP at arm time
        self.sniper_qty    = None    # pre-calculated qty at arm time
        self.sniper_margin = None    # pre-calculated margin at arm time

    def reset(self):
        self.__init__()


# ── Open-trade management: scale-out + break-even ──────────────────────────────

def manage_open_trade(paper, state, symbol, cur_price, base):
    """Defeat the round-trip heartbreak. Runs from BOTH the 5-min loop and the 10s
    watcher (so break-even moves fast enough to actually catch a reversal):
      • SCALE OUT 50% once price is halfway to target — banks profit, lets the rest run.
      • BREAK-EVEN at 85% of the way — a near-miss can no longer turn into a loss.
    Idempotent: each action fires at most once per trade (flags on SymbolState)."""
    if state.state != "POSITION_OPEN" or not state.entry_price or not state.take_profit:
        return
    held = paper.get_position(symbol)
    if abs(held) < 1e-9:
        return
    is_long = held > 0
    entry   = state.entry_price
    tp_dist = abs(state.take_profit - entry)
    if tp_dist <= 0:
        return
    favorable = (is_long and cur_price > entry) or (not is_long and cur_price < entry)
    progress  = (abs(cur_price - entry) / tp_dist) if favorable else 0.0

    # 1. Scale out 50% at the halfway mark.
    if progress >= 0.50 and not state.partial_taken:
        half = math.floor((abs(held) * 0.5) * 1e6) / 1e6
        if half > 0:
            if is_long:
                paper.sell(symbol, half, cur_price); pnl = (cur_price - entry) * half
            else:
                paper.buy(symbol, half, cur_price);  pnl = (entry - cur_price) * half
            state.partial_taken = True
            left = abs(paper.get_position(symbol))
            lev_tag = f"[{PAPER_LEVERAGE}x]" if PAPER_LEVERAGE > 1 else ""
            trade_print(base, "💰 SCALED OUT 50%", cur_price,
                        pnl=pnl, balance=paper.balance,
                        extra=f"{lev_tag}  {left:,.4f} remaining")
            alert(f"💰 SCALED OUT — {base}",
                  f"50% locked @ ${cur_price:,.4f}  +${pnl:.2f}{lev_tag}  ({left:.4f} left)",
                  sound="Ping",
                  speak=f"{base} halfway. Scaled out fifty percent. Locked {abs(pnl):.0f} dollars.")

    # 2. Trail SL to break-even (+ small fee buffer) at 60% of the way to target.
    # 85% was too close to TP — in choppy post-spike markets price would bounce from
    # 85% progress back to entry, closing the trade at 0 before ever reaching TP.
    # 60% gives the same protection (no losing a winner) but locks it in much earlier,
    # so the distance from break-even SL to current price is larger and harder to wick through.
    if progress >= 0.60 and not state.breakeven_moved:
        state.stop_loss = entry * (1.001 if is_long else 0.999)
        state.breakeven_moved = True
        print(f"[{base}] 🛡 60% to target — SL trailed to break-even ${state.stop_loss:,.4f} "
              f"(winner locked in — can no longer close at a loss)")


# ── NVIDIA AI trade confirmation ───────────────────────────────────────────────

MIN_AI_RR = 2.0   # hard floor — 1:2 minimum keeps TP reachable intraday on 5m entries
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
    elif amd_phase == 'trend_follow':
        zone_label = 'supply' if side == 'SHORT' else 'demand'
        amd_context = (
            f"AMD Phase    : TREND_FOLLOW (half size — no sweep required)\n"
            f"Zone Type    : {zone_type or zone_label}\n"
            f"Narrative    : Daily trend is clearly {('bearish' if side == 'SHORT' else 'bullish')}. "
            f"Price pulled back into a {zone_type or zone_label} zone.\n"
            f"               This is a trend-continuation entry — no liquidity sweep needed.\n"
            f"               Zone: ${fvg_low:,.4f} – ${fvg_high:,.4f}  SL: ${sl:,.4f}"
        )
        amd_question = (
            f"- Is the daily trend clearly {'bearish' if side == 'SHORT' else 'bullish'} on the 4H chart?\n"
            f"- Is price rejecting from the {zone_type or zone_label} zone "
            f"${fvg_low:,.4f}–${fvg_high:,.4f} with bearish/bullish structure?\n"
            f"- Is there enough room to the next liquidity pool to justify a 1:3.5+ R:R?"
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

    import concurrent.futures

    def _call_ai():
        from openai import OpenAI
        import re
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

    # Use shutdown(wait=False) so a timeout never blocks the main loop.
    # The `with` form calls shutdown(wait=True) on exit, which hangs forever
    # if the thread is stuck on a Gatekeeper scan of openai's .so files.
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    _future   = _executor.submit(_call_ai)
    try:
        result = _future.result(timeout=25)
        _executor.shutdown(wait=False)
        return result
    except concurrent.futures.TimeoutError:
        _executor.shutdown(wait=False)
        print(f"  ⚠️  AI call timed out (25s) — approving at minimum R:R", flush=True)
        return True, MIN_AI_RR, "AI timeout (25s) — proceeding at minimum R:R"
    except Exception as e:
        _executor.shutdown(wait=False)
        return True, MIN_AI_RR, f"API error ({e}) — proceeding at minimum 1:4 R:R"


# ── Core strategy loop per symbol ─────────────────────────────────────────────

def process_symbol(exchange, paper: PaperTrader, symbol: str,
                   state: SymbolState, risk_fraction: float,
                   htf_exchange=None):
    base = symbol.split("/")[0]
    now = datetime.now(timezone.utc)

    try:
        df_ltf    = ohlcv_to_df(fetch_ohlcv_retry(exchange, symbol, "5m",  120))  # execution + ~10h of liquidity pools
        df_ltf_15 = ohlcv_to_df(fetch_ohlcv_retry(exchange, symbol, "15m", 55))   # MSS / CHoCH confirmation
        df_daily  = ohlcv_to_df(fetch_ohlcv_retry(exchange, symbol, "1d",  60))
        # Bybit uses USDT pairs — fetch both 4H (high conviction) and 1H (faster signals)
        if htf_exchange:
            bybit_sym = symbol.replace("/USD", "/USDT")
            df_htf    = ohlcv_to_df(fetch_ohlcv_retry(htf_exchange, bybit_sym, "4h", 200))
            df_htf_1h = ohlcv_to_df(fetch_ohlcv_retry(htf_exchange, bybit_sym, "1h", 200))
        else:
            df_htf    = ohlcv_to_df(fetch_ohlcv_retry(exchange, symbol, "6h", 200))
            df_htf_1h = ohlcv_to_df(fetch_ohlcv_retry(exchange, symbol, "1h", 200))
    except Exception as e:
        print(f"[{base}] Data error (after retries): {e}")
        return None

    price = float(df_ltf["close"].iloc[-1])
    held  = paper.get_position(symbol)
    has_position = abs(held) > 1e-6

    daily_trend = indicators.get_daily_trend(df_daily)

    # Dual HTF BOS: 4H = full conviction, 1H = faster signal at half size
    is_bos_4h, direction_4h, _ = indicators.detect_displacement_bos(df_htf,    lookback=15)
    is_bos_1h, direction_1h, _ = indicators.detect_displacement_bos(df_htf_1h, lookback=15)
    is_bos    = is_bos_4h or is_bos_1h
    direction = direction_4h if is_bos_4h else direction_1h
    bos_tf    = "4H" if is_bos_4h else ("1H" if is_bos_1h else "—")
    is_1h_only = is_bos_1h and not is_bos_4h   # half size when only 1H confirms

    # Candle pattern on the latest 5m bar
    candle_type = indicators.classify_candle(df_ltf.iloc[-1], df_ltf.iloc[-2])

    # Check sweep + FVG on BOTH 5m (precise entry) and 4H (macro setup)
    # 4H signals fire first when the entire setup is on the higher timeframe
    is_sweep_ltf, _, sweep_wick_ltf = indicators.check_liquidity_sweep(df_ltf)
    is_sweep_htf, htf_support_level, sweep_wick_htf = indicators.check_liquidity_sweep(df_htf, sweep_window=5)
    is_sweep   = is_sweep_ltf or is_sweep_htf
    sweep_wick = sweep_wick_htf if is_sweep_htf else sweep_wick_ltf

    # High-side (buy-side liquidity) sweeps — needed for SHORT setups and the
    # bullish manipulation_down AMD leg (sweep highs → bleed down → long demand).
    is_sweep_high_ltf, _, sweep_high_wick_ltf = indicators.check_liquidity_sweep_high(df_ltf)
    is_sweep_high_htf, htf_resistance_level, sweep_high_wick_htf = indicators.check_liquidity_sweep_high(df_htf, sweep_window=5)
    is_sweep_high   = is_sweep_high_ltf or is_sweep_high_htf
    sweep_high_wick = sweep_high_wick_htf if is_sweep_high_htf else sweep_high_wick_ltf

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

    # Equal-lows / equal-highs liquidity pools — the wick shelves where stops cluster.
    # LTF (5m, ~10h back to match the live chart); HTF = macro pools the sweep logic hunts.
    eql_found, eql_level, eql_touch = indicators.detect_equal_lows(df_ltf, lookback=100)
    eqh_found, eqh_level, eqh_touch = indicators.detect_equal_highs(df_ltf, lookback=100)
    eql_h_found, eql_h_level, _ = indicators.detect_equal_lows(df_htf)
    eqh_h_found, eqh_h_level, _ = indicators.detect_equal_highs(df_htf)
    # Stash for the chart overlay (so the live chart can draw the pools the bot sees)
    state.eql_level = eql_level if eql_found else None
    state.eql_touch = eql_touch if eql_found else 0
    state.eqh_level = eqh_level if eqh_found else None
    state.eqh_touch = eqh_touch if eqh_found else 0
    # Diagonal trendline (ascending support / descending resistance) for the chart overlay
    tl_found, tl_kind, tl_a, tl_b = indicators.detect_trendline(df_ltf)
    state.trendline = ({"kind": tl_kind, "t1": tl_a[0], "p1": tl_a[1],
                        "t2": tl_b[0], "p2": tl_b[1]} if tl_found else None)

    tf_tag = "4H" if (is_sweep_htf or is_fvg_bull_htf or is_fvg_bear_htf) else "5m"
    zone_tag = ""
    if state.state == "ENTRY_WAIT" and state.fvg_low and state.fvg_high:
        tag = "AMD" if state.amd_phase else "FVG"
        in_zone = state.fvg_low <= price <= state.fvg_high
        zone_tag = (f"  [{tag} zone ${state.fvg_low:,.2f}–${state.fvg_high:,.2f} "
                    f"{'✅IN' if in_zone else '⏳waiting'}]")
    eq_tag = ""
    if eql_found:
        eq_tag += f"  EQL=${eql_level:,.2f}({eql_touch})"
    if eqh_found:
        eq_tag += f"  EQH=${eqh_level:,.2f}({eqh_touch})"
    print(f"[{base}] {now.strftime('%H:%M')} ${price:,.2f}  "
          f"daily={daily_trend}  state={state.state}  bos={is_bos}({direction}/{bos_tf})  "
          f"sweepL={is_sweep}  sweepH={is_sweep_high}  fvg_bull={is_fvg_bull}  fvg_bear={is_fvg_bear}  "
          f"candle={candle_type}"
          f"{eq_tag}{zone_tag}")

    # ── POSITION_OPEN ──────────────────────────────────────────────────────────
    if state.state == "POSITION_OPEN":
        if not has_position:
            print(f"[{base}] Position gone. Resetting.")
            state.reset()
            return price

        is_long = held > 0

        def close_position(reason):
            # Re-read live qty so we close whatever actually remains (covers a prior scale-out)
            qty_now = abs(paper.get_position(symbol))
            if qty_now < 1e-9:
                state.reset(); return
            if is_long:
                paper.sell(symbol, qty_now, price)
                pnl = (price - state.entry_price) * qty_now
            else:
                paper.buy(symbol, qty_now, price)
                pnl = (state.entry_price - price) * qty_now
            direction_label = "LONG" if is_long else "SHORT"
            lev_tag = f"[{PAPER_LEVERAGE}x]" if PAPER_LEVERAGE > 1 else ""
            won = pnl >= 0
            trade_print(f"{base} {direction_label}", f"{reason}",
                        price, pnl=pnl, balance=paper.balance,
                        extra=lev_tag)
            alert(f"{'🟢 TP' if won else '🔴 SL'} — {base} {direction_label} closed",
                  f"P&L ${pnl:+.2f}  Balance ${paper.balance:,.2f}",
                  sound="Glass" if won else "Basso",
                  speak=f"{base} closed. {'Profit' if won else 'Loss'} {abs(pnl):.0f} dollars")
            state.reset()

        # Trade management first — scale out 50% at halfway, trail SL to break-even at 85%.
        manage_open_trade(paper, state, symbol, price, base)

        # Use candle high/low so SL/TP fire on the wick — same as a real resting order
        candle_high = float(df_ltf["high"].iloc[-1])
        candle_low  = float(df_ltf["low"].iloc[-1])
        sl_hit = (is_long and candle_low  <= state.stop_loss) or \
                 (not is_long and candle_high >= state.stop_loss)
        tp_hit = (is_long and candle_high >= state.take_profit) or \
                 (not is_long and candle_low  <= state.take_profit)

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
        # ATR filter shared by all entry paths — dead/ranging markets produce false signals.
        # Use max(14-bar average, recent 3-bar) so a FRESH impulse (a sharp move the slow
        # 14-bar average hasn't caught up to yet) isn't mislabeled "consolidating" and skipped.
        rng     = df_htf['high'] - df_htf['low']
        atr_14  = rng.rolling(14).mean().iloc[-1]
        atr_3   = rng.rolling(3).mean().iloc[-1]
        atr_pct = max(atr_14, atr_3) / price
        atr_min = atr_gate_for(symbol)

        # ── FIX #2: Accumulation guard runs FIRST, gating every path below ────────
        # A tight HTF range = chop / institutions still accumulating. Don't trade
        # EITHER side until it breaks. Without this first, a 15-bar "BOS" printed
        # inside a 25-bar range whipsaws us. Log the range and bail for this cycle.
        amd_phase_now, amd_info_now = indicators.detect_amd_phase(df_htf)
        if amd_phase_now == 'accumulation':
            print(f"[{base}] 📦 Accumulation: range ${amd_info_now['range_low']:,.2f}–"
                  f"${amd_info_now['range_high']:,.2f} ({amd_info_now['range_pct']:.1%} wide) "
                  f"— chop, no entry until breakout/sweep")
            return price

        # ── FIX #1: BOS handling — trade the displacement retest, don't demand a sweep ──
        # When a BOS leaves a displacement FVG, lock that gap and go straight to
        # ENTRY_WAIT for the retest. This is how clean breakdowns/breakouts get traded:
        # an impulsive move has no opposing sweep, so requiring one (old behaviour) made
        # the bot blind to its best continuation setups. Fall back to SWEEP_HUNT only
        # when the move left no displacement gap.
        bos_aligned = is_bos and direction and (daily_trend == direction or daily_trend is None)
        bos_counter = is_bos and direction and daily_trend and daily_trend != direction

        if bos_aligned and atr_pct < atr_min:
            print(f"[{base}] BOS skip — consolidating (4H ATR={atr_pct:.1%} < {atr_min:.1%})")
        elif bos_aligned:
            state.bias         = direction.upper()
            state.ranging_mode = (daily_trend is None) or is_1h_only
            size_tag = "HALF" if state.ranging_mode else "FULL"
            disp_found, disp_dir, disp_lo, disp_hi, _ = indicators.detect_displacement_fvg(df_htf)
            if disp_found and disp_dir == direction:
                state.fvg_low            = disp_lo
                state.fvg_high           = disp_hi
                state.amd_zone_type      = "choch_fvg"     # displacement gap = the CHoCH zone
                state.state              = "ENTRY_WAIT"
                state.bars_in_entry_wait = 0
                print(f"[{base}] STEP 1+2: {bos_tf} BOS ({direction}) + displacement FVG "
                      f"${disp_lo:,.4f}–${disp_hi:,.4f} → ENTRY_WAIT retest [{size_tag} size]")
            else:
                state.state          = "SWEEP_HUNT"
                state.sweep_hunt_bar = 0
                print(f"[{base}] STEP 1: {bos_tf} BOS ({direction}) → hunting sweep [{size_tag} size]")
        elif bos_counter:
            print(f"[{base}] {bos_tf} BOS {direction} blocked — daily trend is {daily_trend} (counter-trend skip)")

        # ── AMD counter-setup: a 4H sweep + aligned daily trend = stop hunt → reversal.
        # Sweep of LOWS  + bearish daily = bleed up → SHORT from supply (manipulation_up).
        # Sweep of HIGHS + bullish daily = bleed down → LONG  from demand (manipulation_down).
        if state.state == "IDLE":
            if is_sweep_htf and daily_trend == 'bearish':
                # Guard 1: per-asset ATR consolidation filter
                if atr_pct < atr_min:
                    print(f"[{base}] AMD skip — consolidating (4H ATR={atr_pct:.1%} < {atr_min:.1%})")
                    return price
                # Guard 2: sweep wick must pierce support by ≥0.3% (real stop hunt, not noise)
                sweep_depth = ((htf_support_level - sweep_wick_htf) / htf_support_level
                               if htf_support_level and htf_support_level > 0 else 0)
                if sweep_depth < 0.003:
                    print(f"[{base}] AMD skip — sweep too shallow ({sweep_depth:.2%} < 0.3%)")
                    return price
                # Liquidity confluence: was the swept level an actual EQL pool? Higher conviction.
                pool_note = ""
                if eql_h_found and eql_h_level and abs(sweep_wick_htf - eql_h_level) / eql_h_level <= 0.005:
                    pool_note = f"  📍 swept EQL pool ${eql_h_level:,.2f}"
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
                    print(f"[{base}] 🎯 AMD: 4H low-sweep ${sweep_wick_htf:,.4f} + daily BEARISH → "
                          f"[{sup_type}] ${sup_lo:,.2f}–${sup_hi:,.2f} → waiting for SHORT entry{pool_note}")
                else:
                    print(f"[{base}] AMD sweep valid but no supply zone found above ${price:,.2f} — staying IDLE")

            elif is_sweep_high_htf and daily_trend == 'bullish':
                # Guard 1: per-asset ATR consolidation filter
                if atr_pct < atr_min:
                    print(f"[{base}] AMD skip — consolidating (4H ATR={atr_pct:.1%} < {atr_min:.1%})")
                    return price
                # Guard 2: sweep wick must pierce resistance by ≥0.3% (real stop hunt above)
                sweep_depth = ((sweep_high_wick_htf - htf_resistance_level) / htf_resistance_level
                               if htf_resistance_level and htf_resistance_level > 0 else 0)
                if sweep_depth < 0.003:
                    print(f"[{base}] AMD skip — high-sweep too shallow ({sweep_depth:.2%} < 0.3%)")
                    return price
                # Liquidity confluence: was the swept level an actual EQH pool? Higher conviction.
                pool_note = ""
                if eqh_h_found and eqh_h_level and abs(sweep_high_wick_htf - eqh_h_level) / eqh_h_level <= 0.005:
                    pool_note = f"  📍 swept EQH pool ${eqh_h_level:,.2f}"
                found, dem_lo, dem_hi, dem_type = indicators.find_demand_zone(df_htf, price)
                if found:
                    state.bias               = "BULLISH"
                    state.sweep_low          = sweep_high_wick_htf
                    state.amd_phase          = 'manipulation_down'
                    state.amd_zone_type      = dem_type
                    state.fvg_low            = dem_lo
                    state.fvg_high           = dem_hi
                    state.ranging_mode       = False
                    state.state              = "ENTRY_WAIT"
                    state.bars_in_entry_wait = 0
                    print(f"[{base}] 🎯 AMD: 4H high-sweep ${sweep_high_wick_htf:,.4f} + daily BULLISH → "
                          f"[{dem_type}] ${dem_lo:,.2f}–${dem_hi:,.2f} → waiting for LONG entry{pool_note}")
                else:
                    print(f"[{base}] AMD high-sweep valid but no demand zone found below ${price:,.2f} — staying IDLE")

        # ── Trend-zone fallback: no fresh BOS/sweep but daily trend is clear ──────
        # Find the nearest supply/demand zone and wait for price to pull back into it.
        # Lower conviction than AMD/BOS → half position size (ranging_mode=True).
        if state.state == "IDLE" and atr_pct >= atr_min:
            # Accumulation already filtered at the top of IDLE (returns early), so any
            # symbol reaching here is NOT in a tight range — safe to seek a trend zone.
            if daily_trend == "bearish":
                found, sup_lo, sup_hi, sup_type = indicators.find_supply_zone(
                    df_htf, price, max_distance_pct=0.08
                )
                if found:
                    state.bias               = "BEARISH"
                    state.amd_phase          = "trend_follow"
                    state.amd_zone_type      = sup_type
                    state.fvg_low            = sup_lo
                    state.fvg_high           = sup_hi
                    state.ranging_mode       = True   # half size — no fresh BOS/sweep
                    state.state              = "ENTRY_WAIT"
                    state.bars_in_entry_wait = 0
                    state.zone_set_price     = price
                    print(f"[{base}] 📊 Trend zone: daily BEARISH → [{sup_type}] "
                          f"${sup_lo:,.2f}–${sup_hi:,.2f} → SHORT on rally (½ size)")
            elif daily_trend == "bullish":
                found, dem_lo, dem_hi, dem_type = indicators.find_demand_zone(
                    df_htf, price, max_distance_pct=0.08
                )
                if found:
                    state.bias               = "BULLISH"
                    state.amd_phase          = "trend_follow"
                    state.amd_zone_type      = dem_type
                    state.fvg_low            = dem_lo
                    state.fvg_high           = dem_hi
                    state.ranging_mode       = True
                    state.state              = "ENTRY_WAIT"
                    state.bars_in_entry_wait = 0
                    state.zone_set_price     = price
                    print(f"[{base}] 📊 Trend zone: daily BULLISH → [{dem_type}] "
                          f"${dem_lo:,.2f}–${dem_hi:,.2f} → LONG on pullback (½ size)")

    # ── SWEEP_HUNT: require a liquidity sweep before FVG entry ────────────────
    # Crypto sweeps on 5m can take up to 4h to develop — allow 48 bars patience.
    # After that the BOS signal is stale and we reset to IDLE.
    SWEEP_PATIENCE = 48
    if state.state == "SWEEP_HUNT" and not has_position:
        state.sweep_hunt_bar += 1

        # Direction-aware sweep: a SHORT wants buy-side liquidity (highs) swept,
        # a LONG wants sell-side liquidity (lows) swept. Matching the sweep to the
        # bias filters out the wrong-side grab that precedes the opposite move.
        if state.bias == "BEARISH":
            sweep_ok, sweep_lvl, sweep_src = is_sweep_high, sweep_high_wick, ("4H" if is_sweep_high_htf else "5m")
        else:
            sweep_ok, sweep_lvl, sweep_src = is_sweep, sweep_wick, ("4H" if is_sweep_htf else "5m")

        if sweep_ok and sweep_lvl:
            state.sweep_low = sweep_lvl
            print(f"[{base}] Sweep confirmed [{sweep_src}] @ ${sweep_lvl:,.4f} — hunting FVG/OB.")

        if state.sweep_hunt_bar > SWEEP_PATIENCE and not state.sweep_low:
            print(f"[{base}] No sweep in {SWEEP_PATIENCE} bars — BOS stale. Resetting.")
            state.reset()
            return price

        # Only advance to ENTRY_WAIT once sweep is confirmed
        if not state.sweep_low:
            return price

        # Preferred: the FVG left by the post-sweep displacement that broke structure.
        # That gap IS the CHoCH — we trade its retest. Falls back to a generic FVG/OB
        # only if no clean displacement gap exists yet.
        want = "bullish" if state.bias == "BULLISH" else "bearish"
        disp_found, disp_dir, disp_lo, disp_hi, _ = indicators.detect_displacement_fvg(df_ltf)

        if disp_found and disp_dir == want:
            state.fvg_low       = disp_lo
            state.fvg_high      = disp_hi
            state.amd_zone_type = "choch_fvg"      # mark: FVG == CHoCH → retest-rebounce entry
            state.state         = "ENTRY_WAIT"
            print(f"[{base}] STEP 2: 🎯 CHoCH FVG (displacement) locked  "
                  f"${state.fvg_low:,.4f}-${state.fvg_high:,.4f}  "
                  f"sweep=${state.sweep_low:,.4f} — waiting for retest/refill")

        elif state.bias == "BULLISH" and is_fvg_bull:
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

    # ── Falling wedge breakout (cross-state) ─────────────────────────────────────
    # Detects descending highs + ascending lows converging, then price breaking
    # above the resistance line. Works from any state except POSITION_OPEN:
    #  • IDLE / SWEEP_HUNT       → arms a long retest ENTRY_WAIT immediately
    #  • ENTRY_WAIT (bearish)    → abandons the bearish setup, arms the long instead
    #  • ENTRY_WAIT (bullish)    → already set up, skip
    if not has_position and state.state != "POSITION_OPEN":
        _do_wedge = (state.state in ("IDLE", "SWEEP_HUNT") or
                     (state.state == "ENTRY_WAIT" and state.bias == "BEARISH"))
        if _do_wedge:
            _wbk, _wsl, _wbl = indicators.detect_falling_wedge(df_ltf)
            if _wbk and _wsl is not None:
                was_bearish = state.bias == "BEARISH"
                state.reset()
                state.bias           = "BULLISH"
                state.amd_phase      = "wedge_breakout"
                state.amd_zone_type  = "wedge_retest"
                # Entry zone: from the last ascending low up to just above broken resistance.
                # fvg_low becomes the SL reference (structural stop just below last higher low).
                state.fvg_low        = round(_wsl, 8)
                state.fvg_high       = round(_wbl * 1.005, 8)
                state.zone_set_price = price
                state.state          = "ENTRY_WAIT"
                pfx = "bearish setup abandoned → " if was_bearish else ""
                print(f"[{base}] 🔺 FALLING WEDGE BREAKOUT  {pfx}"
                      f"LONG retest zone ${_wsl:,.4f}–${_wbl*1.005:,.4f}  "
                      f"(last ascending low=${_wsl:,.4f}  broken resistance=${_wbl:,.4f})")
                return price

    # ── ENTRY_WAIT: enter when price taps into FVG / supply / demand zone ───────
    if state.state == "ENTRY_WAIT" and not has_position:
        state.bars_in_entry_wait += 1
        if state.zone_set_price is None:             # capture the arm-price for ANY zone type
            state.zone_set_price = price
        if state.amd_phase:                          # supply/demand zone — can take hours to tap
            expiry, tag = AMD_ENTRY_WAIT_BARS, "AMD zone"
        elif state.amd_zone_type == "choch_fvg":     # displacement retest — needs room (~4h)
            expiry, tag = 48, "CHoCH FVG"
        else:                                        # generic FVG/OB
            expiry, tag = FVG_EXPIRY_BARS, "FVG"
        if state.bars_in_entry_wait > expiry:
            print(f"[{base}] {tag} expired after {expiry} bars — resetting.")
            state.reset()
            return price

        # Stale-zone invalidation — applies to EVERY pending zone (AMD supply/demand,
        # trend-follow, CHoCH-FVG retest). They're all "wait for price to reach the zone"
        # setups; if price instead RUNS AWAY ≥1.5% in our direction from where the zone was
        # armed, the move happened without us — abandon it and re-hunt the breakdown/breakout.
        # (Measured vs the arm-price, not a displacement, so it never thrash-abandons.)
        if state.zone_set_price:
            ran_away = ((state.bias == "BEARISH" and price < state.zone_set_price * 0.985) or
                        (state.bias == "BULLISH" and price > state.zone_set_price * 1.015))
            if ran_away:
                moved = abs(price - state.zone_set_price) / state.zone_set_price
                print(f"[{base}] ⚠️ Zone abandoned — price ran {moved:.1%} from setup "
                      f"(${state.zone_set_price:,.2f}→${price:,.2f}) without tapping; re-hunting the move.")
                state.reset()
                return price

        # ── Sniper arming (once, on first bar, before zone tap) ─────────────────
        # Pre-calculate SL/TP/qty NOW while we have df_ltf ATR data, so the
        # 10-second watcher can fire immediately when price enters the zone
        # without waiting up to 5 minutes for the next full strategy cycle.
        if not state.sniper_armed and state.bars_in_entry_wait == 1:
            _is_long_s  = state.bias == "BULLISH"
            _fill_s     = state.fvg_low if _is_long_s else state.fvg_high
            _ltf_rng_s  = df_ltf['high'] - df_ltf['low']
            _atr_s      = float(_ltf_rng_s.rolling(14).mean().iloc[-1])
            _sl_dist_s  = SL_ATR_MULT * _atr_s
            _sl_s       = _fill_s - _sl_dist_s if _is_long_s else _fill_s + _sl_dist_s
            _risk_s     = abs(_fill_s - _sl_s)
            _tp_s       = _fill_s + MIN_AI_RR * _risk_s if _is_long_s else _fill_s - MIN_AI_RR * _risk_s
            _avail_s    = paper.check_daily_cap(BINANCE_CASH_AT_RISK * paper.balance)
            _margin_s   = min(_avail_s, BINANCE_CASH_AT_RISK * paper.balance)
            _qty_s      = _margin_s * PAPER_LEVERAGE / _fill_s if _fill_s and _margin_s > 0 else 0
            if _qty_s > 1e-6:
                state.sniper_armed  = True
                state.sniper_sl     = _sl_s
                state.sniper_tp     = _tp_s
                state.sniper_qty    = _qty_s
                state.sniper_margin = _margin_s
                print(f"[{base}] 🔫 Sniper armed — {'LONG' if _is_long_s else 'SHORT'} "
                      f"zone ${state.fvg_low:,.4f}–${state.fvg_high:,.4f}  "
                      f"SL ${_sl_s:,.4f}  TP ${_tp_s:,.4f}  "
                      f"(10-sec precision entry ready)", flush=True)

        # Small tolerance so a near-miss tap still counts — price stalling $0.02 short of the
        # zone edge and reversing shouldn't cost the whole setup.
        tol = price * 0.0015   # 0.15%
        in_fvg = (state.fvg_low is not None and
                  state.fvg_low - tol <= price <= state.fvg_high + tol)

        if in_fvg:
            is_long = state.bias == "BULLISH"
            bias_str = "bullish" if is_long else "bearish"

            # ATR gate: if market has gone dead since the zone was set, skip entry
            # (catches stale ENTRY_WAIT states restored from file or set in quiet markets).
            # max(14-bar, 3-bar) so a fresh impulse at the tap isn't mislabeled "dead".
            _rng = df_htf['high'] - df_htf['low']
            entry_atr = max(_rng.rolling(14).mean().iloc[-1], _rng.rolling(3).mean().iloc[-1])
            entry_atr_pct = entry_atr / price
            if entry_atr_pct < atr_gate_for(symbol):
                print(f"[{base}] ⏸ Entry skipped — market dead at tap "
                      f"(4H ATR={entry_atr_pct:.1%} < {atr_gate_for(symbol):.1%})")
                return price

            # ── Reversal veto: never fade a fresh opposite-side reversal ──────────────
            # Debbie-La core rule: a swept LOW that gets RECLAIMED is a BULLISH reversal —
            # you go long, you never short it; a swept HIGH that gets rejected is bearish.
            # A stale bias can leave the bot trying to SHORT a V-recovery off a swept low
            # (exactly the AVAX bounce: -6% flush to $6.00, then reclaimed straight back up)
            # or LONG a flush off a swept high. Scan the last ~4.5h of 5m candles: if price
            # has V-recovered hard off the window's far extreme and is now sitting near the
            # opposite end, the manipulation already resolved AGAINST our side — stand aside.
            # (In a bearish daily the bot simply stays flat rather than longing the reversal.)
            # AMD manipulation setups are EXEMPT from the reversal veto.
            # manipulation_up  = sweep lows → bleed up to supply → SHORT. The bleed up
            # IS the manipulation leg and will naturally trip the veto (price reclaimed
            # off the swept low) — but the short entry happens at the supply zone, not
            # blindly into the bounce, so the veto would wrongly block it.
            # manipulation_down = mirror exemption for the long side.
            _amd_exempt = state.amd_phase in ('manipulation_up', 'manipulation_down', 'wedge_breakout')
            rev = df_ltf.tail(REVERSAL_WINDOW)
            w_lo, w_hi = float(rev['low'].min()), float(rev['high'].max())
            rng = w_hi - w_lo
            if rng > 0 and not is_long and not _amd_exempt:
                lo_age  = len(rev) - 1 - int(rev['low'].values.argmin())
                reclaim = (price - w_lo) / w_lo
                if reclaim >= REVERSAL_RECLAIM_PCT and (price - w_lo) / rng >= 0.55 and lo_age >= 6:
                    print(f"[{base}] 🚫 SHORT vetoed — price V-recovered {reclaim:+.1%} off swept low "
                          f"${w_lo:,.4f} ({lo_age} bars ago) and sits near the highs = bullish reversal. "
                          f"Bearish zone invalidated — resetting to IDLE.")
                    state.reset()
                    return price
            elif rng > 0 and is_long and not _amd_exempt:
                hi_age = len(rev) - 1 - int(rev['high'].values.argmax())
                drop   = (w_hi - price) / w_hi
                if drop >= REVERSAL_RECLAIM_PCT and (w_hi - price) / rng >= 0.55 and hi_age >= 6:
                    print(f"[{base}] 🚫 LONG vetoed — price V-dropped {drop:+.1%} off swept high "
                          f"${w_hi:,.4f} ({hi_age} bars ago) and sits near the lows = bearish reversal. "
                          f"Bullish zone invalidated — resetting to IDLE.")
                    state.reset()
                    return price

            # Candle pattern at tap — informational context for the AI
            tap_candle = indicators.classify_candle(df_ltf.iloc[-1], df_ltf.iloc[-2])
            confirms   = indicators.candle_confirms_bias(tap_candle, state.bias)
            candle_note = f"✅ {tap_candle}" if confirms else f"⚠️ {tap_candle} (no candle confirm)"

            # 15m CHoCH / MSS — the LTF structure shift at the zone.
            choch, choch_dir = indicators.detect_choch(df_ltf_15, lookback=10)
            choch_aligned = choch and choch_dir == bias_str
            choch_note = (f"✅ 15m CHoCH {choch_dir}" if choch_aligned
                          else f"⚠️ no 15m CHoCH" if not choch
                          else f"⚠️ 15m CHoCH {choch_dir} (counter)")

            # Only print when something actually changed — suppress tick-by-tick spam
            _tap_changed = (tap_candle != state.last_tap_candle or
                            choch_aligned != state.last_choch_aligned)
            if _tap_changed:
                print(f"[{base}] 🕯 Zone tap — candle: {candle_note}  |  {choch_note}")
                state.last_tap_candle    = tap_candle
                state.last_choch_aligned = choch_aligned

            # Confirmation depends on the zone type:
            #  • CHoCH FVG (displacement retest): the displacement that LEFT the gap already
            #    broke structure — it IS the change of character. The retest only needs a
            #    REBOUNCE: a rejection candle in our direction OR a fresh 15m CHoCH. Requiring
            #    a full 15m CHoCH on top double-confirms the same thing and over-filters.
            #  • Supply/demand/sweep/trend zones (no fresh displacement): a REJECTION CANDLE at
            #    the level OR a 15m CHoCH. The rejection candle catches a fast fakeout-reversal
            #    (the shooting-star that tops a liquidity-grab spike) that the slower CHoCH
            #    misses — while a clean rip-through (no rejection candle, no CHoCH) is still
            #    refused, so we don't short a real breakout.
            if state.amd_zone_type == "choch_fvg":
                # FRESH displacement FVG = self-confirming (the displacement IS the CHoCH).
                # If price retests it QUICKLY (momentum still intact) → DIRECT TAP, no extra
                # confirmation: the displacement proved intent, the SL sits above the gap, and
                # the AI is the final backstop. Don't ask the market to prove itself twice and
                # miss the continuation. If the gap has AGED (momentum likely cooled), fall back
                # to a rebounce — a rejection candle or a 15m CHoCH.
                FRESH_BARS = 6   # ≤30 min since the zone armed = still a fresh impulse
                if state.bars_in_entry_wait > FRESH_BARS and not (confirms or choch_aligned):
                    if _tap_changed:
                        print(f"[{base}] ⏳ Aged CHoCH FVG ({state.bars_in_entry_wait} bars) — waiting "
                              f"for rebounce (rejection candle or 15m CHoCH {bias_str})")
                    return price
                if state.bars_in_entry_wait <= FRESH_BARS:
                    print(f"[{base}] ⚡ Fresh displacement FVG retest — DIRECT TAP entry (no CHoCH needed)")
            elif not (confirms or choch_aligned):
                if _tap_changed:
                    print(f"[{base}] ⏳ In zone — waiting for rebounce "
                          f"(rejection candle or 15m CHoCH {bias_str})")
                return price

            # 1. Calculate SL and risk distance
            # Structural SL: just outside the HTF FVG edge (correct for direction).
            # Then CAP: the HTF zone can be 8-10× the 5m ATR wide — that's swing-sized
            # and absurd as a day-trade stop. Pull the SL to within MAX_SL_ATR_MULT × 5m
            # ATR of entry so position sizing and TP stay in the trade's own timeframe.
            _ltf_rng = df_ltf['high'] - df_ltf['low']
            _ltf_atr = float(_ltf_rng.rolling(14).mean().iloc[-1])
            _max_sl_dist = MAX_SL_ATR_MULT * _ltf_atr
            _sl_dist = SL_ATR_MULT * _ltf_atr   # adaptive breathing room (1.5× ATR)
            if is_long:
                state.stop_loss = state.fvg_low - _sl_dist
                risk_amt        = price - state.stop_loss
                label = "LONG"
                if risk_amt <= 0:
                    state.stop_loss = price - _sl_dist
                    risk_amt        = _sl_dist
                    print(f"[{base}] 📏 SL corrected: entry below zone low → ATR SL ${state.stop_loss:,.4f}")
                elif risk_amt > _max_sl_dist:
                    state.stop_loss = price - _max_sl_dist
                    risk_amt        = _max_sl_dist
                    print(f"[{base}] 📏 SL capped: {MAX_SL_ATR_MULT}×ATR max → SL ${state.stop_loss:,.4f}")
            else:
                state.stop_loss = state.fvg_high + _sl_dist
                risk_amt        = state.stop_loss - price
                label = "SHORT"
                if risk_amt <= 0:
                    state.stop_loss = price + _sl_dist
                    risk_amt        = _sl_dist
                    print(f"[{base}] 📏 SL corrected: entry above zone high → ATR SL ${state.stop_loss:,.4f}")
                elif risk_amt > _max_sl_dist:
                    state.stop_loss = price + _max_sl_dist
                    risk_amt        = _max_sl_dist
                    print(f"[{base}] 📏 SL capped: {MAX_SL_ATR_MULT}×ATR max → SL ${state.stop_loss:,.4f}")

            # 2. Find the nearest 4H liquidity pool — informs the AI's R:R choice,
            #    but the AI (floored at 1:4) has final say on the actual target.
            pool_tp  = indicators.find_next_liquidity_target(df_htf, price, bias_str)

            # 3. Ask NVIDIA AI — pass the TRADE direction (state.bias), not the current BOS
            #    direction. For trend-follow setups with no sweep, pass the zone edge as the
            #    structural reference level instead of $0.
            ref_level = state.sweep_low or (state.fvg_high if not is_long else state.fvg_low)
            confirm, rr_actual, ai_reason = get_ai_confirmation(
                symbol, price, daily_trend, bias_str,
                state.fvg_low, state.fvg_high, ref_level,
                state.stop_loss, risk_amt, pool_tp, df_ltf, df_htf,
                amd_phase=state.amd_phase, zone_type=state.amd_zone_type,
            )
            # Target OFFSET — pull the TP a fraction of ATR inward so we fill BEFORE the
            # herd's orders pile up at the round number / structural ceiling. Giving up a
            # sliver of profit to guarantee the fill (front-run the front-runners).
            # Cap at 25% of the reward so a tight-stop setup can never push TP across entry
            # (which would make tp_dist≈0 → instant "TP" at a loss).
            reward    = risk_amt * rr_actual
            tp_offset = min(0.05 * entry_atr, 0.25 * reward)
            state.take_profit = (price + reward - tp_offset if is_long
                                 else price - reward + tp_offset)
            icon = "✅ YES" if confirm else "❌ NO"
            print(f"[{base}] 🤖 AI Bot Approval: {icon}  R:R=1:{rr_actual:.1f}  {ai_reason[:140]}")

            # 4. Execute only if AI confirms
            if confirm:
                effective_fraction = risk_fraction * 0.5 if state.ranging_mode else risk_fraction
                # Margin-based sizing: deploy risk_fraction% of balance as MARGIN.
                # Leverage stretches that margin into a larger controlled position.
                #   margin  = balance × risk_fraction          ← what you "put in"
                #   qty     = margin × PAPER_LEVERAGE / price  ← what you control
                margin_to_deploy = paper.balance * effective_fraction
                margin_to_deploy = min(margin_to_deploy, paper.balance * 0.20)  # cap 20% per trade
                # Daily margin cap: total margin across all trades ≤ 5% of balance per day
                daily_remaining = paper.check_daily_cap(margin_to_deploy)
                if daily_remaining <= 0:
                    print(f"[{base}] ⏭ Daily margin cap reached (5% of balance) — skipping entry.")
                    return price
                margin_to_deploy = min(margin_to_deploy, daily_remaining)
                qty = math.floor(margin_to_deploy * PAPER_LEVERAGE / price * 1e6) / 1e6

                # Liquidation guard: SL must sit INSIDE the liq distance (1/L from entry).
                # If the structural SL is wider than liq distance, tighten it to 90% of liq
                # so the stop always fires before the exchange forces liquidation.
                liq_dist = price / PAPER_LEVERAGE   # distance from entry to liquidation
                if not is_long and (state.stop_loss - price) >= liq_dist:
                    state.stop_loss = price + liq_dist * 0.90
                    risk_amt = state.stop_loss - price
                    print(f"[{base}] ⚡ SL auto-capped at 90% liq distance "
                          f"({PAPER_LEVERAGE}x liq at +{liq_dist:.4f}) → SL ${state.stop_loss:.4f}")
                elif is_long and (price - state.stop_loss) >= liq_dist:
                    state.stop_loss = price - liq_dist * 0.90
                    risk_amt = price - state.stop_loss
                    print(f"[{base}] ⚡ SL auto-capped at 90% liq distance "
                          f"({PAPER_LEVERAGE}x liq at -{liq_dist:.4f}) → SL ${state.stop_loss:.4f}")

                # Minimum-reward gate
                MIN_REWARD_DOLLARS = 25.0
                actual_reward = qty * (risk_amt * rr_actual) if qty > 0 else 0
                if actual_reward < MIN_REWARD_DOLLARS:
                    print(f"[{base}] ⏭ Trade skipped — reward too small after position cap "
                          f"(${actual_reward:.2f} < ${MIN_REWARD_DOLLARS:.0f} min). "
                          f"Cheap coin + tight SL = deploy capital elsewhere.")
                    return price

                if qty > 0:
                    # Final guard: never stack onto an existing position. The paper trader
                    # is synchronous so this is belt-and-suspenders, but it keeps both bots
                    # consistent and covers any state/position desync.
                    if abs(paper.get_position(symbol)) > 1e-6:
                        print(f"[{base}] ⛔ Entry aborted — position already open. Syncing to POSITION_OPEN.")
                        state.state = "POSITION_OPEN"
                        return price

                    trade = paper.buy(symbol, qty, price) if is_long else paper.sell(symbol, qty, price)

                    if trade:
                        paper.record_margin(margin_to_deploy)   # count against daily 5% cap
                        state.state       = "POSITION_OPEN"
                        state.entry_price = price
                        state.entry_time  = now
                        pos_value = qty * price          # full controlled position
                        margin    = pos_value / PAPER_LEVERAGE   # actual capital posted

                        if PAPER_LEVERAGE > 1:
                            lev_line = (
                                f"\n[{base}]    💹 {PAPER_LEVERAGE}x LEVERAGE  "
                                f"margin=${margin:,.2f} controls ${pos_value:,.2f} "
                                f"({qty:,.4f} {base})  "
                                f"liq if price moves {1/PAPER_LEVERAGE:.0%} against "
                                f"(${price*(1-1/PAPER_LEVERAGE) if is_long else price*(1+1/PAPER_LEVERAGE):,.2f})"
                            )
                        else:
                            lev_line = ""

                        trade_print(base, f"✅ TRADE OPENED {label}",
                                    price, balance=paper.balance,
                                    extra=(f"margin=${margin:,.2f} → ${pos_value:,.2f} controlled  "
                                           f"SL=${state.stop_loss:,.4f}  TP=${state.take_profit:,.4f}  "
                                           f"R:R 1:{rr_actual:.1f}  "
                                           f"risk=${risk_amt*qty:,.2f}  reward=${reward*qty:,.2f}"
                                           + (f"  [{PAPER_LEVERAGE}x]" if PAPER_LEVERAGE > 1 else "")))
                        alert(f"🔔 TRADE OPENED — {label} {base}",
                              f"${price:,.4f}  margin ${margin:,.0f} → ${pos_value:,.0f}  "
                              f"SL ${state.stop_loss:,.2f}  TP ${state.take_profit:,.2f}  (1:{rr_actual:.1f})",
                              sound="Submarine",
                              speak=f"Trade opened. {label} {base}")
            else:
                state.ai_reject_count += 1
                if state.ai_reject_count >= 3:
                    print(f"[{base}] AI rejected setup {state.ai_reject_count}× — zone abandoned, resetting to IDLE.")
                    state.reset()
                else:
                    print(f"[{base}] AI rejected setup ({state.ai_reject_count}/3) — staying in ENTRY_WAIT")

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

    if not _libs_ready.is_set():
        print("⏳  Loading trading libraries (macOS security scan of .so files)…")
        print("    First run after reboot takes ~15-30 min. Bot will begin once ready.\n", flush=True)
        while not _libs_ready.wait(timeout=60):
            elapsed = (time.time() - _libs_start) / 60
            print(f"    ⏳  Still loading… ({elapsed:.0f} min elapsed)", flush=True)

    exchange     = connect_exchange()
    htf_exchange = connect_htf_exchange()
    paper  = PaperTrader(PAPER_BALANCE)
    states = {s: SymbolState() for s in symbols}
    load_crypto_state(paper, states, symbols)  # resume from last save if available

    # ── Startup catch-up: check if any open position already hit SL/TP while bot was offline ──
    print("Checking open positions against current prices…", flush=True)
    for sym in symbols:
        st = states[sym]
        if st.state != "POSITION_OPEN" or not st.stop_loss or not st.take_profit:
            continue
        try:
            _t   = exchange.fetch_ticker(sym)
            _cur = float(_t["last"])
            held = paper.get_position(sym)
            if abs(held) < 1e-9:
                continue
            is_long = held > 0
            base    = sym.split("/")[0]
            sl_hit = (is_long and _cur <= st.stop_loss) or (not is_long and _cur >= st.stop_loss)
            tp_hit = (is_long and _cur >= st.take_profit) or (not is_long and _cur <= st.take_profit)
            if sl_hit or tp_hit:
                fill  = st.stop_loss if sl_hit else st.take_profit
                label = "🔴 SL" if sl_hit else "🟢 TP"
                close_qty = abs(held)
                if is_long:
                    paper.sell(sym, close_qty, fill)
                    pnl = (fill - st.entry_price) * close_qty
                else:
                    paper.buy(sym, close_qty, fill)
                    pnl = (st.entry_price - fill) * close_qty
                trade_print(base, f"{label} HIT (startup catch-up — bot was offline)", fill,
                            pnl=pnl, balance=paper.balance)
                st.reset()
                print(f"  ⚠️  {base} {label} was missed while bot was offline — closed now at ${fill:,.4f}", flush=True)
            else:
                print(f"  ✅  {base} position intact  price=${_cur:,.4f}  SL=${st.stop_loss:,.4f}  TP=${st.take_profit:,.4f}", flush=True)
        except Exception as e:
            print(f"  Catch-up check failed for {sym}: {e}", flush=True)

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
        print(f"  Sleeping {SLEEP_SECONDS // 60}m (SL/TP watching every 10s) …\n")

        # ── Fast SL/TP watcher ─────────────────────────────────────────────────
        # Checks every 10 seconds so SL/TP fire within 10s of being hit,
        # not after the full 5-minute strategy sleep.
        deadline = time.time() + SLEEP_SECONDS
        while time.time() < deadline:
            time.sleep(10)
            for sym in symbols:
                st = states[sym]

                # ── 10-second entry sniper ────────────────────────────────────
                if (st.state == "ENTRY_WAIT" and st.sniper_armed
                        and st.fvg_low and st.fvg_high
                        and not paper.get_position(sym)):
                    try:
                        _t      = exchange.fetch_ticker(sym)
                        _cur    = float(_t["last"])
                        _tol    = _cur * 0.0015
                        _in     = st.fvg_low - _tol <= _cur <= st.fvg_high + _tol
                        if _in:
                            _is_long = st.bias == "BULLISH"
                            _fill    = st.fvg_low if _is_long else st.fvg_high
                            _base    = sym.split("/")[0]
                            if _is_long:
                                paper.buy(sym, st.sniper_qty, _fill)
                            else:
                                paper.sell(sym, st.sniper_qty, _fill)
                            paper.margin_used[sym] = st.sniper_margin
                            paper.record_margin(st.sniper_margin)
                            st.state       = "POSITION_OPEN"
                            st.entry_price = _fill
                            st.stop_loss   = st.sniper_sl
                            st.take_profit = st.sniper_tp
                            st.entry_time  = datetime.now(timezone.utc)
                            _lbl = "LONG" if _is_long else "SHORT"
                            _pv  = st.sniper_qty * _fill
                            _r   = abs(_fill - st.sniper_sl) * st.sniper_qty
                            _rw  = abs(st.sniper_tp - _fill) * st.sniper_qty
                            trade_print(_base, f"⚡ SNIPER ENTRY — {_lbl} (10-sec precision)",
                                        _fill,
                                        extra=f"SL ${st.sniper_sl:,.4f}  TP ${st.sniper_tp:,.4f}  "
                                              f"margin ${st.sniper_margin:,.2f} → ${_pv:,.2f}  "
                                              f"risk ${_r:,.2f}  reward ${_rw:,.2f}"
                                              + (f"  [{PAPER_LEVERAGE}x]" if PAPER_LEVERAGE > 1 else ""))
                            alert(f"⚡ SNIPER — {_lbl} {_base}",
                                  f"@ ${_fill:,.4f}  SL ${st.sniper_sl:,.4f}  TP ${st.sniper_tp:,.4f}",
                                  sound="Submarine",
                                  speak=f"Sniper entry. {_lbl} {_base}")
                            prices[sym] = _cur
                            save_crypto_state(paper, states, symbols, prices)
                    except Exception:
                        pass

                # Orphan guard: paper position exists but state machine lost POSITION_OPEN
                # (can happen if state was reset while a sniper-entered position stayed open)
                _held_check = paper.get_position(sym)
                if abs(_held_check) > 1e-6 and st.state != "POSITION_OPEN":
                    if st.stop_loss and st.take_profit and st.entry_price:
                        print(f"[{sym.split('/')[0]}] ⚠️  Orphaned position detected — restoring POSITION_OPEN", flush=True)
                        st.state = "POSITION_OPEN"

                if st.state != "POSITION_OPEN" or not st.stop_loss:
                    continue
                try:
                    ticker  = exchange.fetch_ticker(sym)
                    cur     = float(ticker["last"])
                    held    = paper.get_position(sym)
                    if abs(held) < 1e-9:
                        continue
                    base = sym.split("/")[0]

                    # Scale-out + break-even on the LIVE tick — fast enough to catch a
                    # near-miss reversal before it round-trips to the original stop.
                    manage_open_trade(paper, st, sym, cur, base)
                    held = paper.get_position(sym)      # re-read after a possible scale-out
                    if abs(held) < 1e-9:
                        continue

                    is_long   = held > 0
                    close_qty = abs(held)

                    # Use 1m candle data to catch crosses between 10-second polls.
                    # SL: use candle CLOSE — SMC liquidity grabs wick through SL then close
                    #     back above/below, so a wick alone shouldn't stop you out.
                    # TP: use candle HIGH/LOW wick — you want the fill as soon as target touched.
                    try:
                        m1 = exchange.fetch_ohlcv(sym, "1m", limit=2)
                        candle_close = float(m1[-1][4]) if m1 else cur
                        candle_high  = float(m1[-1][2]) if m1 else cur
                        candle_low   = float(m1[-1][3]) if m1 else cur
                    except Exception:
                        candle_close = cur
                        candle_high  = cur
                        candle_low   = cur

                    sl_hit = (is_long  and (cur <= st.stop_loss  or candle_low  <= st.stop_loss)) or \
                             (not is_long and (cur >= st.stop_loss  or candle_high >= st.stop_loss))
                    tp_hit = (is_long  and (cur >= st.take_profit or candle_high  >= st.take_profit)) or \
                             (not is_long and (cur <= st.take_profit or candle_low   <= st.take_profit))
                    if not sl_hit and not tp_hit:
                        continue

                    # Fill at the level that was crossed (not the current last price)
                    if sl_hit:
                        fill = st.stop_loss
                    else:
                        fill = st.take_profit
                    label = "🔴 SL" if sl_hit else "🟢 TP"
                    if is_long:
                        paper.sell(sym, close_qty, fill)
                        pnl = (fill - st.entry_price) * close_qty
                    else:
                        paper.buy(sym, close_qty, fill)
                        pnl = (st.entry_price - fill) * close_qty
                    lev_tag = f"[{PAPER_LEVERAGE}x]" if PAPER_LEVERAGE > 1 else ""
                    trade_print(base, f"{label} HIT (watcher)",
                                fill, pnl=pnl, balance=paper.balance,
                                extra=lev_tag)
                    won = pnl >= 0
                    alert(f"{label} hit — {base} closed",
                          f"@ ${fill:,.4f}  P&L ${pnl:+.2f}  Balance ${paper.balance:,.2f}",
                          sound="Glass" if won else "Basso",
                          speak=f"{base} {'take profit' if won else 'stop loss'} hit. "
                                f"{'Profit' if won else 'Loss'} {abs(pnl):.0f} dollars")
                    st.reset()
                    prices[sym] = cur
                    save_crypto_state(paper, states, symbols, prices)
                except Exception:
                    pass


if __name__ == "__main__":
    run()

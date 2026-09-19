"""
1H/1M SMC Sweep-Reversal Test Bot — stocks + crypto.
Stocks : IWM via Alpaca paper (NYSE hours, long-only)
Crypto : 8 pairs via Coinbase public (24/7, long + short) — BTC, ETH, SOL,
         DOGE, XRP, AVAX, POL, ADA, matching binance_bot.py's watchlist.
Both pipelines: 1H swing high/low = liquidity pools (recalculated hourly),
1M candle wicks past a pool and closes back inside = sweep+reversal entry,
opposite pool = target, min 1:3 R:R, 1% risk per trade.
Sound + desktop notification on entry/SL/TP; hourly session-stats summary.
Purpose: confirm execution works on both pipelines before trusting the SMC bot.
"""

import os
import re
import sys
import math
import time
import logging
import threading

# ── Make this run explainable no matter how it was launched ───────────────────
# scripts/start_test_bot.sh redirects stdout into a log; a bare `python3 test_bot.py`
# in a Terminal does not, and then the session exists only in scrollback. On
# 2026-09-18 this bot took a counter-trend ADA short that cost a full stop, and the
# entry reasoning could not be reconstructed at all — the process had written to a tty
# and the window was gone, so the diagnosis had to come from replaying market data
# instead. binance_bot.py and tradingbot.py already do this; this one did not.
# Runs before anything prints so the startup banner is captured. should_tee() makes it
# a no-op when stdout is already a file, so a script launch never double-writes.
if __name__ == "__main__":
    from bot.tee_logging import tee_stdout_to
    _TEED = tee_stdout_to(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "logs", "test_bot.log"))
    if _TEED:
        print(f"📝 Also logging to {_TEED} (started from a terminal — teeing so this "
              f"session is still explainable afterwards)")
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv

from bot import indicators
from bot import ai_model as _ai_model
from config import GOOGLE_SHEET_URL
from sheets_logger import get_sheet_client, log_trade, ensure_tabs, LEDGER_HEADER
from chart_renderer import (render_trade_chart, render_stock_tradingview,
                             save_chart_locally)
from github_chart_uploader import upload_chart_to_github, ensure_chart_branch

load_dotenv()

API_KEY    = os.getenv("ALPACA_API_KEY", "")
API_SECRET = os.getenv("ALPACA_API_SECRET", "")
PAPER      = os.getenv("ALPACA_PAPER", "True").lower() in ("1", "true", "yes")

POOL_LOOKBACK_1H     = 24        # 1H candles used to mark the swing high/low anchor
POOL_RECALC_SECONDS  = 60 * 60   # recompute the 1H pools once per hour
SL_BUFFER_PCT        = 0.0005    # legacy fixed buffer — kept as a floor under the ATR stop
STOP_ATR_TF          = "15m"     # timeframe the stop-distance ATR is measured on. 1m ATR is
                                 # ~0.05% (tighter than the noise band); 15m ATR ~0.3-0.5% puts
                                 # the stop OUTSIDE 1m noise while entries stay on the 1m sweep.
STOP_ATR_LEN         = 14        # ATR lookback
STOP_ATR_MULT        = 1.0       # stop sits this many 15m-ATRs beyond the sweep wick
# 15m ATR alone still sits inside NORMAL post-sweep continuation — real ledger data showed
# 14/15 trades hitting stop, several within 90 seconds of entry. Same fix already proven in
# binance_bot.py + bot/strategy.py: FLOOR the stop against a HIGHER-timeframe (1h) ATR so a
# normal wiggle can't tag it — it only stops on a genuine structural break. Uses the 1h
# candles already fetched for the trend filter below, so no extra API call.
MIN_STOP_ATR_MULT_HTF = 1.5      # stop floored at least this many 1h-ATRs from entry
MIN_RR               = 2.5       # skip the trade if implied R:R is below this
# The momentum half of the sweep confirmation needs an absolute size floor as well as a
# body/range ratio, or a clean-looking body on a dead 1m chart passes as a "move". Sized
# as a fraction of the 1h ATR, because a 1m body is a small multiple of it. Measured on
# ADA: 0.10x clears roughly the top quartile of 1m bodies. 0 disables the floor.
SWEEP_MOMENTUM_ATR_FRAC = float(os.getenv("SWEEP_MOMENTUM_ATR_FRAC", "0.10"))
MAX_AI_RR            = 15.0      # sanity ceiling on a model-suggested R:R
# Higher-timeframe momentum filter (Step 4). Coinbase has NO native 4h granularity
# (only 1m/5m/15m/30m/1h/2h/6h/1d), so we fetch 1h and resample to true 4h candles.
TREND_FETCH_TF       = "1h"      # granularity actually requested from the exchange
TREND_TF_SECONDS     = 4 * 3600  # resample target: 4-hour buckets
TREND_EMA_LEN        = 30        # price vs this 4H EMA defines UP/DOWN (30×4h = 5 days)
RISK_PCT             = 0.01      # 1% of balance/cash risked per trade
TEST_LEVERAGE        = 1         # UNLEVERAGED by user decision (2026-07-19) — margin == notional
MAX_MARGIN_PCT       = 1.00      # backstop only, not a routine ceiling: the pure 1%-risk
                                 # formula (RISK_PCT) governs sizing; this just prevents an
                                 # unrealistically tight stop from demanding MORE than the
                                 # whole account (impossible at TEST_LEVERAGE=1 anyway). Was
                                 # 0.02 — that bound basically every real trade (only a stop
                                 # ≥50% of price ever fell under it), silently shrinking every
                                 # position to a few dollars of real risk regardless of setup
                                 # quality, e.g. a real 5% stop that should risk $50 on $5k was
                                 # actually risking ~$5. See DAILY_RISK_PCT below for the
                                 # separate, still-active daily aggregate cap across all trades.
# A daily cap is worth having — it stops one bad session compounding — but it has to be
# denominated in the SAME unit the sizing measures. This was DAILY_ACCOUNT_PCT = 0.10:
# 10% of the account of NOTIONAL per day, summed across all trades, never released on
# close. On the $5,000 paper balance that $500/day ceiling silently undid risk-based
# sizing — a 1%-risk trade actually risked $4.00 behind a 0.8% stop and $25 behind a 5%
# stop, so the TIGHTER and better the stop, the LESS money was at risk. That is the exact
# inversion risk-based sizing exists to prevent, and it is what made 1% behave like
# "what you put in" rather than "what you can lose".
# 3% = three full-size 1% trades per day, whatever their stops happen to be.
DAILY_RISK_PCT       = 0.03      # max % of the account that may be LOST per day, all trades
# Sizing (user-specified 2026-07-20): no leverage; each trade deploys at most 2% of
# the account (~$100 on $5k), and at most 10% (~$500) may be put to work across the
# whole UTC day combined — so ~5 trades/day before the daily budget is spent. The
# daily counter is persisted to test_state.json so a restart can't reset it. Min R:R 1:2.5.
STOCK_SYMBOL   = "IWM"  # kept off DebbieLaSMC's watchlist on purpose — avoids both bots trading the same ticker
CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD",
                   "XRP/USD", "AVAX/USD", "POL/USD", "ADA/USD"]
CRYPTO_BALANCE = 5_000.0
STOCK_TEST_BALANCE = 5_000.0    # reference balance for STOCK position sizing only. self.get_cash()
                                 # returns the REAL shared Alpaca account (~$95k — the same account
                                 # tradingbot.py trades), not an isolated test sleeve. REAL INCIDENT
                                 # 2026-08-06: IWM sized to $98,305 notional (a stop only 0.063% away
                                 # blew up the 1%-risk formula against the real account balance).
                                 # Sizing is computed against this fixed reference instead — real
                                 # orders still fill against the shared account, only the qty math
                                 # changes. The crypto side never had this problem: PaperTrader
                                 # already tracks its own isolated CRYPTO_BALANCE, never touching a
                                 # real exchange account.
STALE_TRADE_HOURS = 4    # a sweep setup that hasn't hit SL/TP in this long has lost its edge and
                          # is just tying up capital doing nothing — these are 1H-timeframe setups,
                          # so 4h (~4 candles) is the natural ceiling. REAL INCIDENT 2026-08-09: an
                          # ADA LONG sat open 48.9 hours with no time-based exit anywhere in this
                          # file (binance_bot.py already has this exact concept, this bot never did).
POLL_SECONDS   = 60      # 1-minute poll — matches the 1M sniper timeframe
FAST_WATCH_SECONDS = 10  # between polls, re-check open positions' SL/TP this often
TEST_STATE_FILE = "test_state.json"
TEST_LEDGER_TAB = "Test Ledger"   # own tab — test trades never pollute the real ledgers
TRADE_ALERTS   = True    # macOS sound + desktop notification + spoken alert on entry/exit


# ── Google Sheets trade ledger (same system as the main bots) ──────────────────

def ensure_test_sheet():
    """Create the Test Ledger tab (with header) and the GitHub chart branch if
    missing. Idempotent and fully fail-soft — call it at every pipeline start."""
    try:
        ensure_tabs(get_sheet_client(), GOOGLE_SHEET_URL, {TEST_LEDGER_TAB: LEDGER_HEADER})
        ensure_chart_branch()
    except Exception as e:
        print(f"[SHEETS] Test Ledger setup skipped: {e}")


def log_test_trade(ticker, is_long, entry_price, exit_price, qty, pnl, sl, tp,
                    entry_time, reason, chart_path=None, leverage=1):
    """Append one closed test trade to the Test Ledger tab, uploading its chart to
    GitHub (local charts/ fallback). Margin column = notional/leverage (the cash the
    trade actually tied up). Never raises — a Sheets or chart error can't break
    the test loop."""
    try:
        now = datetime.now(timezone.utc)
        chart_ref = None
        if chart_path:
            filename = f"TEST_{ticker}_{now:%Y%m%dT%H%M%S}.png"
            chart_ref = upload_chart_to_github(chart_path, filename)
            if chart_ref:
                os.remove(chart_path)
            else:
                chart_ref = save_chart_locally(chart_path, filename)
        notional = qty * entry_price
        margin   = notional / max(1, leverage)
        log_trade(get_sheet_client(), GOOGLE_SHEET_URL, TEST_LEDGER_TAB,
                  (entry_time or now).astimezone().isoformat(), now.astimezone().isoformat(), ticker,
                  "LONG" if is_long else "SHORT", entry_price, sl, tp, exit_price,
                  qty, margin, notional, leverage, pnl, reason, chart_ref)
    except Exception as e:
        print(f"[SHEETS] Test ledger log failed for {ticker}: {e}")


def render_crypto_test_chart(exchange, symbol, is_long, entry_price, sl, tp, entry_time):
    """Chart for a closed crypto test trade — same adaptive-window lightweight-charts
    render as binance_bot. Returns a temp PNG path or None; never raises."""
    try:
        now = datetime.now(timezone.utc)
        dur_min = (max(1.0, (now - entry_time).total_seconds() / 60)
                   if entry_time else 60.0)
        if dur_min <= 360:
            tf, tf_min = "5m", 5
        elif dur_min <= 1080:
            tf, tf_min = "15m", 15
        else:
            tf, tf_min = "1h", 60
        bars = min(140, int(dur_min / tf_min) + 60)
        since = int((now.timestamp() - bars * tf_min * 60) * 1000)
        df = ohlcv_to_df(exchange.fetch_ohlcv(symbol, tf, since=since, limit=bars))
        return render_trade_chart(df, symbol.split("/")[0], "LONG" if is_long else "SHORT",
                                   entry_price, sl, tp, TEST_LEVERAGE, tf, entry_time)
    except Exception as e:
        print(f"[CHART] Test chart failed for {symbol}: {e}")
        return None


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


# ── Silence lumibot's Alpaca order-sync spam (stock thread only) ───────────────
# The shared Alpaca paper account carries ~44 dead early-July bracket/OCO orders
# that lumibot's parser can't read; every 1-minute sync re-warns on ALL of them.
# tradingbot.py already dedups these, but that filter lives in ITS process — this
# is the same treatment for test_bot's own lumibot instance: each dead order ID
# warns once per run, then goes silent.
_MALFORMED_ORDER_RE = re.compile(r"Skipping malformed order (\S+)")
_seen_malformed_order_ids: set = set()

class _DedupMalformedOrders(logging.Filter):
    def filter(self, record):
        try:
            m = _MALFORMED_ORDER_RE.search(record.getMessage())
        except Exception:
            return True
        if not m:
            return True
        if m.group(1) in _seen_malformed_order_ids:
            return False
        _seen_malformed_order_ids.add(m.group(1))
        return True

_DEDUP_MALFORMED = _DedupMalformedOrders()

def _quiet_lumibot_noise():
    """Attach the dedup filter to every handler lumibot has set up. Lumibot's console
    handler hangs off the 'lumibot' logger (not the true root), and records that reach
    a handler-less root fall through to logging.lastResort — cover all three."""
    for lg in (logging.getLogger("lumibot"), logging.getLogger()):
        if _DEDUP_MALFORMED not in lg.filters:
            lg.addFilter(_DEDUP_MALFORMED)
        for h in lg.handlers:
            if _DEDUP_MALFORMED not in h.filters:
                h.addFilter(_DEDUP_MALFORMED)
    if _DEDUP_MALFORMED not in logging.lastResort.filters:
        logging.lastResort.addFilter(_DEDUP_MALFORMED)



# ── Paper trader (crypto side) ─────────────────────────────────────────────────

class PaperTrader:
    """Margin-model paper trader (mirrors binance_bot): opening a position costs
    notional/TEST_LEVERAGE of cash as margin; closing returns the margin plus the
    full price-move P&L on the whole notional. Net balance change per round trip
    is exactly the P&L — leverage only changes how much cash a position ties up."""

    def __init__(self, balance: float):
        self.balance       = balance
        self.positions     = {}   # symbol -> qty (negative = short)
        self.entry_prices  = {}
        self.margin_used   = {}   # symbol -> cash locked as margin while open
        self.trade_count   = 0
        self.daily_risked   = 0.0   # total $ of RISK committed today, across ALL symbols (resets daily)
        self.daily_date     = None

    def get_position(self, symbol):
        return self.positions.get(symbol, 0.0)

    def _roll_daily_window(self):
        """Reset the daily risk counter on a day change. Both risk_remaining and
        record_risk call this independently — neither may assume the other ran first,
        or a record-before-check call order silently loses the day's total."""
        today = datetime.now().astimezone().date()   # LOCAL day boundary
        if self.daily_date != today:
            self.daily_risked = 0.0
            self.daily_date   = today

    def risk_remaining(self, balance: float, daily_risk_pct: float) -> float:
        """Dollars of RISK still available today — NOT released when a trade closes.
        This bounds how much can be LOST in a day, and deliberately says nothing about
        position size; the stop distance decides that. See indicators.daily_risk_remaining."""
        self._roll_daily_window()
        return indicators.daily_risk_remaining(self.daily_risked, balance, daily_risk_pct)

    def record_risk(self, risk_dollars: float):
        """Call after an entry is confirmed to count it against today's risk budget."""
        self._roll_daily_window()
        self.daily_risked += max(0.0, float(risk_dollars))

    def _release_margin(self, symbol, closed_qty, held_qty):
        """Return the proportional slice of locked margin for a partial/full close."""
        m = self.margin_used.get(symbol, 0.0)
        if held_qty <= 0 or m <= 0:
            return 0.0
        frac = min(1.0, closed_qty / held_qty)
        release = m * frac
        remaining = m - release
        if remaining < 1e-9:
            self.margin_used.pop(symbol, None)
        else:
            self.margin_used[symbol] = remaining
        return release

    def buy(self, symbol, qty, price):
        held = self.positions.get(symbol, 0.0)
        if held < 0:   # covering a short: release margin + realize P&L on the move
            cover = min(qty, abs(held))
            entry = self.entry_prices.get(symbol, price)
            self.balance += self._release_margin(symbol, cover, abs(held))
            self.balance += (entry - price) * cover
            new_held = held + cover
            if abs(new_held) < 1e-9:
                self.positions.pop(symbol, None); self.entry_prices.pop(symbol, None)
            else:
                self.positions[symbol] = new_held
        else:          # opening/adding a long: lock notional/leverage as margin
            margin = qty * price / TEST_LEVERAGE
            if margin > self.balance:
                qty    = math.floor((self.balance * 0.95 * TEST_LEVERAGE / price) * 1e6) / 1e6
                margin = qty * price / TEST_LEVERAGE
            if qty <= 0:
                return None
            self.balance -= margin
            self.margin_used[symbol]  = self.margin_used.get(symbol, 0.0) + margin
            self.positions[symbol]    = held + qty
            self.entry_prices[symbol] = price
        self.trade_count += 1
        return {"id": self.trade_count, "qty": qty, "price": price}

    def sell(self, symbol, qty, price):
        held = self.positions.get(symbol, 0.0)
        if held > 0:   # closing a long: release margin + realize P&L on the move
            qty = min(qty, held)
            if qty <= 0:
                return None
            entry = self.entry_prices.get(symbol, price)
            self.balance += self._release_margin(symbol, qty, held)
            self.balance += (price - entry) * qty
            new_held = held - qty
            if new_held < 1e-9:
                self.positions.pop(symbol, None); self.entry_prices.pop(symbol, None)
            else:
                self.positions[symbol] = new_held
        else:          # opening a short: lock notional/leverage as margin
            margin = qty * price / TEST_LEVERAGE
            if margin > self.balance:
                qty    = math.floor((self.balance * 0.95 * TEST_LEVERAGE / price) * 1e6) / 1e6
                margin = qty * price / TEST_LEVERAGE
            if qty <= 0:
                return None
            self.balance -= margin
            self.margin_used[symbol]  = self.margin_used.get(symbol, 0.0) + margin
            self.positions[symbol]    = held - qty
            self.entry_prices[symbol] = price
        self.trade_count += 1
        return {"id": self.trade_count, "qty": qty, "price": price}


# ── ccxt patch ─────────────────────────────────────────────────────────────────

def _patch_ccxt():
    try:
        import importlib.util
        spec = importlib.util.find_spec("ccxt")
        if spec is None:
            return
        vpath = os.path.join(os.path.dirname(spec.origin),
                             "static_dependencies", "toolz", "_version.py")
        if not os.path.exists(vpath):
            return
        txt = open(vpath).read()
        patched = txt.replace(
            'pieces["distance"] = int(count_out)',
            'pieces["distance"] = int(count_out) if count_out is not None else 0'
        )
        if patched != txt:
            open(vpath, "w").write(patched)
    except Exception:
        pass


def _restore_daily_budget(paper):
    """Load today's already-deployed total from test_state.json into a fresh
    PaperTrader so the 10%-of-account/day cap survives restarts. Only restores if
    the saved date is still the current UTC day — a stale (yesterday) counter is
    ignored so the new day starts with the full budget. Fail-soft."""
    import json
    try:
        with open(TEST_STATE_FILE) as f:
            saved = json.load(f)
        saved_date = saved.get("daily_date")
        today = datetime.now().astimezone().date().isoformat()
        if saved_date == today:
            # "daily_deployed" was notional; it means nothing under a risk budget and
            # resets daily anyway, so an old file starts the day at zero rather than
            # importing a number in the wrong unit.
            paper.daily_risked = float(saved.get("daily_risked", 0.0))
            paper.daily_date     = datetime.now().astimezone().date()
            print(f"[STATE] Resumed today's risk budget used: ${paper.daily_risked:,.2f} "
                  f"already used of the daily cap")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[STATE] daily-budget restore skipped: {e}")


def save_test_state(paper, sl_levels, tp_levels, prices, pools, trade_states, stats=None, entry_times=None):
    import json
    entry_times = entry_times or {}
    try:
        positions = {}
        for sym, qty in paper.positions.items():
            if abs(qty) < 1e-9:
                continue
            entry = paper.entry_prices.get(sym, 0.0)
            cur   = prices.get(sym, 0.0)
            sl    = sl_levels.get(sym)
            tp    = tp_levels.get(sym)
            et    = entry_times.get(sym)
            is_long = qty > 0
            upnl  = (cur - entry) * qty if is_long else (entry - cur) * abs(qty)
            risk  = abs(entry - sl) * abs(qty) if sl else None
            reward = abs(tp - entry) * abs(qty) if tp else None
            positions[sym] = {
                "qty": qty, "side": "LONG" if is_long else "SHORT",
                "entry_price": entry, "current_price": cur,
                "stop_loss": sl, "take_profit": tp,
                "entry_time": et.isoformat() if et else None,
                "unrealized_pnl": upnl,
                "risk_dollars": risk, "reward_dollars": reward,
            }
        pool_data = {
            sym: {
                "pool_high": pools[sym]["high"],
                "pool_low":  pools[sym]["low"],
                "state":     trade_states.get(sym),
            }
            for sym in pools
        }
        # True account equity = free cash + Σ(locked margin + unrealized P&L), via the
        # ONE shared definition. This used to be computed inline here, and the short
        # branch added only the unrealized P&L on the reasoning that shorts move no cash
        # at open. PaperTrader.sell() locks notional/leverage as margin exactly as buy()
        # does, so on 2026-09-18 an ADA short holding the whole $5,000 as margin and down
        # $28.81 reported equity of -$30.77 — the dashboard read "-100.62%", a blown
        # account, on a trade that was fine. See tests/test_account_equity.py.
        equity = indicators.account_equity(
            paper.balance, paper.positions, paper.entry_prices,
            paper.margin_used, prices, TEST_LEVERAGE)
        data = {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "bot": "SweepTestBot",
            "balance": paper.balance,
            "equity": equity,
            "start_balance": CRYPTO_BALANCE,
            "trade_count": paper.trade_count,
            # Persist the daily-deployment budget so the 10%-of-account/day cap
            # survives restarts. Without this, every restart reset the counter to
            # $0 and handed out a fresh full budget (seen live: two same-UTC-day
            # trades totalling $700 against a $500 cap).
            "daily_risked": paper.daily_risked,
            "daily_date": paper.daily_date.isoformat() if paper.daily_date else None,
            "positions": positions,
            "pools": pool_data,
            "live_prices": {s: prices.get(s, 0) for s in CRYPTO_SYMBOLS},
            "stats": stats or {},
        }
        with open(TEST_STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[STATE] save failed: {e}")


def ohlcv_to_df(ohlcv):
    df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.set_index("timestamp")


def compute_pools(df_1h: pd.DataFrame, lookback: int = POOL_LOOKBACK_1H):
    """Returns (pool_high, pool_low) — the swing high/low over the last `lookback` 1H candles."""
    window = df_1h.tail(lookback)
    return float(window["high"].max()), float(window["low"].min())


def detect_sweep(candle_high: float, candle_low: float, candle_close: float,
                  pool_high: float, pool_low: float):
    """Returns 'SHORT', 'LONG', or None — a wick past the pool that closes back inside it."""
    if candle_high > pool_high and candle_close <= pool_high:
        return "SHORT"
    if candle_low < pool_low and candle_close >= pool_low:
        return "LONG"
    return None


def atr(candles, length: int = STOP_ATR_LEN) -> float:
    """Average True Range over `length` bars from a list of [ts,o,h,l,c,v] candles.
    Returns 0.0 if there isn't enough data (caller falls back to the fixed buffer)."""
    if not candles or len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i][2], candles[i][3], candles[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    return sum(trs[-length:]) / min(length, len(trs))


def resample_ohlcv(candles, bucket_seconds: int):
    """Aggregate [ts,o,h,l,c,v] candles into larger buckets (e.g. 1h -> 4h). Buckets
    are aligned to epoch boundaries so the result is stable regardless of where the
    input series starts. Used because Coinbase has no native 4h granularity."""
    buckets = {}
    order = []
    for ts, o, h, l, c, v in candles:
        key = (int(ts) // 1000) // bucket_seconds * bucket_seconds
        if key not in buckets:
            buckets[key] = [key * 1000, o, h, l, c, v]
            order.append(key)
        else:
            b = buckets[key]
            b[2] = max(b[2], h)
            b[3] = min(b[3], l)
            b[4] = c
            b[5] += v
    return [buckets[k] for k in sorted(order)]


def ema(values, length: int) -> float:
    """Exponential moving average of the last values. Returns the final EMA, or the
    simple mean if there's less than `length` data."""
    if not values:
        return 0.0
    k = 2.0 / (length + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def trend_direction(candles, ema_len: int = TREND_EMA_LEN):
    """Higher-timeframe trend from a list of [ts,o,h,l,c,v] candles: 'UP' if the last
    close is above the EMA, 'DOWN' if below, None if there isn't enough data (caller
    treats None as 'no filter' so a thin/new market still trades)."""
    if not candles or len(candles) < ema_len:
        return None
    closes = [c[4] for c in candles]
    e = ema(closes, ema_len)
    price = closes[-1]
    if price > e:
        return "UP"
    if price < e:
        return "DOWN"
    return None


def compute_stop_target(direction: str, candle_high: float, candle_low: float,
                         pool_high: float, pool_low: float,
                         sl_buffer_pct: float = SL_BUFFER_PCT, atr_value: float = 0.0):
    """Returns (stop_loss, take_profit). The stop sits beyond the sweep wick by
    STOP_ATR_MULT × atr_value (a higher-TF ATR passed by the caller) so it clears the
    1-minute noise band — the fixed sl_buffer_pct only acts as a floor when ATR is
    unavailable. Target is the opposite liquidity pool. Wider stops shrink R:R, so the
    caller's MIN_RR filter naturally drops trades whose target no longer justifies the
    (realistic) stop."""
    atr_buffer = STOP_ATR_MULT * atr_value
    if direction == "SHORT":
        sl = candle_high + max(atr_buffer, candle_high * sl_buffer_pct)
        return sl, pool_low
    sl = candle_low - max(atr_buffer, candle_low * sl_buffer_pct)
    return sl, pool_high


def compute_rr(entry: float, sl: float, tp: float) -> float:
    """Risk:reward as a plain float (3.0 means 1:3). Returns 0.0 if risk <= 0."""
    risk   = abs(entry - sl)
    reward = abs(tp - entry)
    return reward / risk if risk > 0 else 0.0


def size_position(balance: float, risk_pct: float, entry: float, sl: float) -> float:
    """Qty sized so a stop-out loses exactly risk_pct of balance. Returns 0.0 if risk <= 0.

    Capped at MAX_MARGIN_PCT of balance in margin (notional = margin × TEST_LEVERAGE):
    with a tight stop, pure risk-based sizing demands more capital than the account
    even has (e.g. a $123 stop on a $61k coin with 1% of a $5k account = $24k of
    notional) — without this cap the paper trader would silently deploy everything
    into one position. A capped trade risks LESS than risk_pct, never more."""
    risk_per_unit = abs(entry - sl)
    if risk_per_unit <= 0:
        return 0.0
    qty = (balance * risk_pct) / risk_per_unit
    qty = min(qty, (balance * MAX_MARGIN_PCT * TEST_LEVERAGE) / entry)
    return math.floor(qty * 1e6) / 1e6


def is_trade_stale(entry_time, now, stale_hours: float) -> bool:
    """True once a position has been open at least stale_hours, regardless of SL/TP.
    entry_time=None (no position, or not yet recorded) is never stale.

    REAL INCIDENT 2026-08-09: test_bot.py had no time-based exit anywhere — an ADA
    LONG sat open 48.9 hours with no resolution before the user noticed. Mirrors
    binance_bot.py's STALE_TRADE_HOURS concept, which this codebase never had."""
    if entry_time is None:
        return False
    return (now - entry_time).total_seconds() / 3600 >= stale_hours


def new_stats():
    """Fresh running-session stats accumulator."""
    return {"trades": 0, "wins": 0, "losses": 0, "gross_win": 0.0, "gross_loss": 0.0}


def record_trade_result(stats: dict, pnl: float):
    """Update a stats accumulator (in place) with one closed trade's P&L."""
    stats["trades"] += 1
    if pnl >= 0:
        stats["wins"]       += 1
        stats["gross_win"]  += pnl
    else:
        stats["losses"]     += 1
        stats["gross_loss"] += abs(pnl)


def stats_summary_line(stats: dict, balance: float, start_balance: float) -> str:
    """Formats a running-session stats block for console printing."""
    trades = stats["trades"]
    win_rate = (stats["wins"] / trades * 100) if trades else 0.0
    avg_win  = (stats["gross_win"]  / stats["wins"])   if stats["wins"]   else 0.0
    avg_loss = (stats["gross_loss"] / stats["losses"]) if stats["losses"] else 0.0
    net_pnl  = balance - start_balance
    bar = "━" * 30
    return (f"{bar} SESSION STATS {bar}\n"
            f"Trades: {trades}   Win rate: {win_rate:.1f}%   "
            f"Avg win: ${avg_win:.2f}   Avg loss: ${avg_loss:.2f}\n"
            f"Balance: ${balance:,.2f}   Net P&L: {net_pnl:+,.2f}\n"
            f"{bar}{'━' * 15}{bar}")


def preview_rr_string(candle, pool_high, pool_low,
                       sl_buffer_pct: float = SL_BUFFER_PCT, min_wick_frac: float = 0.10) -> str:
    """Live 'what-if' R:R preview for both directions off the current candle, shown while
    WATCHING. Only shows a side's number when that side's wick is at least min_wick_frac
    of the candle's range — otherwise a strong trend candle (marubozu, near-zero wick)
    produces a near-zero hypothetical risk and an absurdly inflated, meaningless R:R,
    since it's using a candle that could never actually trigger a real sweep on that side."""
    if pool_high is None or pool_low is None:
        return ""
    c_hi, c_lo = float(candle["high"]), float(candle["low"])
    c_op, c_cl = float(candle["open"]), float(candle["close"])
    rng = c_hi - c_lo
    if rng <= 0:
        return ""
    upper_wick = c_hi - max(c_op, c_cl)
    lower_wick = min(c_op, c_cl) - c_lo

    if upper_wick / rng >= min_wick_frac:
        sl_s, tp_s = compute_stop_target("SHORT", c_hi, c_lo, pool_high, pool_low, sl_buffer_pct)
        short_str = f"SHORT@high 1:{compute_rr(c_cl, sl_s, tp_s):.1f}"
    else:
        short_str = "SHORT@high n/a"

    if lower_wick / rng >= min_wick_frac:
        sl_l, tp_l = compute_stop_target("LONG", c_hi, c_lo, pool_high, pool_low, sl_buffer_pct)
        long_str = f"LONG@low 1:{compute_rr(c_cl, sl_l, tp_l):.1f}"
    else:
        long_str = "LONG@low n/a"

    return f"  preview: {short_str} · {long_str}"


# ── Crypto sweep-reversal loop ────────────────────────────────────────────────

def run_crypto_sweep():
    _patch_ccxt()
    import ccxt

    exchange = ccxt.coinbase({"enableRateLimit": True})
    paper    = PaperTrader(CRYPTO_BALANCE)
    _restore_daily_budget(paper)   # resume today's spent budget so a restart can't reset the 10%/day cap
    ensure_test_sheet()

    pools        = {s: {"high": None, "low": None} for s in CRYPTO_SYMBOLS}
    sl_levels    = {s: None for s in CRYPTO_SYMBOLS}
    tp_levels    = {s: None for s in CRYPTO_SYMBOLS}
    trade_states = {s: "RETIRED" for s in CRYPTO_SYMBOLS}   # forces a pool recalc on the first tick
    last_recalc  = {s: 0.0 for s in CRYPTO_SYMBOLS}
    live_prices  = {s: 0.0 for s in CRYPTO_SYMBOLS}
    entry_times  = {s: None for s in CRYPTO_SYMBOLS}   # so the chart can anchor the entry zone box precisely
    last_watch_ms = {s: None for s in CRYPTO_SYMBOLS}  # epoch ms of the last 1m candle already SL/TP-checked
    stats        = new_stats()   # combined across all symbols
    last_stats_print = 0.0

    def try_close_position(symbol, base, live_price, hi, lo):
        """Close an open position the instant price TOUCHES its SL or TP — checks the
        live price AND the candle wick (hi/lo), so a wick through the level fills even
        if price snapped back. Shared by the 60s poll and the 10s fast watcher below.
        Returns True if it closed the position. A real stop fills the moment price
        reaches it — this makes the paper bot behave the same."""
        held = paper.get_position(symbol)
        if abs(held) < 1e-9:
            return False
        sl = sl_levels[symbol]
        tp = tp_levels[symbol]
        if sl is None or tp is None:
            return False
        is_long = held > 0
        entry   = paper.entry_prices.get(symbol, live_price)
        hit = None
        if   is_long     and (live_price <= sl or lo <= sl): hit = "SL"
        elif is_long     and (live_price >= tp or hi >= tp): hit = "TP"
        elif not is_long and (live_price >= sl or hi >= sl): hit = "SL"
        elif not is_long and (live_price <= tp or lo <= tp): hit = "TP"
        if not hit and is_trade_stale(entry_times[symbol], datetime.now(timezone.utc), STALE_TRADE_HOURS):
            hit = "STALE"
        if not hit:
            return False
        fill = live_price if hit == "STALE" else (sl if hit == "SL" else tp)   # a resting
        # stop/limit fills AT its level; a stale close fills at the current live price.
        if is_long:
            paper.sell(symbol, abs(held), fill); pnl = (fill - entry) * abs(held)
        else:
            paper.buy(symbol, abs(held), fill);  pnl = (entry - fill) * abs(held)
        icon = "\U0001F7E2" if hit == "TP" else ("⏰" if hit == "STALE" else "\U0001F534")
        print(f"[{base}] {icon} {hit} hit @ ${fill:,.4f}  "
              f"P&L: ${pnl:+.2f}  Balance: ${paper.balance:,.2f}")
        record_trade_result(stats, pnl)
        alert(f"{icon} {hit} hit — {base}",
              f"@ ${fill:,.4f}  P&L ${pnl:+.2f}  Balance ${paper.balance:,.2f}",
              sound="Glass" if hit == "TP" else "Basso",
              speak=f"{base} {'take profit' if hit == 'TP' else 'stale close' if hit == 'STALE' else 'stop loss'} hit. "
                    f"{'Profit' if pnl >= 0 else 'Loss'} {abs(pnl):.0f} dollars")
        chart = render_crypto_test_chart(exchange, symbol, is_long,
                                          entry, sl, tp, entry_times[symbol])
        log_test_trade(base, is_long, entry, fill, abs(held), pnl, sl, tp,
                        entry_times[symbol], f"sweep {'LONG' if is_long else 'SHORT'}",
                        chart, leverage=TEST_LEVERAGE)
        sl_levels[symbol]    = None
        tp_levels[symbol]    = None
        entry_times[symbol]  = None
        trade_states[symbol] = "RETIRED"
        last_recalc[symbol]  = time.time()   # fresh recalc countdown from this close
        return True

    print(f"[CRYPTO] 1H/1M Sweep-Reversal on "
          f"{', '.join(s.split('/')[0] for s in CRYPTO_SYMBOLS)} | "
          f"${CRYPTO_BALANCE:,.0f} paper  |  risk={RISK_PCT*100:.0f}%  "
          f"min R:R=1:{MIN_RR:.0f}  |  stop={STOP_ATR_MULT}×{STOP_ATR_TF}ATR  "
          f"|  4h EMA{TREND_EMA_LEN} trend filter  |  SL/TP watch every {FAST_WATCH_SECONDS}s")

    while True:
        ts  = datetime.now(timezone.utc).strftime("%H:%M UTC")
        now = time.time()
        for symbol in CRYPTO_SYMBOLS:
            base = symbol.split("/")[0]
            try:
                if trade_states[symbol] != "IN_TRADE" and now - last_recalc[symbol] >= POOL_RECALC_SECONDS:
                    df_1h = ohlcv_to_df(exchange.fetch_ohlcv(symbol, "1h", limit=POOL_LOOKBACK_1H))
                    pool_high, pool_low = compute_pools(df_1h, POOL_LOOKBACK_1H)
                    pools[symbol]["high"] = pool_high
                    pools[symbol]["low"]  = pool_low
                    last_recalc[symbol]   = now
                    trade_states[symbol]  = "WATCHING"
                    print(f"[{base}] \U0001F553 1H recalc — pool_high=${pool_high:,.2f}  pool_low=${pool_low:,.2f}")

                pool_high = pools[symbol]["high"]
                pool_low  = pools[symbol]["low"]
                # limit=10, not 3: thin pairs (AVAX/POL/ADA) often have minutes with
                # zero trades, and Coinbase simply omits those candles — asking for 10
                # returns the last 10 candles that EXIST, so we nearly always get the
                # ≥2 closed candles needed instead of skipping the whole tick.
                df_1m  = ohlcv_to_df(exchange.fetch_ohlcv(symbol, "1m", limit=10))
                if len(df_1m) < 2:
                    print(f"[{base}] ⏭ Skipping tick — exchange returned only "
                          f"{len(df_1m)} 1m candle(s)")
                    continue
                candle = df_1m.iloc[-2]   # last CLOSED candle — iloc[-1] is still forming
                prev_candle = df_1m.iloc[-3] if len(df_1m) >= 3 else None
                price  = float(candle["close"])
                live_prices[symbol] = price
                held = paper.get_position(symbol)

                candle_type = indicators.classify_candle(candle, prev_candle)
                dist_high = f"(→{(pool_high - price) / price * 100:+.1f}%)" if pool_high else ""
                dist_low  = f"(→{(price - pool_low) / price * 100:+.1f}%)" if pool_low else ""
                preview = (preview_rr_string(candle, pool_high, pool_low)
                           if trade_states[symbol] == "WATCHING" else "")

                print(f"[{base}] {ts}  ${price:,.2f}  pool_high=${pool_high}{dist_high}  "
                      f"pool_low=${pool_low}{dist_low}")
                print(f"[{base}]   candle={candle_type}  state={trade_states[symbol]}{preview}")

                # ── Manage an open trade ────────────────────────────────────────
                if held != 0:
                    # Check the live (forming-candle) price AND the wick of BOTH the
                    # last closed and the still-forming candle — a touch anywhere since
                    # the previous poll must fill, not just a level the closed candle
                    # happened to end past. (The 10s watcher below catches touches
                    # between polls; this is the once-a-minute backstop.)
                    forming = df_1m.iloc[-1]
                    live_now = float(forming["close"])
                    hi = max(float(candle["high"]), float(forming["high"]))
                    lo = min(float(candle["low"]),  float(forming["low"]))
                    try_close_position(symbol, base, live_now, hi, lo)
                    continue

                # ── Look for a new sweep+reversal entry ─────────────────────────
                if trade_states[symbol] != "WATCHING" or pool_high is None:
                    continue

                direction = detect_sweep(float(candle["high"]), float(candle["low"]),
                                          float(candle["close"]), pool_high, pool_low)
                if direction is None:
                    continue

                # ── 4H trend filter (Step 4): trade reversals WITH the higher-TF trend ──
                # A LONG sweep is buy-the-dip (needs an uptrend); a SHORT sweep is
                # sell-the-rip (needs a downtrend). Blocking the counter-trend side
                # stops the bot fading a running move — the "all shorts in an uptrend,
                # all stopped" pattern from the ledger. None = not enough data, allow.
                htf_atr_value = 0.0
                trend = None
                try:
                    # Fetch 1h (native), resample to true 4h, then read the trend.
                    # The margin matters: (30+5)*4 = 140 hourly candles is only 35 4H
                    # bars against a 30-bar EMA minimum, so any short or partial response
                    # dropped the read to None — which used to mean "allow". Ask for
                    # roughly double, so a thin response still clears the minimum.
                    hourly = exchange.fetch_ohlcv(
                        symbol, TREND_FETCH_TF, limit=(TREND_EMA_LEN * 2 + 10) * 4)
                    trend = trend_direction(resample_ohlcv(hourly, TREND_TF_SECONDS))
                    htf_atr_value = atr(hourly, STOP_ATR_LEN)   # reuse this fetch for the stop floor below
                except Exception as _trend_err:
                    # Say so. This used to fail silently into trend = None, and the ADA
                    # short on 2026-09-18 left no trace of why the filter had vanished.
                    print(f"[{base}] ⚠️ 4H trend fetch failed ({type(_trend_err).__name__}) "
                          f"— no trend read this tick")
                # A check that cannot reach a verdict is NOT a verdict in favour: an
                # unreadable trend now refuses both directions. See
                # tests/test_trend_filter_fail_closed.py.
                _trend_ok, _trend_why = indicators.trend_filter_verdict(trend, direction)
                if not _trend_ok:
                    print(f"[{base}] ⏭ {direction} sweep skipped — {_trend_why}")
                    continue

                # The two-bar reversal: INDECISION at the swept level, then MOMENTUM away
                # from it. A sweep alone says only that a level was touched, and price may
                # simply keep going — which is what ADA did on 2026-09-18 while this bot
                # was short into it. Closed candles only; iloc[-1] is still forming.
                # See tests/test_sweep_confirmation.py.
                _bars = [{"open": float(r["open"]), "high": float(r["high"]),
                          "low": float(r["low"]), "close": float(r["close"])}
                         for _, r in df_1m.iloc[:-1].iterrows()]
                _pat_ok, _pat_why = indicators.sweep_confirmation(
                    _bars, is_long=(direction == "LONG"),
                    min_body_abs=htf_atr_value * SWEEP_MOMENTUM_ATR_FRAC)
                if not _pat_ok:
                    print(f"[{base}] ⏭ {direction} sweep skipped — {_pat_why}")
                    continue

                # ATR-based stop: measure volatility on the higher timeframe (1m ATR is
                # inside the noise band) so the stop clears noise while the entry stays
                # on the 1m sweep. Fetched only now (on an actual signal), not every tick.
                atr_value = 0.0
                try:
                    atr_value = atr(exchange.fetch_ohlcv(symbol, STOP_ATR_TF, limit=STOP_ATR_LEN + 1),
                                    STOP_ATR_LEN)
                except Exception:
                    pass   # fall back to the fixed-buffer floor inside compute_stop_target
                sl, tp = compute_stop_target(direction, float(candle["high"]), float(candle["low"]),
                                              pool_high, pool_low, SL_BUFFER_PCT, atr_value)
                # Floor the stop outside 1h-ATR noise (see MIN_STOP_ATR_MULT_HTF above) — widens
                # it only when the 15m-ATR stop above would sit inside normal continuation.
                if htf_atr_value > 0:
                    sl = indicators.structural_stop_price(
                        price, sl, htf_atr_value, direction == "LONG", MIN_STOP_ATR_MULT_HTF)
                rr = compute_rr(price, sl, tp)
                if rr < MIN_RR:
                    print(f"[{base}] ⏭ Sweep {direction} skipped — R:R 1:{rr:.1f} < 1:{MIN_RR:.0f} min")
                    continue

                # AI confirmation, LAST — after every mechanical gate, so the model is
                # only asked about setups that already qualify and a slow call costs
                # nothing on the setups that were never going to trade. Shared with the
                # other two bots (bot/ai_model.confirm_setup) rather than a third private
                # copy, because the private copies are how both of them ended up with the
                # same fail-open. Unreadable reply = no trade; transport failure =
                # proceed on technicals, but labelled NO OPINION, never a green tick.
                _ai_ok, _ai_rr, _ai_why = _ai_model.confirm_setup(
                    {"symbol": symbol, "direction": direction, "entry": price,
                     "stop": sl, "target": tp, "rr": f"{rr:.1f}",
                     "trend": trend or "unknown", "pattern": _pat_why},
                    min_rr=MIN_RR, max_rr=MAX_AI_RR)
                _ai_icon = (("⚠️ NO OPINION" if _ai_model.is_no_opinion(_ai_why)
                             else "✅ YES") if _ai_ok else "❌ NO")
                print(f"[{base}] 🤖 AI: {_ai_icon}  (suggested 1:{_ai_rr:.1f})  {_ai_why[:130]}")
                if not _ai_ok:
                    continue

                qty = size_position(paper.balance, RISK_PCT, price, sl)
                if qty <= 0:
                    print(f"[{base}] ⏭ Sweep {direction} skipped — position size rounds to zero")
                    continue

                # Daily RISK budget: at most DAILY_RISK_PCT of the account may be LOST
                # per day across all trades, not released when a trade closes. This caps
                # the same thing the sizing measures, so it bounds a bad day without
                # touching position size — the stop distance still decides that.
                daily_cap       = paper.balance * DAILY_RISK_PCT
                daily_remaining = paper.risk_remaining(paper.balance, DAILY_RISK_PCT)
                if daily_remaining <= 0:
                    print(f"[{base}] ⏭ Sweep {direction} skipped — daily risk budget "
                          f"${daily_cap:.2f} ({DAILY_RISK_PCT:.0%} of account) used up, resets tomorrow")
                    continue
                qty, _was_trimmed = indicators.trim_qty_to_risk(qty, price, sl, daily_remaining)
                notional = qty * price
                margin   = notional / TEST_LEVERAGE
                if qty <= 0 or notional < 10.0:       # dust floor — not worth opening
                    print(f"[{base}] ⏭ Sweep {direction} skipped — only "
                          f"${daily_remaining:.2f} of the daily ${daily_cap:.2f} risk budget left")
                    continue
                trade_risk = qty * abs(price - sl)
                if _was_trimmed:
                    print(f"[{base}] ⚠️ Position trimmed — risking ${trade_risk:.2f}, "
                          f"all that is left of today's ${daily_cap:.2f} budget")

                if direction == "SHORT":
                    paper.sell(symbol, qty, price)
                else:
                    paper.buy(symbol, qty, price)
                paper.record_risk(trade_risk)
                sl_levels[symbol]    = sl
                tp_levels[symbol]    = tp
                entry_times[symbol]  = datetime.now(timezone.utc)
                trade_states[symbol] = "IN_TRADE"
                lev_tag = f"  [{TEST_LEVERAGE}x]" if TEST_LEVERAGE > 1 else ""
                print(f"[{base}] ⚡ SWEEP {direction} @ ${price:,.2f}  SL=${sl:,.2f}  "
                      f"TP=${tp:,.2f}  R:R=1:{rr:.1f}  qty={qty:.6f}  "
                      f"${notional:,.2f} deployed{lev_tag}")
                alert(f"⚡ SWEEP {direction} — {base}",
                      f"@ ${price:,.4f}  SL ${sl:,.4f}  TP ${tp:,.4f}  R:R 1:{rr:.1f}",
                      sound="Submarine",
                      speak=f"{base} sweep {direction.lower()} entry")

            except Exception as e:
                print(f"[{base}] Error: {e}")

        pos_str = "  ".join(
            f"{k.split('/')[0]}={'L' if v>0 else 'S'}{abs(v):.4f}"
            for k, v in paper.positions.items()
        ) or "flat"
        _equity = indicators.account_equity(
            paper.balance, paper.positions, paper.entry_prices,
            paper.margin_used, live_prices, TEST_LEVERAGE)
        print(f"  [CRYPTO] Cash: ${paper.balance:,.2f}  |  Equity: ${_equity:,.2f}  |  {pos_str}\n")

        if now - last_stats_print >= POOL_RECALC_SECONDS:
            print(stats_summary_line(stats, paper.balance, CRYPTO_BALANCE))
            last_stats_print = now

        save_test_state(paper, sl_levels, tp_levels, live_prices, pools, trade_states, stats, entry_times)

        # ── Fast SL/TP watcher ─────────────────────────────────────────────────
        # Check open positions every FAST_WATCH_SECONDS so a stop/target fills within
        # ~10s of being touched. Rather than just the latest candle, scan EVERY 1m
        # candle since the last check (tracked per symbol in last_watch_ms): if the
        # process was frozen — laptop sleep, a slow iteration, a network stall — the
        # gap's candles are still scanned on resume, so a TP/SL hit while suspended
        # can't be missed. (Seen live: ETH spiked $2.66 through TP at 14:05 UTC while
        # the Mac was asleep; the old limit=2 watcher never saw that candle on wake.)
        deadline = time.time() + POLL_SECONDS
        while time.time() < deadline:
            time.sleep(FAST_WATCH_SECONDS)
            closed_any = False
            for symbol in CRYPTO_SYMBOLS:
                if abs(paper.get_position(symbol)) < 1e-9:
                    last_watch_ms[symbol] = None   # flat — reset watermark for next trade
                    continue
                try:
                    since = last_watch_ms[symbol]
                    if since is None:
                        et = entry_times.get(symbol)
                        since = int(et.timestamp() * 1000) if et else int((time.time() - 180) * 1000)
                    # since -> forward, up to 300 candles; long gaps paginate across ticks
                    m = exchange.fetch_ohlcv(symbol, "1m", since=since, limit=300)
                    if not m:
                        continue
                    base = symbol.split("/")[0]
                    for candle in m:                       # oldest → newest: fill at the FIRST touch
                        hi, lo, close = float(candle[2]), float(candle[3]), float(candle[4])
                        if try_close_position(symbol, base, close, hi, lo):
                            closed_any = True
                            break
                    last_watch_ms[symbol] = m[-1][0] + 60_000   # advance past the last scanned candle
                    live_prices[symbol] = float(m[-1][4])
                except Exception:
                    pass
            if closed_any:
                save_test_state(paper, sl_levels, tp_levels, live_prices, pools, trade_states, stats, entry_times)


# ── Stock sweep-reversal bot (lumibot + Alpaca) ───────────────────────────────

def run_stock_sweep():
    try:
        from lumibot.strategies import Strategy
        from lumibot.entities import Asset, Order
        from lumibot.brokers import Alpaca
        from lumibot.traders import Trader

        _quiet_lumibot_noise()   # lumibot's handlers exist once it's imported

        class SweepTestBot(Strategy):
            def initialize(self):
                self.sleeptime    = "1M"
                self.entry_order  = None
                self.sl_order     = None
                self.tp_order     = None
                self.stop_loss    = None
                self.take_profit  = None
                self.entry_price  = None
                self.entry_time   = None
                self.pool_high    = None
                self.pool_low     = None
                self.last_recalc  = None   # datetime of the last 1H pool recalculation
                self.armed        = False
                # NOTE: named session_stats, not stats — lumibot's own Strategy base
                # class already defines a read-only `stats` property; assigning
                # self.stats crashes with "property 'stats' has no setter".
                self.session_stats    = new_stats()
                self.last_stats_print = None
                self.start_cash       = None   # captured lazily on first tick
                ensure_test_sheet()

            def _cancel_resting_orders(self):
                for attr in ("sl_order", "tp_order"):
                    order = getattr(self, attr)
                    if order is not None:
                        try:
                            self.cancel_order(order)
                        except Exception:
                            pass
                        setattr(self, attr, None)
                self.stop_loss   = None
                self.take_profit = None

            def on_filled_order(self, position, order, price, quantity, multiplier):
                # Single OCO order, not two separate stop + limit orders — submitting
                # them separately makes Alpaca hold the full share count against the
                # first one, so the second always gets rejected with "insufficient
                # qty available" (fails async, after we've already logged success).
                if order is not self.entry_order:
                    return
                self.entry_order = None
                asset = order.asset

                sl = self.stop_loss
                tp = self.take_profit
                self.entry_price = price
                self.entry_time  = datetime.now(timezone.utc)
                try:
                    oco_order = self.create_order(
                        asset, abs(quantity), Order.OrderSide.SELL,
                        order_class=Order.OrderClass.OCO,
                        limit_price=tp,
                        stop_price=sl,
                    )
                    self.submit_order(oco_order)
                    self.sl_order = oco_order
                    self.tp_order = oco_order
                    self.log_message(
                        f"[{STOCK_SYMBOL}] \U0001F6D1\U0001F3AF OCO order placed — SL @ ${sl:.2f}  TP @ ${tp:.2f}",
                        color="yellow"
                    )
                    alert(f"⚡ SWEEP LONG — {STOCK_SYMBOL}",
                          f"@ ${price:.2f}  SL ${sl:.2f}  TP ${tp:.2f}",
                          sound="Submarine",
                          speak=f"{STOCK_SYMBOL} sweep long entry")
                except Exception as e:
                    self.log_message(f"[{STOCK_SYMBOL}] OCO order failed: {e}", color="red")

            def on_trading_iteration(self):
                asset    = Asset(STOCK_SYMBOL, asset_type=Asset.AssetType.STOCK)
                price    = self.get_last_price(STOCK_SYMBOL)
                position = self.get_position(asset)
                now      = self.get_datetime()
                if self.start_cash is None:
                    self.start_cash = self.get_cash()

                if not (position and abs(position.quantity) > 0) and (
                    self.last_recalc is None or (now - self.last_recalc).total_seconds() >= POOL_RECALC_SECONDS
                ):
                    bars_1h = self.get_historical_prices(asset, POOL_LOOKBACK_1H, "1 hour")
                    if bars_1h is not None:
                        df_1h = bars_1h.pandas_df
                        if len(df_1h) >= POOL_LOOKBACK_1H:
                            self.pool_high, self.pool_low = compute_pools(df_1h, POOL_LOOKBACK_1H)
                            self.last_recalc = now
                            self.armed = True
                            self.log_message(
                                f"[{STOCK_SYMBOL}] \U0001F553 1H recalc — "
                                f"pool_high=${self.pool_high:.2f}  pool_low=${self.pool_low:.2f}"
                            )

                # Fetch the current 1m candle once per tick — used for logging (candle
                # classification, R:R preview) regardless of position state, and for
                # entry detection below when flat.
                candle, prev_candle = None, None
                bars_1m = self.get_historical_prices(asset, 3, "1 minute")
                if bars_1m is not None:
                    df_1m = bars_1m.pandas_df
                    if len(df_1m) >= 2:
                        candle      = df_1m.iloc[-2]   # last CLOSED candle — iloc[-1] is still forming
                        prev_candle = df_1m.iloc[-3] if len(df_1m) >= 3 else None

                has_position = bool(position and abs(position.quantity) > 0)
                sl_str      = f"  SL=${self.stop_loss:.2f}  TP=${self.take_profit:.2f}" if self.stop_loss else ""
                dist_high   = f"(→{(self.pool_high - price) / price * 100:+.1f}%)" if self.pool_high else ""
                dist_low    = f"(→{(price - self.pool_low) / price * 100:+.1f}%)" if self.pool_low  else ""
                candle_type = indicators.classify_candle(candle, prev_candle) if candle is not None else "n/a"
                preview = ""
                if self.armed and not has_position and self.pool_high is not None and candle is not None:
                    preview = preview_rr_string(candle, self.pool_high, self.pool_low)
                self.log_message(
                    f"[{STOCK_SYMBOL}] ${price:.2f}  pool_high={self.pool_high}{dist_high}  "
                    f"pool_low={self.pool_low}{dist_low}{sl_str}"
                )
                self.log_message(f"[{STOCK_SYMBOL}]   candle={candle_type}  armed={self.armed}{preview}")

                if self.last_stats_print is None or \
                   (now - self.last_stats_print).total_seconds() >= POOL_RECALC_SECONDS:
                    self.log_message(stats_summary_line(self.session_stats, self.get_cash(), self.start_cash))
                    self.last_stats_print = now

                # Manual SL/TP check — closes the position if the resting broker order
                # hasn't filled yet by the time we poll (keeps this in sync either way).
                # Wick-aware like the crypto side: a real stop/limit order fills the
                # instant price touches the level, not when a candle closes past it.
                if position and position.quantity > 0 and self.stop_loss and self.take_profit:
                    c_low  = float(candle["low"])  if candle is not None else price
                    c_high = float(candle["high"]) if candle is not None else price
                    sl_hit = price <= self.stop_loss or c_low  <= self.stop_loss
                    tp_hit = price >= self.take_profit or c_high >= self.take_profit
                    stale_hit = (not sl_hit and not tp_hit
                                 and is_trade_stale(self.entry_time, datetime.now(timezone.utc), STALE_TRADE_HOURS))
                    if sl_hit or tp_hit or stale_hit:
                        label = "SL" if sl_hit else "TP" if tp_hit else "STALE"
                        fill  = price if stale_hit else (self.stop_loss if sl_hit else self.take_profit)
                        pnl   = (fill - self.entry_price) * position.quantity if self.entry_price else None
                        # capture before _cancel_resting_orders() wipes them — the ledger needs both
                        sl_at_close, tp_at_close = self.stop_loss, self.take_profit
                        self._cancel_resting_orders()
                        self.submit_order(
                            self.create_order(asset, position.quantity, Order.OrderSide.SELL))
                        pnl_str = f"  P&L: ${pnl:+.2f}" if pnl is not None else ""
                        icon = "🟢" if label == "TP" else ("⏰" if label == "STALE" else "🔴")
                        self.log_message(
                            f"[{STOCK_SYMBOL}] {icon} {label} hit @ ${fill:.2f}{pnl_str}",
                            color="green" if label == "TP" else ("yellow" if label == "STALE" else "red"))
                        if pnl is not None:
                            record_trade_result(self.session_stats, pnl)
                        alert(f"{icon} {label} — {STOCK_SYMBOL}",
                              f"@ ${fill:.2f}" + (f"  P&L ${pnl:+.2f}" if pnl is not None else ""),
                              sound="Glass" if label == "TP" else "Basso",
                              speak=f"{STOCK_SYMBOL} {'take profit' if label == 'TP' else 'stale close' if label == 'STALE' else 'stop loss'} hit"
                                    + (f". {'Profit' if pnl >= 0 else 'Loss'} {abs(pnl):.0f} dollars"
                                       if pnl is not None else ""))
                        # Ledger + chart — real TradingView screenshot first (same system
                        # as the main stock bot), self-rendered chart from Alpaca bars as
                        # fallback. Fail-soft: never blocks the trading loop.
                        try:
                            entry_for_log = self.entry_price if self.entry_price else fill
                            chart = render_stock_tradingview(
                                STOCK_SYMBOL, "LONG", entry_for_log, sl_at_close or fill,
                                tp_at_close or fill, 1, self.entry_time,
                                datetime.now(timezone.utc))
                            if not chart:
                                bars_c = self.get_historical_prices(asset, 200, "5 minutes")
                                df_c = bars_c.pandas_df if bars_c is not None else None
                                chart = render_trade_chart(df_c, STOCK_SYMBOL, "LONG",
                                                            entry_for_log, sl_at_close or fill,
                                                            tp_at_close or fill, 1, "5m",
                                                            self.entry_time)
                            log_test_trade(STOCK_SYMBOL, True, entry_for_log, fill,
                                            float(position.quantity),
                                            pnl if pnl is not None else 0.0,
                                            sl_at_close, tp_at_close,
                                            self.entry_time, "sweep LONG", chart)
                        except Exception as e:
                            self.log_message(f"[{STOCK_SYMBOL}] Test ledger log failed: {e}", color="red")
                        self.entry_time  = None
                        self.entry_price = None
                        self.armed = False   # retired until the next 1H recalc
                        self.last_recalc = now   # restart the recalc countdown fresh from this close —
                        # never let a recalc that was merely deferred by an open position fire immediately on exit
                        return

                if has_position:
                    return   # already in a trade — nothing else to do this tick

                if not self.armed or self.pool_high is None or candle is None:
                    return

                entry_price = float(candle["close"])
                direction = detect_sweep(float(candle["high"]), float(candle["low"]),
                                          float(candle["close"]), self.pool_high, self.pool_low)
                if direction is None:
                    return
                if direction == "SHORT":
                    # Stock pipeline is long-only by design — see Global Constraints scope note.
                    return

                sl, tp = compute_stop_target(direction, float(candle["high"]), float(candle["low"]),
                                              self.pool_high, self.pool_low, SL_BUFFER_PCT)
                rr = compute_rr(entry_price, sl, tp)
                if rr < MIN_RR:
                    self.log_message(f"[{STOCK_SYMBOL}] ⏭ Sweep {direction} skipped — "
                                      f"R:R 1:{rr:.1f} < 1:{MIN_RR:.0f} min")
                    return

                qty = int(size_position(STOCK_TEST_BALANCE, RISK_PCT, entry_price, sl))
                if qty < 1:
                    self.log_message(f"[{STOCK_SYMBOL}] ⏭ Sweep {direction} skipped — qty rounds to zero")
                    return

                order = self.create_order(asset, qty, Order.OrderSide.BUY)
                self.entry_order = order
                self.stop_loss   = sl
                self.take_profit = tp
                self.armed       = False   # retire the pools until the next 1H recalc
                self.submit_order(order)
                self.log_message(f"[{STOCK_SYMBOL}] ⚡ SWEEP {direction} @ ${entry_price:.2f}  "
                                  f"SL=${sl:.2f}  TP=${tp:.2f}  R:R=1:{rr:.1f}  qty={qty}", color="green")

        print(f"[STOCKS] 1H/1M Sweep-Reversal on {STOCK_SYMBOL} | Alpaca paper "
              f"(waits for NYSE open 9:30 AM ET)")
        broker   = Alpaca({"API_KEY": API_KEY, "API_SECRET": API_SECRET, "PAPER": PAPER})
        strategy = SweepTestBot(broker=broker)
        trader   = Trader()
        trader.add_strategy(strategy)
        trader.run_all()

    except Exception as e:
        print(f"[STOCKS] Failed to start: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Single-instance lock — duplicate test_bot.py processes have raced against the
    # same state file this session; refuse to start a second copy rather than corrupt
    # shared state silently.
    from bot.single_instance_lock import acquire_single_instance_lock
    _lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test_bot.lock")
    if not acquire_single_instance_lock(_lock_path):
        print(f"⛔ Another test_bot.py instance is already running (lock: {_lock_path}). "
              f"Refusing to start a duplicate — kill the other process first if you really "
              f"want to restart.")
        sys.exit(1)

    crypto_only = "--crypto-only" in sys.argv

    print("=" * 65)
    if crypto_only:
        print("1H/1M SWEEP-REVERSAL TEST BOT — CRYPTO ONLY")
    else:
        print("1H/1M SWEEP-REVERSAL TEST BOT — STOCKS + CRYPTO")
        print(f"  Stocks : {STOCK_SYMBOL} via Alpaca paper  (long-only, fires at NYSE open)")
    print(f"  Crypto : {', '.join(s.split('/')[0] for s in CRYPTO_SYMBOLS)} via Coinbase  (24/7, long+short)")
    print(f"  Pools  : 1H, {POOL_LOOKBACK_1H}-candle lookback  |  Entries: 1M sweep+reversal")
    print(f"  Risk   : {RISK_PCT*100:.0f}% per trade  |  Min R:R: 1:{MIN_RR:.0f}")
    print("=" * 65)

    if not crypto_only:
        # Stock bot runs in a background thread (lumibot blocks internally)
        threading.Thread(target=run_stock_sweep, daemon=True).start()

    # Crypto bot runs in the main thread
    run_crypto_sweep()

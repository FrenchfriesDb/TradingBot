"""
CCXT bot — Debbie-La SMC state machine with paper trading.
Uses Kraken public data feed by default (no API key needed).
Set BINANCE_API_KEY/SECRET in .env to switch to real Binance trading.
"""

import time
import math
import os
import sys
import threading
from datetime import date, datetime, timedelta, timezone

# ── Single-instance lock — checked FIRST, before any slow import ───────────────
# Real incident: duplicate binance_bot.py instances have raced against the same
# crypto_state.json/paper account multiple times this session (a restart happening
# without confirming the old process had actually died), corrupting shared state.
# Checked here specifically (not after the lazy pandas/ccxt load below) so a
# duplicate start exits in milliseconds instead of waiting through a ~16-minute
# fresh-boot Gatekeeper scan first.
# Guarded on __main__ so this file stays IMPORTABLE while the bot is running. Tests and
# backtest_crypto.py import it purely to read constants (MAX_RISK_DOLLARS, MIN_AI_RR,
# STALE_ZONE_BARS…) so they can't silently drift from production; grabbing the lock at
# import time made any such import die with "already running" whenever the bot was live.
# Running the file directly still refuses a duplicate in milliseconds, which is the point.
from bot.single_instance_lock import acquire_single_instance_lock

# Tee stdout to logs/binance_bot.log whenever it would otherwise only reach a terminal.
# scripts/start_binance_bot.sh already redirects there, and this is a no-op in that case
# (see tee_logging.should_tee) — it exists for the HAND-STARTED runs, which have now
# blocked three separate live diagnoses by leaving no record of why a trade was taken:
# ASTER 2026-09-01, POL 2026-09-04, and the ASTER fill 4.19% outside its own zone on
# 2026-09-05. Must run before anything prints, so the startup banner is captured too.
if __name__ == "__main__":
    from bot.tee_logging import tee_stdout_to
    _TEED = tee_stdout_to(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "logs", "binance_bot.log"))
    if _TEED:
        print(f"📝 Also logging to {_TEED} (started from a terminal — teeing so this "
              f"session is still explainable afterwards)")

_LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".binance_bot.lock")
if __name__ == "__main__" and not acquire_single_instance_lock(_LOCK_PATH):
    print(f"⛔ Another binance_bot.py instance is already running (lock: {_LOCK_PATH}). "
          f"Refusing to start a duplicate — kill the other process first if you really "
          f"want to restart.")
    sys.exit(1)

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
        # An EMPTY file means a previous run was killed mid-write. Say so loudly instead
        # of silently no-op'ing: the replace below cannot repair it (patched == txt == "")
        # so the patch would skip the write forever while every ccxt import on the machine
        # fails. REAL INCIDENT 2026-09-12 16:23 — this file hit 0 bytes and the bot then
        # hung for 16.5h printing "Still loading…".
        if not txt.strip():
            print("⛔ ccxt _version.py is EMPTY — a previous patch was interrupted "
                  "mid-write. ccxt cannot import until it is restored:\n"
                  "     pip install --force-reinstall --no-deps ccxt", flush=True)
            return
        patched = txt.replace(
            'pieces["distance"] = int(count_out)',
            'pieces["distance"] = int(count_out) if count_out is not None else 0'
        )
        if patched != txt:
            # ATOMIC. open(path,"w") truncates the instant it is called, so a process
            # killed between the truncate and the write leaves a 0-byte file. Three bots
            # plus any verification import all run this at startup, and stop_*.sh kills
            # processes — the window is small but it is hit regularly enough to matter.
            # Write beside it, then rename: a rename is atomic, so the file is either the
            # old content or the new one, never nothing.
            _tmp = vpath + ".patchtmp"
            with open(_tmp, "w") as _fh:
                _fh.write(patched)
                _fh.flush()
                os.fsync(_fh.fileno())
            os.replace(_tmp, vpath)
            print("ccxt patched.")
    except Exception as e:
        print(f"ccxt patch skipped: {e}")

from config import (BINANCE_API_KEY, BINANCE_SECRET, BINANCE_TESTNET, BINANCE_CASH_AT_RISK,
                    NVIDIA_API_KEY, GOOGLE_SHEET_URL,
                    # Alpaca creds are used here ONLY to read the news feed (verified live
                    # to cover crypto symbols too) — never to place an order. Crypto fills
                    # stay entirely on the ccxt/Coinbase paper path.
                    API_KEY as ALPACA_KEY_FOR_NEWS,
                    API_SECRET as ALPACA_SECRET_FOR_NEWS)
from sheets_logger import (get_sheet_client, ensure_tabs, log_daily_snapshot, log_trade,
                            missing_snapshot_dates, MACRO_HEADER, LEDGER_HEADER)
from chart_renderer import render_trade_chart, save_chart_locally
from github_chart_uploader import upload_chart_to_github

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
_libs_failed = threading.Event()   # set when loading DIED, so run() can tell the
                                   # difference between 'slow' and 'never coming'
_libs_start = time.time()

def _load_heavy_libs():
    """Import the heavy libraries off the main thread.

    EVERY failure in here must be loud. This thread had no exception handling, so when
    `import ccxt` started raising on 2026-09-12 the thread died silently, _libs_ready was
    never set, and run()'s `while not _libs_ready.wait(60)` loop printed "Still loading…"
    once a minute for 16.5 HOURS. The bot looked patient; it was dead. A message that says
    "loading" while nothing is loading is worse than a crash.
    """
    global pd, ccxt, indicators
    try:
        _load_heavy_libs_inner()
    except BaseException as e:
        import traceback
        print("\n" + "=" * 70, flush=True)
        print(f"⛔ LIBRARY LOADING FAILED — {type(e).__name__}: {e}", flush=True)
        print("   The bot CANNOT trade. It is not still loading; it is stopped.", flush=True)
        traceback.print_exc()
        print("=" * 70 + "\n", flush=True)
        _libs_failed.set()
        _libs_ready.set()      # release run() so it can exit instead of waiting forever


def _load_heavy_libs_inner():
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
        print(f"[ALERT] skipped (TRADE_ALERTS=False): {title}")
        return
    # Diagnostic: user reports no sound/notification on real TP hits despite both
    # close paths correctly calling this function. Code-level firing is now visible
    # in the log regardless of whether the sound/notification actually reaches the
    # user (a system-level issue — DND, notification permissions, or the detached
    # background process's audio session — would show THIS log line firing with no
    # audible result, isolating it from a code bug where alert() never gets called).
    print(f"[ALERT] firing: {title} | {message}")
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


# Added 2026-08-20: HYPE, INJ, SEI, DRIFT, ASTER. All five verified on Coinbase for every
# timeframe _process_symbol pulls (5m/15m/1h/6h/1d) at full depth before being added.
# NOTE the HTF path: Bybit (the 4H source) returns 403 from this machine — a US geo-block —
# so connect_htf_exchange() falls back to 6H on Coinbase for ALL symbols. That fallback is
# what these were validated against; nothing here depends on Bybit coming back.
DEFAULT_SYMBOLS  = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "XRP/USD", "AVAX/USD", "POL/USD", "ADA/USD",
                    "HYPE/USD", "INJ/USD", "SEI/USD", "DRIFT/USD", "ASTER/USD"]
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

# Structural SL: FVG-zone edge + a 5m-ATR breathing-room buffer, widened further by the
# swing-guard if a manipulation wick sits beyond the zone. That distance is then FLOORED
# (never capped) against 1H-ATR noise via structural_stop_price — entries stay on the
# 5m chart, but the stop can never be tighter than ~1.5x a full 1H candle's normal range.
# OLD behavior capped the stop at 3x the 5m ATR, which crushed it onto liquid/quiet
# pairs (AVAX $0.06 stop, ADA $0.0002 stop) — right into single-candle noise. A floor
# fixes that without slowing down entries.
SL_ATR_MULT          = 1.5   # SL placed 1.5× 5m-ATR outside the FVG edge (dynamic breathing room)
MIN_STOP_ATR_MULT_HTF = 1.5  # stop distance floored at 1.5× the 1H ATR — outside 1H-candle noise
SWING_LOOKBACK  = 12    # candles to scan for structural swing high/low (wick-sweep guard)

# ── Entry-quality filters (make the AMD manipulation setup the primary trigger) ──
# The wedge/chase/BOS continuation setups kept firing late into chop, then bled out to
# the 6h stale-timer. These gates make them prove momentum + room first, so the cleaner
# AMD-manipulation setup (checked earlier in IDLE) wins more of the trades.
DISPLACEMENT_BODY_FRAC = 0.5   # a displacement candle's body must be ≥50% of its range
# ...and ≥ max(ATR_MULT × ATR, MIN_PCT × price) — see indicators.displacement_min_body.
#
# RAISED FROM 0.6 on 2026-09-04. A multiple below 1.0 means "smaller than an AVERAGE
# candle", so an utterly ordinary bar cleared the momentum test: POL/USD armed a long on
# a bar measuring 1.07× ATR, which the operator could not find on the chart because there
# was nothing to find. Displacement must mean OUTLIER — the multiple has to exceed 1.0.
# This is a ~3× tightening and WILL cut trade frequency. That is the intent.
DISPLACEMENT_ATR_MULT  = float(os.getenv("DISPLACEMENT_ATR_MULT", "1.8"))
# Backstop for a flat tape. When ATR collapses the ATR term collapses with it, so even
# 1.8× a dead ATR can be invisible — POL's 5m ATR was 0.169% of price while its 6H ATR
# (2.755%) sailed through ATR_GATE. That timeframe mismatch is what this floor closes.
DISPLACEMENT_MIN_PCT   = float(os.getenv("DISPLACEMENT_MIN_PCT", "0.0015"))
# Minimum FVG width. A thinner gap is a LINE, not a zone: price grazes it on any tick and
# the retest carries no information. POL armed on a gap 0.00001 wide — 0.011% of price.
MIN_FVG_PCT            = float(os.getenv("MIN_FVG_PCT", "0.0015"))
# Target reachability. Positions are force-closed at STALE_TRADE_HOURS (one HTF candle),
# so a target beyond this × the HTF ATR needs several average HTF candles of travel
# inside the span of one. It cannot resolve — it just prints a flattering R:R on the way
# in and exits on the timer. POL: target 13.3% away vs a 2.755% 6H ATR, reported 1:9.4.
MAX_TARGET_ATR_MULT    = float(os.getenv("MAX_TARGET_ATR_MULT", "1.5"))
# How far a swept level may sit from price and still be treated as THIS move's
# inducement. Volatility-relative, with the percentage as a floor so a dead ATR read
# tightens the gate rather than opening it. 2.5x/2% refuses the 2026-09-17 entries
# (AVAX 5.7% away, SEI 6.3%, ADA 5.8%) while leaving a normal post-sweep retest alone.
SWEEP_MAX_ATR_MULT     = float(os.getenv("SWEEP_MAX_ATR_MULT", "2.5"))
SWEEP_MAX_PCT          = float(os.getenv("SWEEP_MAX_PCT", "0.02"))
STALE_ZONE_BARS        = 6     # ~30 min on 5m — a zone armed longer than this needs FRESH
                                # displacement at tap time, not just a technically-qualifying
                                # signal from a dead tape. Shared by the main 5m cycle AND the
                                # 10s sniper watcher (see the sniper's own staleness check below —
                                # real incident: a SOL zone sat stale 23 bars/~2h, the main cycle's
                                # gate would have correctly rejected the eventual weak tap, but the
                                # sniper watcher fired anyway because it never checked staleness at all).
OVERHEAD_MIN_ROOM_ATR  = 2.0   # need ≥2× 5m ATR of clear air to the opposing pool to enter

# ── PURE-SNIPER MODE ──────────────────────────────────────────────────────────
# Only fire the two high-conviction setups the trader would take by hand:
#   1. AMD liquidity sweep → CHoCH/displacement → FVG/OB retest  (the priority engine)
#   2. BOS displacement retest  (a real momentum break of structure, then retest)
# The two low(er)-conviction engines below are GATED OFF — they were the source of the
# "dip-buy in consolidation that bleeds to the 6h timer" trades:
ENABLE_TREND_FOLLOW   = False  # "buy the discount in a clear daily trend" fallback (no sweep/BOS)
ENABLE_WEDGE_BREAKOUT = False  # falling-wedge breakout — a generic pattern, not a sweep or BOS
# Breakout chase: enter at MARKET when price ran away from a zone without tapping it.
# Was the only continuation engine with NO enable flag, so "pure-sniper mode" never
# actually gated it — and of the 26 trades in the 2026-08/09 log, all 13 whose zone
# could be recovered were chases, filling 5-8% above their zone. It abandons the
# retest thesis by design, so it belongs behind the same switch as the other two.
ENABLE_CHASE = os.getenv("ENABLE_CHASE", "0").lower() in ("1", "true", "yes")
PAUSE_NEW_ENTRIES     = False  # LIVE as of 2026-08-15 (user decision). Paused 2026-08-11 after a
                                # real AVAX SHORT (entry $6.231, zone $6.24-$6.454, $79 risk, in a
                                # market whose whole prior 4h range was 1.5%) exposed the true cause
                                # of the recurring "armed long ago, entered on nothing" trades. The
                                # earlier 2026-08-04 max_age_bars diagnosis was WRONG — a real bug,
                                # but not this mechanism. All four defects are now fixed and each was
                                # verified to independently block that exact trade:
                                #   (a) carried_zone_age — zones used to re-arm for free: an expired
                                #       zone fell to IDLE, got re-derived identically from slow-moving
                                #       6h data, and re-armed with bars_in_entry_wait back at 0,
                                #       forever, so STALE_ZONE_BARS could never fire. Age now carries
                                #       across re-arms (the live zone read 3 bars while actually ~20).
                                #   (b) drop_forming_candle — zone edges now come from CLOSED candles
                                #       only. The stored edge was $6.24 while a fresh call returned
                                #       $6.326, because that bar was still forming; price sat 1.5%
                                #       below the live zone and only filled against the stale snapshot.
                                #   (c) price_in_entry_zone — a SHORT must now actually REACH its
                                #       supply zone. The old symmetric ±0.15% band let it fill BELOW
                                #       the zone (selling supply at a discount) by $0.0003.
                                #   (d) cap_qty_for_risk — the main path sized by MARGIN with no risk
                                #       cap, so a wide stop meant $79 at risk; now hard-capped at
                                #       MAX_RISK_DOLLARS ($20) like the sniper path always was.
                                # These are verified against historical data, NOT yet proven in live
                                # trading. If the "no confirmation / entered on nothing" pattern shows
                                # up again, set this back to True and diagnose from raw candles before
                                # patching — that has been the reliable method every time.

# ── Simulated leverage (paper perps mode) ──────────────────────────────────────
# Set PAPER_LEVERAGE > 1 to simulate perpetual futures returns WITHOUT real
# liquidation risk. The paper trader multiplies P&L by this factor, and also
# tracks whether a loss would have liquidated a real perp position (warning only).
# Keep at 1 for pure spot simulation. Recommended progression: 1 → 3 → 5 → 10.
PAPER_LEVERAGE = 10   # 1 = spot (default). Change to 3, 5, or 10 to simulate perps.
# ── Risk budget: a PERCENTAGE of live equity, not a fixed dollar amount ───────────
# A hardcoded dollar figure is a different bet at every account size. MAX_RISK_DOLLARS=20
# means 0.2% on the $10,000 paper book and 20% on a real $100 account — five losses from
# zero — so the constant silently changed meaning with the balance. RISK_PCT fixes the
# meaning, compounds as the account grows, and de-risks itself in a drawdown.
#
# 0.002 reproduces today's $20 on today's equity: this is a change of MECHANISM, not of
# risk appetite. Raising it is a separate decision that belongs after ~30 clean trades.
#
# Leverage is deliberately NOT in this calculation. Risk is (entry - stop) x qty; leverage
# only changes the margin posted. That is what lets the same constants work on 1x spot and
# on 10x perps, so PAPER_LEVERAGE stays free to change.
RISK_PCT     = float(os.getenv("RISK_PCT", "0.002"))       # 0.2% of equity per trade
RISK_FLOOR   = float(os.getenv("RISK_FLOOR", "0.0"))       # never size below this
RISK_CEILING = float(os.getenv("RISK_CEILING", "0")) or None  # circuit breaker; 0 = off
MAX_RISK_DOLLARS = 20.0  # fixed-risk sizing: every stop-out loses ~$20. Position size is
                         # derived from the stop distance (qty = $20 / |entry-SL|), so a full
                         # 2R winner ≈ $40 and a 3R winner ≈ $60 — winners outrun losses by design.
                         # Raised 8→20 on 2026-08-12 by user decision, aligning this with the
                         # long-standing ~$20/trade sizing plan (BINANCE_CASH_AT_RISK=0.02 ≈ $200
                         # margin). The two had silently disagreed: the 10-sec sniper path sized
                         # to $8 while the main path sized by MARGIN and had no risk cap at all
                         # (a 3.9% stop put $79 at risk on the real 2026-08-11 AVAX SHORT). Both
                         # paths now honour this single number.


# ── Paper trader ───────────────────────────────────────────────────────────────

# ── Trading costs ─────────────────────────────────────────────────────────────
# PaperTrader charged NO fees until 2026-08-22, which made every ledger number gross.
# Audit of 121 real trades: $148,063 notional produced $725 gross — a 0.49% return on
# notional, against taker fees of 0.25-0.60% PER SIDE. Break-even was 0.245%/side, i.e.
# the strategy sat exactly on the fee line while the dashboard showed it clearly green.
# Fees are charged on NOTIONAL (qty x price), both entry and exit — not on margin.
TAKER_FEE_RATE = float(os.getenv("TAKER_FEE_RATE", "0.0025"))   # 0.25%/side default
# Maker rate, for orders that REST on the book instead of crossing it. Defaults to the
# TAKER rate on purpose: assuming a discount the account has not been verified to get is
# exactly the kind of optimism that made P&L look better than the balance. Set it from
# your real Coinbase fee tier (Advanced Trade fee schedule) and it starts mattering.
#
# Which legs can earn it:
#   entry        resting limit at the zone edge   -> MAKER
#   take-profit  resting limit above/below        -> MAKER
#   stop-loss    triggers and CROSSES the book    -> TAKER, always
# So a winner pays maker+maker and a loser pays maker+taker. At a 1.73% stop that is
# ~29% of the risk budget today vs ~14% blended — roughly half, not the ~7% that
# assuming maker on BOTH legs would suggest.
MAKER_FEE_RATE = float(os.getenv("MAKER_FEE_RATE", os.getenv("TAKER_FEE_RATE", "0.0025")))
# Rest the sniper's entry on the book instead of crossing it. OFF by default: it is not a
# free win. A resting buy limit at the zone's near edge fills AT that edge, which is a
# WORSE price than market-buying after price has fallen through the zone — you trade fill
# quality for the maker rate, and some taps stop filling at all. Turn it on once your real
# fee tier is in MAKER_FEE_RATE, so the discount is verified rather than assumed.
MAKER_ENTRIES = os.getenv("MAKER_ENTRIES", "0").lower() in ("1", "true", "yes")
ROUND_TRIP_COST = TAKER_FEE_RATE * 2                            # what a full trade costs
# A target must clear the round trip by this multiple or the setup is skipped.
# 3x means: on a 0.50% round trip, the target must be at least 1.50% away.
MIN_FEE_CLEARANCE = float(os.getenv("MIN_FEE_CLEARANCE", "3.0"))


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
        # Recomputed from equity once per cycle (see the main loop). Every sizing site
        # reads THIS rather than a module constant, so one refresh moves them all and
        # they cannot drift apart — which is how the main path and the sniper path came
        # to disagree about sizing in the first place.
        self.risk_budget = balance * RISK_PCT
        self.last_alive_ms = 0   # set by load_crypto_state; 0 = no prior session
        self.total_fees = 0.0          # cumulative trading costs, for honest reporting
        self.maker_fees = 0.0          # split out so you can see if resting orders land
        self.taker_fees = 0.0
        self.positions: dict = {}      # symbol -> qty (neg = short)
        self.entry_prices: dict = {}   # symbol -> avg entry price
        self.margin_used: dict = {}    # symbol -> margin posted (for liq tracking)
        self.trade_count = 0
        self.daily_margin = 0.0        # total margin deployed today (reset each UTC day)
        self.daily_date   = None       # UTC date of last reset

    def check_daily_cap(self, margin_needed: float, daily_cap_frac: float = 0.05) -> float:
        """Return how much margin is actually available under the daily cap (5% of balance)."""
        today = datetime.now().astimezone().date()   # LOCAL day so the cap resets at your midnight
        if self.daily_date != today:
            self.daily_margin = 0.0
            self.daily_date   = today
        cap = self.balance * daily_cap_frac
        return max(0.0, cap - self.daily_margin)

    def record_margin(self, margin: float):
        """Call after an entry is confirmed to count margin against today's cap."""
        self.daily_margin += margin

    def _charge_fee(self, qty: float, price: float, liquidity: str = "taker") -> float:
        """Deduct the exchange fee for one side of a trade and return what was charged.

        Charged on NOTIONAL, because that is what an exchange bills — at 10x leverage an
        $80 margin position controls $800, and the fee is on the $800."""
        rate = MAKER_FEE_RATE if liquidity == "maker" else TAKER_FEE_RATE
        fee = abs(qty) * price * rate
        self.balance -= fee
        self.total_fees += fee
        # split for reporting: knowing WHICH side the cost came from is what tells you
        # whether the limit-order work is actually landing.
        if liquidity == "maker":
            self.maker_fees = getattr(self, "maker_fees", 0.0) + fee
        else:
            self.taker_fees = getattr(self, "taker_fees", 0.0) + fee
        return fee

    def get_position(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def equity(self, prices: dict) -> float:
        """Mark-to-market account value = free cash + Σ(locked margin + unrealized P&L).
        This is the true portfolio value a broker shows, not self.balance (free cash).
        Self-contained (mirrors indicators.account_equity, which is unit-tested) so it
        never depends on the lazy-loaded `indicators` module being ready."""
        eq = self.balance
        for sym, qty in self.positions.items():
            if abs(qty) < 1e-9:
                continue
            entry = self.entry_prices.get(sym, 0.0)
            cur = (prices or {}).get(sym, entry)
            upnl = (cur - entry) * qty if qty > 0 else (entry - cur) * abs(qty)
            margin = self.margin_used.get(sym, abs(qty) * entry / PAPER_LEVERAGE)
            eq += margin + upnl
        return eq

    def buy(self, symbol: str, qty: float, price: float, liquidity: str = "taker"):
        """Open long, or cover an existing short."""
        held = self.positions.get(symbol, 0.0)
        if held < 0:
            # Cover short: return margin + leveraged P&L
            cover = min(qty, abs(held))
            frac  = cover / abs(held)
            full_margin     = self.margin_used.get(symbol, abs(held) * self.entry_prices.get(symbol, price) / PAPER_LEVERAGE)
            margin_returned = full_margin * frac
            raw_pnl = (self.entry_prices.get(symbol, price) - price) * cover
            self._charge_fee(cover, price, liquidity)   # exit leg of a short
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
            self._charge_fee(qty, price, liquidity)     # entry leg of a long
            self.positions[symbol] = held + qty
            self.entry_prices[symbol] = price
            self.margin_used[symbol] = cost
        self.trade_count += 1
        return {"id": self.trade_count, "qty": qty, "price": price}

    def sell(self, symbol: str, qty: float, price: float, liquidity: str = "taker"):
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
            self._charge_fee(qty, price, liquidity)     # exit leg of a long
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
            self._charge_fee(qty, price, liquidity)     # entry leg of a short
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


_MARKET_LIMITS_CACHE = {}


def exchange_minimums(exchange, symbol):
    """(min_amount, min_cost) for a symbol from ccxt's market limits, or (None, None).

    Cached: load_markets() is a network call and these limits do not move intraday.

    This matters far more on a small account than a large one. A risk-sized order is
    qty = budget / stop-distance, so a $100 account at 0.2% produces a ~$10 position —
    under the minimum notional on plenty of pairs. The paper book never noticed because
    nothing was validating against a real venue. Returning (None, None) on any failure
    means "no constraint known", which keeps a data hiccup from vetoing every entry.
    """
    key = (id(exchange), symbol)
    if key in _MARKET_LIMITS_CACHE:
        return _MARKET_LIMITS_CACHE[key]
    out = (None, None)
    try:
        if not getattr(exchange, "markets", None):
            exchange.load_markets()
        lim = (exchange.market(symbol) or {}).get("limits", {}) or {}
        out = ((lim.get("amount") or {}).get("min"), (lim.get("cost") or {}).get("min"))
    except Exception:
        pass
    _MARKET_LIMITS_CACHE[key] = out
    return out


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


def save_crypto_state(paper: "PaperTrader", states: dict, symbols: list, prices: dict,
                       daily_snap: dict = None):
    """Persist the full bot state to JSON — positions, balances, and all state machine fields.
    daily_snap (optional): {"date": date|None, "open_balance": float, "open_btc": float|None} —
    the Crypto Macro once-per-day snapshot dedup tracker. Without persisting this, every
    restart resets it to None and re-logs "today" again, producing duplicate same-day rows
    (seen live: 5 rows for one date after a day of restarts)."""
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
                "entry_stop_loss":    st.entry_stop_loss,
                "take_profit":        st.take_profit,
                "bars_in_entry_wait": st.bars_in_entry_wait,
                "entry_time":         st.entry_time.isoformat() if st.entry_time else None,
                "amd_phase":          st.amd_phase,
                "amd_zone_type":      st.amd_zone_type,
                "partial_taken":      st.partial_taken,
                "banked_pnl":         st.banked_pnl,
                "breakeven_moved":    st.breakeven_moved,
                "stop_moved_ms":      st.stop_moved_ms,
                "zone_set_price":     st.zone_set_price,
                "eql_level":          st.eql_level,
                "eql_touch":          st.eql_touch,
                "eqh_level":          st.eqh_level,
                "eqh_touch":          st.eqh_touch,
                "trendline":          st.trendline,
                # SMC context markings (chart overlay only)
                "bos_level":          st.bos_level,
                "bos_dir":            st.bos_dir,
                "choch_level":        st.choch_level,
                "choch_dir":          st.choch_dir,
                "disp_high":          st.disp_high,
                "disp_low":           st.disp_low,
                "ote_low":            st.ote_low,
                "ote_high":           st.ote_high,
                "inducement":         st.inducement,
                "sniper_armed":       st.sniper_armed,
                "sniper_sl":          st.sniper_sl,
                "sniper_tp":          st.sniper_tp,
                "sniper_qty":         st.sniper_qty,
                "sniper_margin":      st.sniper_margin,
            }

        # TRUE equity = free cash + (locked margin + unrealized P&L) of every open
        # position — i.e. mark-to-market account value, exactly what Coinbase/Kraken show.
        # paper.balance alone is only FREE CASH (margin is deducted on open), so opening a
        # position made the header drop as if money vanished and never came back. Writing
        # a real `equity` field (which chart_server already prefers) fixes the display.
        open_equity = sum((p.get("margin") or 0.0) + (p.get("unrealized_pnl") or 0.0)
                          for p in positions.values())
        equity = paper.balance + open_equity

        data = {
            "last_updated":  datetime.now(timezone.utc).isoformat(),
            "balance":       paper.balance,
            "equity":        equity,
            "start_balance": PAPER_BALANCE,
            "trade_count":   paper.trade_count,
            # Cumulative trading costs. Charged in memory since 2026-08-22 but never
            # persisted, so they vanished on restart and never reached the dashboard —
            # which defeats the point of modelling them at all.
            "total_fees":    getattr(paper, "total_fees", 0.0),
            "fee_rate":      TAKER_FEE_RATE,
            "state_machine": state_machine,
            "positions":     positions,
            "daily_snapshot": {
                "date":         daily_snap["date"].isoformat() if daily_snap and daily_snap.get("date") else None,
                "open_balance": daily_snap.get("open_balance") if daily_snap else None,
                "open_btc":     daily_snap.get("open_btc") if daily_snap else None,
            } if daily_snap else None,
        }
        with open(CRYPTO_STATE_FILE, "w") as fh:
            _json.dump(data, fh, indent=2)
    except Exception as e:
        print(f"[STATE] save failed: {e}")


def load_crypto_state(paper: "PaperTrader", states: dict, symbols: list, daily_snap: dict = None):
    """Restore paper trader and all state machine fields from the last save. If
    daily_snap is passed, it's mutated in place with the saved once-per-day
    snapshot tracker (see save_crypto_state) so a restart can't re-log today."""
    import json as _json
    if not os.path.exists(CRYPTO_STATE_FILE):
        return
    try:
        with open(CRYPTO_STATE_FILE) as fh:
            data = _json.load(fh)

        # When the bot was last alive. The startup catch-up replays this gap's candles so
        # a stop that was breached while the process was DOWN still gets honoured — the
        # paper stop exists only in-process, so that window is genuinely unprotected.
        paper.last_alive_ms = 0
        try:
            _lu = data.get("last_updated")
            if _lu:
                paper.last_alive_ms = int(
                    datetime.fromisoformat(_lu).timestamp() * 1000)
        except Exception:
            pass

        paper.balance     = data.get("balance", PAPER_BALANCE)
        paper.trade_count = data.get("trade_count", 0)
        # Carry cumulative fees across restarts — otherwise the counter resets to 0 and
        # understates what the strategy has actually paid to trade.
        paper.total_fees  = data.get("total_fees", 0.0)

        if daily_snap is not None:
            ds = data.get("daily_snapshot")
            if ds:
                if ds.get("date"):
                    try:
                        daily_snap["date"] = date.fromisoformat(ds["date"])
                    except Exception:
                        pass
                if ds.get("open_balance") is not None:
                    daily_snap["open_balance"] = ds["open_balance"]
                if ds.get("open_btc") is not None:
                    daily_snap["open_btc"] = ds["open_btc"]

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
            st.entry_stop_loss    = saved.get("entry_stop_loss")
            st.take_profit        = saved.get("take_profit")
            st.bars_in_entry_wait = saved.get("bars_in_entry_wait", 0)
            st.amd_phase          = saved.get("amd_phase")
            st.amd_zone_type      = saved.get("amd_zone_type")
            st.partial_taken      = saved.get("partial_taken", False)
            st.banked_pnl         = saved.get("banked_pnl", 0.0)
            st.breakeven_moved    = saved.get("breakeven_moved", False)
            st.stop_moved_ms      = saved.get("stop_moved_ms", 0)
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
        self.entry_stop_loss = None  # the REAL entry-time stop, set once at fill, NEVER
                                      # mutated by the breakeven trail — logged to the
                                      # ledger instead of the live (possibly-trailed)
                                      # stop_loss so R:R reflects actual risk taken.
        self.take_profit = None
        self.entry_time = None
        self.bars_in_entry_wait = 0
        # ── Zone-age memory (survives reset(); see arm_zone below) ──────────────
        # Without these, an expired zone fell to IDLE, got re-derived identically from
        # the same slow-moving 6h data, and re-armed with bars_in_entry_wait back at 0
        # — forever. The counter measured time since the last RE-ARM, never how long
        # the zone had actually existed, so STALE_ZONE_BARS could never fire.
        self.last_zone_lo = None
        self.last_zone_hi = None
        self.last_zone_age = 0
        self.ranging_mode = False  # True when daily trend is unclear — use half size
        self.amd_phase = None      # 'manipulation_up'|'manipulation_down' when AMD override active
        self.amd_zone_type = None  # 'bearish_fvg'|'bearish_ob'|'ifvg'|'bullish_fvg'|...
        self.partial_taken = False    # True after scaling out the first 50% tranche
        self.banked_pnl = 0.0         # profit realized by the scale-out leg — added to the
                                      # final ledger row so the sheet shows the trade's TRUE total
        self.breakeven_moved = False  # True after SL trailed to +0.5R profit lock
        self.stop_moved_ms = 0        # when the stop was last MOVED (ms epoch). A stop
                                      # cannot fill on a candle older than itself.
        self.zone_set_price = None    # price when a trend_follow zone was armed (for stale-zone invalidation)
        self.eql_level = None; self.eql_touch = 0   # latest equal-lows pool (for chart overlay)
        self.eqh_level = None; self.eqh_touch = 0   # latest equal-highs pool (for chart overlay)
        self.trendline = None    # {kind,t1,p1,t2,p2} diagonal trendline (for chart overlay)
        # ── SMC context markings (chart overlay ONLY — never gate an entry) ──────
        self.bos_level = None; self.bos_dir = None      # break-of-structure level + direction
        self.choch_level = None; self.choch_dir = None  # 15m change-of-character
        self.disp_high = None; self.disp_low = None     # latest displacement candle's range
        self.ote_low = None; self.ote_high = None       # fib Optimal Trade Entry band
        self.inducement = None   # minor liquidity swept before the zone tap
        self.ai_reject_count = 0  # consecutive AI rejections; reset to IDLE at threshold
        self.is_chase = False     # True when the current ENTRY_WAIT is a breakout-chase, not a retest
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
        # Carry the abandoned zone's identity and age across the reset. __init__()
        # zeroes everything, which is exactly how a re-armed zone used to look brand
        # new; arm_zone() reads these back so the freshness clock keeps running.
        _lo, _hi = self.fvg_low, self.fvg_high
        _age = max(self.bars_in_entry_wait, self.last_zone_age
                   if (self.last_zone_lo == _lo and self.last_zone_hi == _hi) else 0)
        self.__init__()
        self.last_zone_lo, self.last_zone_hi, self.last_zone_age = _lo, _hi, _age

    def arm_zone(self, lo, hi):
        """Set the entry zone and start its freshness clock. If this is the SAME zone
        re-arming after an expiry/reset, its age carries forward instead of restarting
        at 0 — so a zone that has really been sitting for hours reads as stale and gets
        rejected by the STALE_ZONE_BARS gate rather than looking freshly armed."""
        self.fvg_low, self.fvg_high = lo, hi
        self.bars_in_entry_wait = indicators.carried_zone_age(
            lo, hi, self.last_zone_lo, self.last_zone_hi, self.last_zone_age)


# ── Open-trade management: scale-out + break-even ──────────────────────────────

def manage_open_trade(paper, state, symbol, cur_price, base):
    """Let winners RUN so realized R:R can't invert. Runs from both the 5-min loop and
    the 10s watcher. Deliberately does NOT scale out early — the full position rides to
    its target, so a 2R target pays the full ~$16 (3R ~$24) instead of the old half-at-1R
    scale-out that clipped every winner to ~$8 while losers took the full stop.
      • BREAK-EVEN (+ fee buffer) only after +1.25R — enough breathing room that a normal
        pullback doesn't scratch the trade, but once it's meaningfully in profit it can no
        longer become a loss.
    Idempotent: the break-even move fires at most once per trade."""
    if state.state != "POSITION_OPEN" or not state.entry_price or not state.take_profit:
        return
    held = paper.get_position(symbol)
    if abs(held) < 1e-9:
        return
    is_long = held > 0
    entry   = state.entry_price
    if not state.stop_loss:
        return
    # Measure progress in R-multiples off the ORIGINAL risk, not fraction-of-target, so
    # the +1.25R trigger is correct no matter what target multiple the setup used. This
    # fires before stop_loss is modified (guarded by breakeven_moved), so stop_loss here
    # is still the entry risk.
    risk_dist = abs(entry - state.stop_loss)
    if risk_dist <= 0:
        return
    favorable  = (is_long and cur_price > entry) or (not is_long and cur_price < entry)
    r_multiple = (abs(cur_price - entry) / risk_dist) if favorable else 0.0

    # Break-even + fee buffer at +1.25R.
    if r_multiple >= 1.25 and not state.breakeven_moved:
        # Must cover the FULL round trip, not one side. The old hardcoded 0.001 (0.1%) was
        # under a quarter of a real 0.5% round trip at 0.25%/side, so every "break-even"
        # exit was a guaranteed net loss. Measured on 121 real trades: 34 break-even exits
        # were -$1.15 gross but -$228 once fees were applied — the single biggest leak,
        # and the reason the bot re-entered the same setup over and over.
        # x1.5 so break-even is a genuine tiny win rather than exactly zero.
        fee_buffer = entry * ROUND_TRIP_COST * 1.5
        state.stop_loss = entry + fee_buffer if is_long else entry - fee_buffer
        state.breakeven_moved = True
        # Stamp the move. The watcher's wick check must not fill this stop on candles that
        # printed BEFORE it existed — and to reach +1.25R price had to rise THROUGH this
        # level, so those candles always contain a qualifying low.
        state.stop_moved_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        print(f"[{base}] 🛡 +1.25R reached — SL moved to break-even ${state.stop_loss:,.4f} "
              f"(trade can no longer lose; still riding for the full target)")


def _fetch_ohlcv_full(exchange, symbol, timeframe, tf_ms, since_ms, total_bars, per_call_cap=300):
    """Paginate fetch_ohlcv calls to get MORE candles than a single exchange request
    allows, so a long trade's chart always covers its full span at the requested
    granularity instead of silently truncating. Coinbase caps a single call at 300
    bars regardless of the limit requested (verified live) — at 5m that's ~25h, so
    any trade open longer than that needs multiple calls stitched together."""
    all_candles = []
    cursor = since_ms
    while len(all_candles) < total_bars:
        remaining = total_bars - len(all_candles)
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=cursor,
                                     limit=min(remaining, per_call_cap))
        if not batch:
            break
        all_candles.extend(batch)
        cursor = batch[-1][0] + tf_ms   # advance past the last candle returned
        if len(batch) < min(remaining, per_call_cap):
            break   # exchange returned fewer than asked -> no more data available
    return all_candles[:total_bars]


# ── Google Sheets trade ledger ──────────────────────────────────────────────────

def _log_trade_close_to_sheet(base, is_long, entry_price, exit_price, qty, pnl, state, exchange, fees=0.0,
                              exit_reason=""):
    """Fire-and-forget: append a completed round-trip row to the Crypto Ledger tab,
    including a candlestick chart of the trade hosted on GitHub (Drive hosting isn't
    viable — service accounts have no storage quota and can't accept ownership
    transfers) with a local charts/ save as a fallback if the GitHub push fails.
    Called from BOTH close points (the 5-min loop's close_position() and the 10s
    watcher's inline close) so every exit gets logged regardless of which path
    caught it. sheets_logger's own functions are already fail-soft; the chart
    render/upload is separately wrapped so a bad candle fetch, GitHub hiccup, or
    disk error can never block the ledger row itself from being written."""
    reason = (f"{'CHASE ' if state.is_chase else ''}{state.amd_phase or 'BOS'} "
              f"{'LONG' if is_long else 'SHORT'}")
    now = datetime.now(timezone.utc)

    chart_ref = None
    try:
        # ALWAYS 5-minute candles, no matter how long the trade ran — matches what's
        # actually watched live, so the archived chart never looks unrecognizably
        # coarser than what was seen in real time. A long trade needs MORE bars, not
        # a bigger timeframe, so this paginates past Coinbase's 300-bar single-call
        # cap (verified live) instead of stepping up to 15m/1h candles.
        tf, tf_min = "5m", 5
        dur_min = (max(1.0, (now - state.entry_time).total_seconds() / 60)
                   if state.entry_time else 60.0)
        bars = int(dur_min / tf_min) + 60   # + ~60 bars of pre-entry context
        since_ms = int((now - timedelta(minutes=bars * tf_min)).timestamp() * 1000)
        candles = _fetch_ohlcv_full(exchange, f"{base}/USD", tf, tf_min * 60 * 1000,
                                     since_ms, bars)
        chart_path = render_trade_chart(ohlcv_to_df(candles), base, "LONG" if is_long else "SHORT",
                                          entry_price, state.stop_loss, state.take_profit,
                                          PAPER_LEVERAGE, tf, state.entry_time)
        # Microseconds in the name: two positions on the same symbol can close within the
        # SAME second (seen live — two AVAX shorts 5s apart), and GitHub rejects a second
        # PUT to an identical path with 422, so the later chart silently lost its upload.
        filename = f"{base}_{now:%Y%m%dT%H%M%S%f}.png"
        chart_ref = upload_chart_to_github(chart_path, filename)
        if chart_ref:
            os.remove(chart_path)
        else:
            chart_ref = save_chart_locally(chart_path, filename)
    except Exception as e:
        print(f"[SHEETS] Chart generation failed for {base}: {e}")

    margin   = qty * entry_price / PAPER_LEVERAGE
    notional = qty * entry_price
    # Include profit banked by the mid-trade scale-out leg — the caller's pnl only
    # covers the final close of the remainder, which understated every scaled winner.
    pnl_total = pnl + (state.banked_pnl or 0.0)
    # Log the REAL entry-time stop, not the live one — manage_open_trade's breakeven
    # trail overwrites state.stop_loss in place once a trade reaches +1.25R, so logging
    # the live value made winners that ran to breakeven show a razor-tight "risk" that
    # was never actually taken (verified: 7 real trades showed fake RR of 21-37).
    # entry_stop_loss is a snapshot taken once at fill, never mutated afterward.
    ledger_sl = state.entry_stop_loss if state.entry_stop_loss is not None else state.stop_loss
    log_trade(get_sheet_client(), GOOGLE_SHEET_URL, "Crypto Ledger",
              (state.entry_time or now).astimezone().isoformat(), now.astimezone().isoformat(), base,
              "LONG" if is_long else "SHORT", entry_price, ledger_sl, state.take_profit,
              exit_price, qty, margin, notional, PAPER_LEVERAGE, pnl_total, reason, chart_ref,
              fees=fees, exit_reason=exit_reason)


# ── NVIDIA AI trade confirmation ───────────────────────────────────────────────

# Deadline for the news/FinBERT context fetch inside get_ai_confirmation. Kept well
# under the 5m loop so a wedged HuggingFace download can never stall a cycle.
# ── AI model resolution (fixed 2026-09-15) ────────────────────────────────────────
# The model id was hardcoded to `meta/llama-3.3-70b-instruct`, which NVIDIA
# decommissioned. get_ai_confirmation fails OPEN, so for weeks every trade logged
# "🤖 AI Bot Approval: ✅ YES" from an exception handler — a verdict no model ever gave.
#
# A hardcoded id will rot again, and the /v1/models listing CANNOT be trusted to prevent
# it: probed on 2026-09-15 that endpoint returned 81 models while only 4 of 14 tried were
# actually callable (the rest 404/410/503). So resolve by PROBING, not by listing.
# AI model resolution lives in bot/ai_model.py so BOTH bots share one definition —
# the hardcoded `meta/llama-3.3-70b-instruct` was duplicated here and in bot/strategy.py,
# and NVIDIA decommissioned it under both. See that module for why probing beats listing.
def resolve_ai_model(timeout=20):
    from bot import ai_model as _ai
    return _ai.resolve(NVIDIA_API_KEY, timeout=timeout)


NEWS_CONTEXT_TIMEOUT_SECS = 20
MIN_AI_RR = 2.0   # hard floor — 1:2 minimum. Was 3.0, but ledger data showed ZERO trades
                  # ever reached a 3R target before scale-out/trail/stale-exit clipped them;
                  # 2R is actually hittable on 5m setups inside the 6h window.
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
        return True, MIN_AI_RR, f"no_nvidia_key — proceeding at minimum 1:{MIN_AI_RR:g} R:R"

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

    # News CONTEXT for the model (policy: context only — never a hardcoded gate). Runs
    # only here, at an actual entry decision, not on every 5-min cycle. Fail-soft: no
    # key / no network / stale-only news all render as "no fresh news" and the setup is
    # judged on structure exactly as before. FinBERT is passed in rather than imported
    # at module scope so its heavyweight transformers load stays lazy.
    # HARD TIMEOUT (added 2026-08-20 after a real 5-hour freeze). FinBERT's first call
    # runs transformers' from_pretrained, which downloads ProsusAI/finbert from
    # HuggingFace with NO timeout of its own. With nothing cached that download hung and
    # froze the ENTIRE trading loop mid-entry for 5+ hours — while XRP and ADA sat open
    # with no stop/target management. The try/except below cannot catch a hang; a hang
    # is not an exception. Only a timeout can, so the whole block runs in a worker with
    # a deadline. News is context-only, so losing it must never cost a trade — and must
    # never cost the loop either.
    _news_base = symbol.split('/')[0]

    def _fetch_news_context():
        from bot.news import get_news_context
        global estimate_sentiment
        if estimate_sentiment is None:
            try:
                from finbert_utils import estimate_sentiment as _es
                estimate_sentiment = _es
            except Exception:
                pass
        return get_news_context(
            symbol, ALPACA_KEY_FOR_NEWS, ALPACA_SECRET_FOR_NEWS,
            sentiment_fn=estimate_sentiment)

    import concurrent.futures   # imported here too: the module-level code path
                               # imports it further down, AFTER this block runs.
    _news_exec = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    _news_fut = _news_exec.submit(_fetch_news_context)
    try:
        news_block, _news_heads, _news_sent, _news_prob = _news_fut.result(
            timeout=NEWS_CONTEXT_TIMEOUT_SECS)
        _news_exec.shutdown(wait=False)
        if _news_heads:
            print(f"[{_news_base}] 📰 {len(_news_heads)} fresh headline(s)"
                  + (f" — FinBERT {_news_sent.upper()} ({_news_prob*100:.0f}%)"
                     if _news_sent else "") + " → passed to AI as context")
    except concurrent.futures.TimeoutError:
        # shutdown(wait=False): never join the stuck thread, or we inherit its hang.
        _news_exec.shutdown(wait=False)
        news_block = "RECENT NEWS: unavailable (lookup timed out) — judge on structure alone."
        print(f"[{_news_base}] 📰 news/FinBERT timed out ({NEWS_CONTEXT_TIMEOUT_SECS}s) "
              f"— proceeding on technicals", flush=True)
    except Exception as e:
        _news_exec.shutdown(wait=False)
        news_block = "RECENT NEWS: unavailable (news lookup failed) — judge on structure alone."
        print(f"[{_news_base}] 📰 news lookup failed ({e}) — proceeding on technicals")

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

{news_block}

Analyze using the full 4H chart above:
{amd_question}
- Weigh the news above as CONTEXT ONLY: does it support or contradict this {side}?
  Structure decides the trade; news can raise or lower your confidence in it, and
  should never be the sole reason to take or skip a setup. If there is no fresh news,
  say so and judge on structure alone rather than inventing a narrative.

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
            model=(resolve_ai_model() or __import__("bot.ai_model", fromlist=["x"]).configured_model()),
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
        print(f"  ⚠️  AI call timed out (25s) — proceeding on technicals at 1:{MIN_AI_RR:g}", flush=True)
        # AI is a secondary confirmation; the setup already passed every technical
        # filter. A timeout must not cost a valid trade — fall back, don't skip.
        return True, MIN_AI_RR, "AI timeout (25s) — proceeding on technicals"
    except Exception as e:
        _executor.shutdown(wait=False)
        # Fail OPEN, but do not CLAIM an approval. The NVIDIA model id is retired
        # (HTTP 410 Gone), so every trade in the log carries "🤖 AI Bot Approval:
        # ✅ YES" from this handler — a verdict no model ever gave.
        return True, MIN_AI_RR, f"⚠️ AI SKIPPED — unavailable ({e}); technicals only, NOT an approval"


def check_chase_continuation(df_ltf, bias, min_body_abs=0.0):
    """Returns (eligible: bool, reason: str) — used when price has run away from a
    retest zone without ever tapping it.

    The momentum test used to be `close[-1] > close[-3]`: two bars of net drift, which a
    run of SHRINKING green candles satisfies perfectly. That is precisely the pattern the
    operator watched on ASTER 2026-09-05 ("the green candles kept getting smaller"), and
    the chase is the ONE path that enters at market with no zone underneath it — so
    "price is a bit higher than it was" is not enough justification. It now requires the
    move to still be ACCELERATING (see indicators.momentum_expanding)."""
    is_long = bias == "BULLISH"
    last, prev = df_ltf.iloc[-1], df_ltf.iloc[-2]
    candle = indicators.classify_candle(last, prev)
    reversal_vs_long  = {"shooting_star", "gravestone_doji", "bearish_engulfing",
                         "hanging_man", "marubozu_bear"}
    reversal_vs_short = {"hammer", "dragonfly_doji", "bullish_engulfing",
                         "inverted_hammer", "marubozu_bull"}
    reversal_set = reversal_vs_long if is_long else reversal_vs_short
    no_reversal_candle = candle not in reversal_set

    _bars = df_ltf.tail(3)[['open', 'high', 'low', 'close']].to_dict('records')
    momentum_intact = indicators.momentum_expanding(_bars, is_long, min_body_abs)

    if no_reversal_candle and momentum_intact:
        return True, f"candle={candle}, momentum EXPANDING"
    return False, f"candle={candle}, momentum_expanding={momentum_intact}"


def execute_confirmed_entry(symbol, base, state, paper, is_long, price, risk_amt,
                             df_ltf, df_htf, daily_trend, entry_atr, now, risk_fraction,
                             exchange=None):
    """Shared AI-confirmation + position-sizing + execution tail for BOTH the
    retest-entry path and the breakout-chase path. The caller must already have set
    state.stop_loss (zone-edge+ATR-cap+swing-guard for a retest, swing-high/low
    directly for a chase) and state.is_chase before calling."""
    bias_str   = "bullish" if is_long else "bearish"
    side_label = "LONG" if is_long else "SHORT"
    label      = f"🚀 CHASE {side_label}" if state.is_chase else side_label

    # ── PAUSE gate — this is the ONLY place a new position gets opened, so gating it
    # here (rather than earlier in process_symbol) means every engine above still runs
    # and prints normally: accumulation notices, counter-trend BOS skips, zone arming,
    # sweep hunts, AI verdicts. You see exactly what the bot WOULD have done, and it
    # simply doesn't do it. Gating it earlier is what flattened the log to a header and
    # a balance line. Existing positions are managed elsewhere and are unaffected.
    if PAUSE_NEW_ENTRIES:
        print(f"[{base}] ⏸ WOULD ENTER {label} @ ${price:,.4f}  SL ${state.stop_loss:,.4f}  "
              f"(risk ${risk_amt:,.4f}/unit) — blocked by PAUSE_NEW_ENTRIES, no order placed.")
        return

    # 2. Find the nearest 4H liquidity pool AT LEAST MIN_AI_RR away — the structural
    #    target (see below). Searching from `price` instead of the MIN_AI_RR floor was
    #    a bug: find_next_liquidity_target always returns the NEAREST qualifying swing
    #    point, which on crypto's noisy 4H structure is almost always closer than 2R —
    #    so structural_take_profit rejected it every time and fell back to the exact
    #    2.0R floor (confirmed live: 93% of trades landed on exactly 1:2 R:R). Searching
    #    from the floor forward finds the nearest REAL pool beyond it instead of giving
    #    up (verified: drops the floor-collapse rate to ~20%).
    _min_tp_lvl = price + MIN_AI_RR * risk_amt if is_long else price - MIN_AI_RR * risk_amt
    pool_tp = indicators.find_next_liquidity_target(df_htf, _min_tp_lvl, bias_str)

    # 3. Ask NVIDIA AI — pass the TRADE direction (state.bias), not the current BOS
    #    direction. For trend-follow setups with no sweep, pass the zone edge as the
    #    structural reference level instead of $0. A chase entry has no structural
    #    zone to reference — pass the risk band (SL to current price) instead, and
    #    a distinct amd_phase so the prompt frames it honestly as a momentum-chase.
    if state.is_chase:
        amd_phase           = 'breakout_chase'
        zone_type           = state.amd_zone_type
        ref_level           = state.zone_set_price
        zone_lo, zone_hi    = min(state.stop_loss, price), max(state.stop_loss, price)
    else:
        amd_phase        = state.amd_phase
        zone_type        = state.amd_zone_type
        ref_level        = state.sweep_low or (state.fvg_high if not is_long else state.fvg_low)
        zone_lo, zone_hi = state.fvg_low, state.fvg_high

    # AI still has final say on GO/NO-GO, but no longer picks the target — its R:R
    # suggestion (_ai_rr) is passed through only as context/logging. The actual target
    # is now pinned to the real 4H pool below (Lever #3, ported from the stock bot's
    # structural-target fix: a multiple of risk isn't "structure" even when an AI
    # picks the multiple — the pool price is).
    confirm, _ai_rr, ai_reason = get_ai_confirmation(
        symbol, price, daily_trend, bias_str,
        zone_lo, zone_hi, ref_level,
        state.stop_loss, risk_amt, pool_tp, df_ltf, df_htf,
        amd_phase=amd_phase, zone_type=zone_type,
    )
    # Structural target: pin TP to the nearest 4H liquidity pool offering ≥MIN_AI_RR,
    # capped at MAX_AI_RR — real structure, not a multiple of risk. rr_actual now means
    # the REAL R:R at the placed TP (used below for sizing/logging, so both stay honest).
    # ── Entry-zone invariant (added 2026-09-05) ──────────────────────────────────
    # Last line of defence before an order: the fill must be inside the zone this setup
    # armed. price_in_entry_zone already checks this at TAP time, but `price` is not
    # re-read afterwards and the AI confirmation between the two can take 25 seconds.
    # REAL INCIDENT: ASTER/USD LONG armed demand at 0.7190-0.7588 and filled at 0.7906 —
    # 4.19% above the top of its own zone, on a price that never traded down to it. Which
    # path did that is still unknown (the bot was logging to a terminal, so nothing
    # recorded the decision); this guard holds regardless of which one it was, and says
    # loudly enough to identify it next time.
    _zone_ok, _zone_why = indicators.entry_respects_zone(
        price, state.fvg_low, state.fvg_high, is_long, state.is_chase)
    if not _zone_ok:
        print(f"[{base}] 🚫 Entry REFUSED — {_zone_why}. "
              f"(zone_type={state.amd_zone_type}, phase={state.amd_phase}, "
              f"bars_in_wait={state.bars_in_entry_wait}) Resetting to IDLE.")
        state.reset()
        return
    tp_planned = indicators.structural_take_profit(
        price, risk_amt, pool_tp, is_long, MIN_AI_RR, MAX_AI_RR)
    # ── Target reachability (added 2026-09-04) ───────────────────────────────────
    # structural_take_profit pins TP to a REAL 4H pool, which is right — but says nothing
    # about whether that pool can be reached before STALE_TRADE_HOURS force-closes the
    # position. POL/USD was handed a target 13.3% away against a 2.755% 6H ATR: roughly
    # 4.8 average HTF candles of travel inside the span of one. It reported a gorgeous
    # 1:9.4 and could only ever exit on the timer — which is precisely what the POL trade
    # before it did, on the identical 0.10721 target, for -$12.08.
    # Clamp rather than veto: the SETUP may be fine, it is the target that was fantasy.
    _htf_atr = indicators.range_atr(df_htf)
    _stop_ref = price - risk_amt if is_long else price + risk_amt
    tp_planned, _rr_reach, _reach_ok = indicators.reachable_target(
        price, _stop_ref, tp_planned, _htf_atr, MAX_TARGET_ATR_MULT, MIN_AI_RR)
    if not _reach_ok:
        print(f"[{base}] 🚫 Reachability gate — nearest structure is "
              f"{abs(tp_planned - price)/price*100:.2f}% away; only "
              f"{MAX_TARGET_ATR_MULT:g}× the HTF ATR ({_htf_atr/price*100:.2f}%) is "
              f"reachable inside {STALE_TRADE_HOURS}h, which pays 1:{_rr_reach:.1f} "
              f"< 1:{MIN_AI_RR:g}. Skipping.")
        state.reset()
        return
    reward    = abs(tp_planned - price)
    rr_actual = reward / risk_amt if risk_amt else 0.0
    # Target OFFSET — pull the TP a fraction of ATR inward so we fill BEFORE the
    # herd's orders pile up at the round number / structural ceiling.
    # Hard cap: offset can never reduce effective R:R below MIN_AI_RR floor.
    min_reward  = risk_amt * MIN_AI_RR
    max_offset  = max(0.0, reward - min_reward)
    tp_offset   = min(0.05 * entry_atr, 0.25 * reward, max_offset)
    state.take_profit = (tp_planned - tp_offset if is_long else tp_planned + tp_offset)
    # `confirm` is True even when the AI never answered — get_ai_confirmation fails
    # OPEN. Printing ✅ YES there asserts a verdict no model gave. Fixing the reason
    # string alone was not enough: this icon is what the eye actually reads.
    _ai_dead = "AI SKIPPED" in (ai_reason or "") or "unavailable" in (ai_reason or "")
    icon    = ("⚠️ SKIPPED" if _ai_dead else "✅ YES") if confirm else "❌ NO"
    _tp_src = "@4H pool" if (pool_tp and abs(tp_planned - pool_tp) < 1e-9) else f"{MAX_AI_RR:.0f}R cap"
    print(f"[{base}] 🤖 AI Bot Approval: {icon}  Structural R:R=1:{rr_actual:.1f} [{_tp_src}] "
          f"(AI suggested 1:{_ai_rr:.1f})  {ai_reason[:140]}")

    # ── Fee-clearance gate (added 2026-08-22) ────────────────────────────────────
    # R:R says nothing about fees. A 1:2 setup whose target sits 0.3% away cannot survive
    # a 0.5% round trip no matter how good the structure is — it is a guaranteed loser the
    # moment costs exist. Measured over 121 real trades, the strategy's break-even fee rate
    # was 0.245%/side, i.e. it sat exactly ON the fee line. This rejects setups whose target
    # does not clear the round trip by a healthy multiple, BEFORE any order is placed.
    tp_move_pct = abs(state.take_profit - price) / price if price else 0.0
    if tp_move_pct < ROUND_TRIP_COST * MIN_FEE_CLEARANCE:
        print(f"[{base}] 🚫 Fee gate — target is only {tp_move_pct*100:.2f}% away; needs "
              f"≥{ROUND_TRIP_COST * MIN_FEE_CLEARANCE * 100:.2f}% to clear a "
              f"{ROUND_TRIP_COST*100:.2f}% round trip. Skipping.")
        state.reset()
        return

    # 4. Execute only if AI confirms
    if confirm:
        effective_fraction = risk_fraction * 0.5 if (state.ranging_mode or state.is_chase) else risk_fraction
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
            return
        margin_to_deploy = min(margin_to_deploy, daily_remaining)
        qty = math.floor(margin_to_deploy * PAPER_LEVERAGE / price * 1e6) / 1e6

        # Hard risk cap. Sizing above is MARGIN-based, so real dollars at risk ride on
        # however wide the structural stop happens to be — a wide stop silently scales
        # the loss up. REAL INCIDENT 2026-08-11: an AVAX SHORT with a 3.9% stop put $79
        # at risk. The 10-second sniper path already sized by fixed risk
        # (MAX_RISK_DOLLARS); this brings the main path in line so both agree.
        _qty_risk_capped = indicators.cap_qty_for_risk(qty, risk_amt, paper.risk_budget)
        if _qty_risk_capped < qty:
            print(f"[{base}] 🛡 Risk cap — qty {qty:.6f}→{_qty_risk_capped:.6f} "
                  f"(stop ${risk_amt:,.4f} away would have risked "
                  f"${qty * risk_amt:,.2f} > ${paper.risk_budget:,.2f} budget)")
        qty = math.floor(_qty_risk_capped * 1e6) / 1e6

        # Venue minimums. A risk-sized order on a small account can land under what the
        # exchange will accept — 0.2% of $100 is $0.20 of risk, which on a 2% stop is a
        # ~$10 position, below the minimum notional on plenty of pairs. REFUSE rather
        # than round up: rounding up breaks the risk cap that produced the number, and a
        # $20-risk order silently inflated to $50 is not the trade that was approved.
        # exchange is None in the backtest harness, where there is no venue to ask —
        # (None, None) means 'no constraint known' and the check passes through.
        _min_amt, _min_cost = (exchange_minimums(exchange, symbol)
                               if exchange is not None else (None, None))
        _size_ok, _size_why = indicators.meets_exchange_minimums(qty, price, _min_amt, _min_cost)
        if not _size_ok:
            print(f"[{base}] 🚫 Below venue minimum — {_size_why}. "
                  f"(risk budget ${paper.risk_budget:,.2f}, stop ${risk_amt:,.4f} away)")
            state.reset()
            return

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

        # Minimum-reward gate — scales with account size so tight-stop majors
        # (BTC/ETH) aren't filtered out just for having a small $ risk_amt.
        MIN_REWARD_PCT = 0.0015   # 0.15% of balance
        min_reward_dollars = paper.balance * MIN_REWARD_PCT
        actual_reward = qty * (risk_amt * rr_actual) if qty > 0 else 0
        if actual_reward < min_reward_dollars:
            print(f"[{base}] ⏭ Trade skipped — reward too small after position cap "
                  f"(${actual_reward:.2f} < ${min_reward_dollars:.2f} min). "
                  f"Cheap coin + tight SL = deploy capital elsewhere.")
            return

        if qty > 0:
            # Final guard: never stack onto an existing position. The paper trader
            # is synchronous so this is belt-and-suspenders, but it keeps both bots
            # consistent and covers any state/position desync.
            if abs(paper.get_position(symbol)) > 1e-6:
                print(f"[{base}] ⛔ Entry aborted — position already open. Syncing to POSITION_OPEN.")
                state.state = "POSITION_OPEN"
                return

            trade = paper.buy(symbol, qty, price) if is_long else paper.sell(symbol, qty, price)

            if trade:
                paper.record_margin(margin_to_deploy)   # count against daily 5% cap
                state.state       = "POSITION_OPEN"
                state.entry_price = price
                state.entry_stop_loss = state.stop_loss  # snapshot BEFORE any breakeven trail
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
        # type(e).__name__ matters more than the message here: ccxt stringifies a
        # NETWORK failure as bare "coinbase GET <url>" with no status, so str(e)
        # alone cannot distinguish RequestTimeout from DDoSProtection (rate limit)
        # from ExchangeNotAvailable (outage) — three problems with three fixes.
        _open = abs(paper.get_position(symbol)) > 1e-9 if paper else False
        print(f"[{base}] Data error (after retries) [{type(e).__name__}]: {e}"
              + ("  ⚠️ POSITION OPEN — SL/TP not evaluated this cycle; the 10s "
                 "watcher is the only cover until the next one." if _open else ""))
        return None

    # Zone derivation uses CLOSED candles only. A zone edge taken from the newest,
    # still-forming HTF bar keeps moving as that bar develops, while the armed zone is
    # a snapshot that never refreshes. REAL INCIDENT 2026-08-11: AVAX armed a zone whose
    # low was $6.24; by entry the same finder on the same data returned $6.326, because
    # that bar had since printed a higher high. Price sat 1.5% BELOW the live zone and
    # the fill only happened against the stale snapshot. Closed bars keep the zone stable
    # between recomputes, so what was armed still means what it meant.
    df_htf_closed = indicators.drop_forming_candle(df_htf)

    price = float(df_ltf["close"].iloc[-1])
    held  = paper.get_position(symbol)
    has_position = abs(held) > 1e-6

    # (PAUSE_NEW_ENTRIES is enforced further down, AFTER the per-symbol status line is
    # printed and after open positions are managed — see the guard above the IDLE block.
    # It used to return right here, which silently gutted the log: every symbol's
    # analysis line vanished and the output collapsed to just a header and a balance.
    # A pause should stop the bot TRADING, not stop it TALKING.)

    # 1H ATR — the noise floor for structural stops. Entries stay on the 5m chart;
    # this only stops the STOP from being tighter than a real 1H candle's normal range.
    atr_1h = (float((df_htf_1h['high'] - df_htf_1h['low']).rolling(14).mean().iloc[-1])
              if df_htf_1h is not None and len(df_htf_1h) >= 15 else None)

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

    # Same size discipline as the displacement path. Without it this FALLBACK absorbs
    # everything the strict branch now rejects — which is exactly what happened after
    # 2026-09-04: 6 of 6 setups armed here, on zones with no displacement behind them.
    _g_ltf = indicators.displacement_gates(df_ltf, DISPLACEMENT_ATR_MULT,
                                           DISPLACEMENT_MIN_PCT, MIN_FVG_PCT)
    _g_htf = indicators.displacement_gates(df_htf, DISPLACEMENT_ATR_MULT,
                                           DISPLACEMENT_MIN_PCT, MIN_FVG_PCT)
    is_fvg_bull_ltf, fvg_bot_ltf, fvg_top_ltf = indicators.find_bullish_fvg(df_ltf, **_g_ltf)
    is_fvg_bear_ltf, fvg_bear_bot_ltf, fvg_bear_top_ltf = indicators.find_bearish_fvg(df_ltf, **_g_ltf)
    is_fvg_bull_htf, fvg_bot_htf, fvg_top_htf = indicators.find_bullish_fvg(df_htf, **_g_htf)
    is_fvg_bear_htf, fvg_bear_bot_htf, fvg_bear_top_htf = indicators.find_bearish_fvg(df_htf, **_g_htf)

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

    # ── SMC context markings for the chart overlay ────────────────────────────────
    # DISPLAY ONLY. Everything here is already computed elsewhere for real decisions;
    # this block just exposes it so the live chart can draw what the bot is "seeing".
    # Wrapped so a detector hiccup can never interrupt trading.
    try:
        # BOS: the HTF break level + direction (4H preferred, else 1H — matches is_bos above)
        _bos_src = df_htf if is_bos_4h else df_htf_1h
        state.bos_dir = direction if is_bos else None
        state.bos_level = (float(_bos_src['high'].tail(15).max()) if direction == 'bullish'
                           else float(_bos_src['low'].tail(15).min()) if direction == 'bearish'
                           else None) if is_bos else None

        # CHoCH on the 15m — the LTF structure shift
        _ch, _ch_dir = indicators.detect_choch(df_ltf_15, lookback=10)
        state.choch_dir = _ch_dir if _ch else None
        state.choch_level = (float(df_ltf_15['high'].tail(10).max()) if _ch_dir == 'bullish'
                             else float(df_ltf_15['low'].tail(10).min()) if _ch_dir == 'bearish'
                             else None) if _ch else None

        # Displacement candle range (the momentum bar that left the FVG)
        _dsp_found, _dsp_dir, _dsp_lo, _dsp_hi, _ = indicators.detect_displacement_fvg(
            df_ltf, **indicators.displacement_gates(df_ltf, DISPLACEMENT_ATR_MULT,
                                                 DISPLACEMENT_MIN_PCT, MIN_FVG_PCT))
        state.disp_low  = float(_dsp_lo) if _dsp_found else None
        state.disp_high = float(_dsp_hi) if _dsp_found else None

        # Fib OTE band off the recent 5m swing range
        _sw_lo = float(df_ltf['low'].tail(60).min())
        _sw_hi = float(df_ltf['high'].tail(60).max())
        if _sw_hi > _sw_lo:
            _, _ote = indicators.calculate_fib_levels(_sw_lo, _sw_hi)
            state.ote_low, state.ote_high = float(_ote['lower']), float(_ote['upper'])
        else:
            state.ote_low = state.ote_high = None

        # Inducement — only meaningful while a zone is armed and waiting for its tap
        state.inducement = (indicators.detect_inducement(
            df_ltf.tail(40)[['open', 'high', 'low', 'close']].values.tolist(),
            state.fvg_low, state.fvg_high, price, state.bias == "BULLISH")
            if state.state == "ENTRY_WAIT" and state.fvg_low and state.fvg_high else None)
    except Exception as _e:
        print(f"[{base}] (chart overlay calc skipped: {_e})")

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
            # A take-profit rests on the book and is hit by someone crossing to it -> MAKER.
            # A stop triggers and crosses the book itself, and so does a timeout's market
            # close -> TAKER. Charging one blended rate would understate every loser.
            _exit_liq = "maker" if indicators.normalize_exit_reason(
                reason, state.breakeven_moved) == "TARGET" else "taker"
            if is_long:
                paper.sell(symbol, qty_now, price, _exit_liq)
                pnl = (price - state.entry_price) * qty_now
            else:
                paper.buy(symbol, qty_now, price, _exit_liq)
                pnl = (state.entry_price - price) * qty_now
            # NET, not gross (fixed 2026-09-04). _charge_fee already moved `balance` by
            # net on all four legs, but the number reported here went to the ledger, the
            # journal tiles and the desktop alert GROSS — so the sheet and the account
            # disagreed on every row. At 10x a $1,360 notional round trip costs $6.80
            # against trades whose edge is a few dollars: POL logged +$3.16 and was
            # really -$3.64. Two of four rows on screen were green and losing.
            _fees = indicators.round_trip_fee(
                state.entry_price, price, qty_now,
                MAKER_FEE_RATE if MAKER_ENTRIES else TAKER_FEE_RATE,
                MAKER_FEE_RATE if _exit_liq == "maker" else TAKER_FEE_RATE)
            pnl -= _fees
            # breakeven_moved is what separates a trailed-to-flat exit from a real
            # stop-out -- both arrive here as "SL hit".
            _exit_reason = indicators.normalize_exit_reason(reason, state.breakeven_moved)
            direction_label = "LONG" if is_long else "SHORT"
            lev_tag = f"[{PAPER_LEVERAGE}x]" if PAPER_LEVERAGE > 1 else ""
            won = pnl >= 0
            # Label the alert by CAUSE, not by outcome sign. This said "🟢 TP" for any
            # close that happened to end green — including a 6h STALE timeout, which is a
            # completely different event. It made the log actively misleading: counting
            # alerts gave 13 "targets" when only 4 real TP HITs existed, so any analysis
            # built on them (including a diagnosis run on 2026-09-14) started out wrong.
            _icon = {"TARGET": "🟢 TP", "STOP": "🔴 SL", "BREAKEVEN": "🛡 BREAK-EVEN",
                     "STALE": "⏰ STALE"}.get(_exit_reason, "⏹ CLOSED")
            trade_print(f"{base} {direction_label}", f"{reason}",
                        price, pnl=pnl, balance=paper.balance,
                        extra=lev_tag)
            alert(f"{_icon} — {base} {direction_label} closed",
                  f"P&L ${pnl:+.2f}  Balance ${paper.balance:,.2f}",
                  sound="Glass" if won else "Basso",
                  speak=f"{base} closed. {'Profit' if won else 'Loss'} {abs(pnl):.0f} dollars")
            _log_trade_close_to_sheet(base, is_long, state.entry_price, price, qty_now, pnl, state, exchange,
                                      fees=_fees, exit_reason=_exit_reason)
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
    # ── Chop measurements: computed for EVERY state, not only IDLE (fixed 2026-09-14) ──
    # These used to live inside the `state == "IDLE"` block below, so once a symbol left
    # IDLE it was never re-checked for chop again — it rode a stale directional read into
    # a flat tape and took the retest late. That is the "bot only enters after the big
    # move" complaint: POL sat in SWEEP_HUNT for ~34h and BTC for ~62h, and the
    # SWEEP_HUNT→ENTRY_WAIT arming block had neither an ATR gate nor an accumulation check
    # of its own.
    rng     = df_htf['high'] - df_htf['low']
    atr_14  = rng.rolling(14).mean().iloc[-1]
    atr_3   = rng.rolling(3).mean().iloc[-1]
    # max(14-bar, recent 3-bar) so a FRESH impulse the slow average hasn't caught up to
    # yet isn't mislabeled "consolidating" and skipped.
    atr_pct = max(atr_14, atr_3) / price
    atr_min = atr_gate_for(symbol)
    amd_phase_now, amd_info_now = indicators.detect_amd_phase(df_htf)

    if state.state == "IDLE" and not has_position:
        # ── Accumulation guard runs FIRST, gating every path below ────────────────
        # A tight HTF range = chop / institutions still accumulating. Don't trade
        # EITHER side until it breaks. Without this first, a 15-bar "BOS" printed
        # inside a 25-bar range whipsaws us. Log the range and bail for this cycle.
        if amd_phase_now == 'accumulation':
            print(f"[{base}] 📦 Accumulation: range ${amd_info_now['range_low']:,.2f}–"
                  f"${amd_info_now['range_high']:,.2f} ({amd_info_now['range_pct']:.1%} wide) "
                  f"— chop, no entry until breakout/sweep")
            return price

        # ══ AMD MANIPULATION — PRIORITY ENGINE (evaluated FIRST) ═══════════════════
        # The core Debbie-La / Wyckoff setup: a 4H liquidity SWEEP (stop-hunt) with the
        # daily trend aligned = manipulation → reversal. This runs BEFORE the generic
        # BOS/wedge continuation engines so a valid institutional sweep can never be "cut
        # in line" and lose the trade to a plain BOS. When NO valid AMD setup exists (no
        # sweep / too shallow / no zone / dead market) the code FALLS THROUGH to the
        # generic engines below — those are gated on `state.state == "IDLE"`, so a valid
        # AMD setup (which moves state → ENTRY_WAIT) automatically suppresses them.
        #   low-sweep  + bearish daily → manipulation_up   → SHORT from supply above
        #   high-sweep + bullish daily → manipulation_down → LONG  from demand below
        if is_sweep_htf and daily_trend == 'bearish' and atr_pct >= atr_min:
            # sweep wick must pierce support by ≥0.3% (real stop-hunt, not noise)
            sweep_depth = ((htf_support_level - sweep_wick_htf) / htf_support_level
                           if htf_support_level and htf_support_level > 0 else 0)
            if sweep_depth < 0.003:
                print(f"[{base}] AMD low-sweep too shallow ({sweep_depth:.2%} < 0.3%) — trying generic setups")
            else:
                found, sup_lo, sup_hi, sup_type = indicators.find_supply_zone(df_htf_closed, price)
                if found:
                    pool_note = ""
                    if eql_h_found and eql_h_level and abs(sweep_wick_htf - eql_h_level) / eql_h_level <= 0.005:
                        pool_note = f"  📍 swept EQL pool ${eql_h_level:,.2f}"
                    state.bias               = "BEARISH"
                    state.sweep_low          = sweep_wick_htf
                    state.amd_phase          = 'manipulation_up'
                    state.amd_zone_type      = sup_type
                    state.ranging_mode       = False
                    state.state              = "ENTRY_WAIT"
                    state.arm_zone(sup_lo, sup_hi)
                    print(f"[{base}] 🎯 AMD (PRIORITY): 4H low-sweep ${sweep_wick_htf:,.4f} + daily BEARISH → "
                          f"[{sup_type}] ${sup_lo:,.2f}–${sup_hi:,.2f} → waiting for SHORT entry{pool_note}")
                else:
                    print(f"[{base}] AMD sweep valid but no supply zone above ${price:,.2f} — trying generic setups")
        elif is_sweep_high_htf and daily_trend == 'bullish' and atr_pct >= atr_min:
            sweep_depth = ((sweep_high_wick_htf - htf_resistance_level) / htf_resistance_level
                           if htf_resistance_level and htf_resistance_level > 0 else 0)
            if sweep_depth < 0.003:
                print(f"[{base}] AMD high-sweep too shallow ({sweep_depth:.2%} < 0.3%) — trying generic setups")
            else:
                found, dem_lo, dem_hi, dem_type = indicators.find_demand_zone(df_htf_closed, price)
                if found:
                    pool_note = ""
                    if eqh_h_found and eqh_h_level and abs(sweep_high_wick_htf - eqh_h_level) / eqh_h_level <= 0.005:
                        pool_note = f"  📍 swept EQH pool ${eqh_h_level:,.2f}"
                    state.bias               = "BULLISH"
                    state.sweep_low          = sweep_high_wick_htf
                    state.amd_phase          = 'manipulation_down'
                    state.amd_zone_type      = dem_type
                    state.ranging_mode       = False
                    state.state              = "ENTRY_WAIT"
                    state.arm_zone(dem_lo, dem_hi)
                    print(f"[{base}] 🎯 AMD (PRIORITY): 4H high-sweep ${sweep_high_wick_htf:,.4f} + daily BULLISH → "
                          f"[{dem_type}] ${dem_lo:,.2f}–${dem_hi:,.2f} → waiting for LONG entry{pool_note}")
                else:
                    print(f"[{base}] AMD high-sweep valid but no demand zone below ${price:,.2f} — trying generic setups")

        # ── GENERIC BOS continuation — ONLY if no AMD setup took the trade ────────
        # A valid AMD setup above moves state → ENTRY_WAIT, so the `state.state == "IDLE"`
        # gate on every branch here makes BOS defer to it. When there's no AMD sweep this
        # is the workhorse: a BOS leaving a displacement FVG → lock it and retest; else
        # hunt the sweep. (bos_aligned/bos_counter computed here — only used in this block.)
        bos_aligned = is_bos and direction and (daily_trend == direction or daily_trend is None)
        bos_counter = is_bos and direction and daily_trend and daily_trend != direction

        if state.state == "IDLE" and bos_aligned and atr_pct < atr_min:
            print(f"[{base}] BOS skip — consolidating (4H ATR={atr_pct:.1%} < {atr_min:.1%})")
        elif state.state == "IDLE" and bos_aligned:
            state.bias         = direction.upper()
            state.ranging_mode = (daily_trend is None) or is_1h_only
            size_tag = "HALF" if state.ranging_mode else "FULL"
            disp_found, disp_dir, disp_lo, disp_hi, _ = indicators.detect_displacement_fvg(
                df_htf_closed, **indicators.displacement_gates(df_htf_closed, DISPLACEMENT_ATR_MULT,
                                                          DISPLACEMENT_MIN_PCT, MIN_FVG_PCT))
            if disp_found and disp_dir == direction:
                state.amd_zone_type      = "choch_fvg"     # displacement gap = the CHoCH zone
                state.state              = "ENTRY_WAIT"
                state.arm_zone(disp_lo, disp_hi)
                print(f"[{base}] STEP 1+2: {bos_tf} BOS ({direction}) + displacement FVG "
                      f"${disp_lo:,.4f}–${disp_hi:,.4f} → ENTRY_WAIT retest [{size_tag} size]")
            else:
                state.state          = "SWEEP_HUNT"
                state.sweep_hunt_bar = 0
                print(f"[{base}] STEP 1: {bos_tf} BOS ({direction}) → hunting sweep [{size_tag} size]")
        elif state.state == "IDLE" and bos_counter:
            print(f"[{base}] {bos_tf} BOS {direction} blocked — daily trend is {daily_trend} (counter-trend skip)")

        # ── Trend-zone fallback: no fresh BOS/sweep but daily trend is clear ──────
        # Find the nearest supply/demand zone and wait for price to pull back into it.
        # Lower conviction than AMD/BOS → half position size (ranging_mode=True).
        # GATED OFF (ENABLE_TREND_FOLLOW) in pure-sniper mode — this "buy the discount"
        # engine (no sweep, no BOS) was producing the consolidation dip-buys.
        if ENABLE_TREND_FOLLOW and state.state == "IDLE" and atr_pct >= atr_min:
            # Accumulation already filtered at the top of IDLE (returns early), so any
            # symbol reaching here is NOT in a tight range — safe to seek a trend zone.
            if daily_trend == "bearish":
                found, sup_lo, sup_hi, sup_type = indicators.find_supply_zone(
                    df_htf_closed, price, max_distance_pct=0.08
                )
                if found:
                    state.bias               = "BEARISH"
                    state.amd_phase          = "trend_follow"
                    state.amd_zone_type      = sup_type
                    state.ranging_mode       = True   # half size — no fresh BOS/sweep
                    state.state              = "ENTRY_WAIT"
                    state.arm_zone(sup_lo, sup_hi)
                    state.zone_set_price     = price
                    print(f"[{base}] 📊 Trend zone: daily BEARISH → [{sup_type}] "
                          f"${sup_lo:,.2f}–${sup_hi:,.2f} → SHORT on rally (½ size)")
            elif daily_trend == "bullish":
                found, dem_lo, dem_hi, dem_type = indicators.find_demand_zone(
                    df_htf_closed, price, max_distance_pct=0.08
                )
                if found:
                    state.bias               = "BULLISH"
                    state.amd_phase          = "trend_follow"
                    state.amd_zone_type      = dem_type
                    state.ranging_mode       = True
                    state.state              = "ENTRY_WAIT"
                    state.arm_zone(dem_lo, dem_hi)
                    state.zone_set_price     = price
                    print(f"[{base}] 📊 Trend zone: daily BULLISH → [{dem_type}] "
                          f"${dem_lo:,.2f}–${dem_hi:,.2f} → LONG on pullback (½ size)")

    # ── SWEEP_HUNT: require a liquidity sweep before FVG entry ────────────────
    # Crypto sweeps on 5m can take up to 4h to develop — allow 48 bars patience.
    # After that the BOS signal is stale and we reset to IDLE.
    SWEEP_PATIENCE = 48
    if state.state == "SWEEP_HUNT" and not has_position:
        state.sweep_hunt_bar += 1

        # Re-check chop while hunting, not only on the way in. A BOS gets a symbol into
        # SWEEP_HUNT and the bias then sticks; without this the market can collapse into
        # accumulation underneath a days-old directional read and the bot still arms a
        # zone off it. Bail for this cycle rather than resetting — the sweep may still
        # come, and sweep_hunt_expired() above owns the age question.
        if amd_phase_now == 'accumulation':
            if state.sweep_hunt_bar % 12 == 1:      # ~hourly, not every 5-min tick
                print(f"[{base}] 📦 Still hunting but HTF is accumulating "
                      f"(${amd_info_now['range_low']:,.4f}–${amd_info_now['range_high']:,.4f}, "
                      f"{amd_info_now['range_pct']:.1%} wide) — not arming into chop.")
            return price
        if atr_pct < atr_min:
            if state.sweep_hunt_bar % 12 == 1:
                print(f"[{base}] ⏸ Still hunting but HTF ATR {atr_pct:.2%} < "
                      f"{atr_min:.2%} floor — market too dead to arm a zone.")
            return price

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

        # `and not state.sweep_low` made this unreachable: sweep_low is set on the FIRST
        # sweep and only cleared by reset(), so a single sweep disabled the 4h limit
        # forever. BTC then held SWEEP_HUNT for 373 cycles (~62h50m) on a 1H BOS from three
        # days earlier, surviving a restart because sweep_hunt_bar is persisted; POL's was
        # ~34h. A directional read from three days ago is not a read on now.
        _expired, _why = indicators.sweep_hunt_expired(
            state.sweep_hunt_bar, SWEEP_PATIENCE, bool(state.sweep_low))
        if _expired:
            print(f"[{base}] 💀 {_why}. Resetting to IDLE.")
            state.reset()
            return price

        # Only advance to ENTRY_WAIT once sweep is confirmed
        if not state.sweep_low:
            return price

        # ...and once that sweep is still anywhere near the market. sweep_hunt_expired()
        # above caps how LONG a hunt may run; nothing capped how FAR the liquidity was.
        # On 2026-09-17 all three bad entries armed off sweeps nowhere near price — AVAX
        # swept $7.17 and armed a zone at $7.61, 5.7% away, with SEI and ADA the same.
        # Drop the stale level and keep hunting rather than resetting: the BOS read may
        # still be good, it is only this particular grab that has gone out of date.
        _reach_ok, _reach_why = indicators.sweep_within_reach(
            state.sweep_low, price, atr_pct * price,
            SWEEP_MAX_ATR_MULT, SWEEP_MAX_PCT)
        if not _reach_ok:
            print(f"[{base}] 🥀 {_reach_why} — dropping it and hunting a fresh sweep.")
            state.sweep_low = None
            return price

        # Preferred: the FVG left by the post-sweep displacement that broke structure.
        # That gap IS the CHoCH — we trade its retest. Falls back to a generic FVG/OB
        # only if no clean displacement gap exists yet.
        want = "bullish" if state.bias == "BULLISH" else "bearish"
        disp_found, disp_dir, disp_lo, disp_hi, _ = indicators.detect_displacement_fvg(
            df_ltf, **indicators.displacement_gates(df_ltf, DISPLACEMENT_ATR_MULT,
                                                 DISPLACEMENT_MIN_PCT, MIN_FVG_PCT))

        if disp_found and disp_dir == want:
            state.amd_zone_type = "choch_fvg"      # mark: FVG == CHoCH → retest-rebounce entry
            state.state         = "ENTRY_WAIT"
            state.arm_zone(disp_lo, disp_hi)
            print(f"[{base}] STEP 2: 🎯 CHoCH FVG (displacement) locked  "
                  f"${state.fvg_low:,.4f}-${state.fvg_high:,.4f}  "
                  f"sweep=${state.sweep_low:,.4f} — waiting for retest/refill")

        elif state.bias == "BULLISH" and is_fvg_bull:
            # A generic gap, NOT the displacement that broke structure — say so.
            # Leaving this unset left amd_zone_type None, and None took the one
            # branch of sniper_entry_allowed() that never checked displacement.
            state.amd_zone_type = "bullish_fvg"
            state.state    = "ENTRY_WAIT"
            state.arm_zone(fvg_bot, fvg_top)
            print(f"[{base}] STEP 2: Bullish OB/FVG locked  "
                  f"${state.fvg_low:,.4f}-${state.fvg_high:,.4f}  "
                  f"sweep=${state.sweep_low:,.4f}")

        elif state.bias == "BEARISH" and is_fvg_bear:
            # A generic gap, NOT the displacement that broke structure — say so.
            # Leaving this unset left amd_zone_type None, and None took the one
            # branch of sniper_entry_allowed() that never checked displacement.
            state.amd_zone_type = "bearish_fvg"
            state.state    = "ENTRY_WAIT"
            state.arm_zone(fvg_bear_bot, fvg_bear_top)
            print(f"[{base}] STEP 2: Bearish OB/FVG locked  "
                  f"${state.fvg_low:,.4f}-${state.fvg_high:,.4f}  "
                  f"sweep=${state.sweep_low:,.4f}")

    # ── Falling wedge breakout (cross-state) ─────────────────────────────────────
    # Detects descending highs + ascending lows converging, then price breaking
    # above the resistance line. Works from any state except POSITION_OPEN:
    #  • IDLE / SWEEP_HUNT       → arms a long retest ENTRY_WAIT immediately
    #  • ENTRY_WAIT (bearish)    → abandons the bearish setup, arms the long instead
    #  • ENTRY_WAIT (bullish)    → already set up, skip
    # AMD PRIORITY: never abandon a live AMD manipulation setup (a fresh sweep-reversal
    # short) for a wedge — the reordered engine above makes AMD the king, and the wedge
    # is a generic continuation setup that must defer to it.
    # GATED OFF (ENABLE_WEDGE_BREAKOUT) in pure-sniper mode — a falling wedge is a generic
    # pattern, not one of the two setups the trader takes by hand (AMD sweep / BOS retest).
    if ENABLE_WEDGE_BREAKOUT and not has_position and state.state != "POSITION_OPEN":
        _do_wedge = (state.state in ("IDLE", "SWEEP_HUNT") or
                     (state.state == "ENTRY_WAIT" and state.bias == "BEARISH"
                      and state.amd_phase not in ('manipulation_up', 'manipulation_down')))
        if _do_wedge:
            _wbk, _wsl, _wbl = indicators.detect_falling_wedge(df_ltf)
            if _wbk and _wsl is not None:
                # ── AMD-primary + displacement + overhead gates ──────────────────
                # 1) If an AMD manipulation setup is live this bar, defer to it — that's
                #    the higher-quality, into-the-ignition setup we want to prioritize.
                # 2) Require a real displacement candle — no weak breakouts into chop.
                # 3) Don't buy right under a resistance pool with no room to target.
                _atr5 = float((df_ltf['high'] - df_ltf['low']).rolling(14).mean().iloc[-1])
                _px_now = float(df_ltf['close'].iloc[-1])   # for the %-of-price size floor
                _recent = df_ltf.tail(3)[['open', 'high', 'low', 'close']].values.tolist()
                _amd_live = amd_phase_now in ('manipulation_up', 'manipulation_down')
                _disp = indicators.has_displacement(
                    _recent, is_long=True, min_body_frac=DISPLACEMENT_BODY_FRAC,
                    min_body_abs=indicators.displacement_min_body(_atr5, _px_now,
                                        DISPLACEMENT_ATR_MULT, DISPLACEMENT_MIN_PCT))
                _overhead = eqh_level if eqh_found else (eqh_h_level if eqh_h_found else None)
                _blocked = indicators.blocked_by_overhead(
                    price, True, _overhead, min_room=OVERHEAD_MIN_ROOM_ATR * _atr5)
                if _amd_live:
                    print(f"[{base}] 🔺 Wedge break — deferring to live AMD {amd_phase_now} setup.")
                elif not _disp:
                    print(f"[{base}] 🔺 Wedge break SKIPPED — no displacement candle "
                          f"(weak breakout, would bleed into chop).")
                elif _blocked:
                    print(f"[{base}] 🔺 Wedge break SKIPPED — resistance pool ${_overhead:,.4f} "
                          f"only {(_overhead - price):.4f} away (<{OVERHEAD_MIN_ROOM_ATR}× ATR room).")
                else:
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
        # armed, the move happened without us. Before giving up on it, check whether the
        # breakout still shows genuine continuation strength — if so, chase it directly
        # instead of only re-hunting a fresh setup.
        # (Measured vs the arm-price, not a displacement, so it never thrash-abandons.)
        if state.zone_set_price:
            ran_away = ((state.bias == "BEARISH" and price < state.zone_set_price * 0.985) or
                        (state.bias == "BULLISH" and price > state.zone_set_price * 1.015))
            if ran_away:
                moved = abs(price - state.zone_set_price) / state.zone_set_price
                if not ENABLE_CHASE:
                    print(f"[{base}] ⚠️ Zone abandoned — price ran {moved:.1%} without "
                          f"tapping and ENABLE_CHASE is off; re-hunting rather than chasing.")
                    state.reset()
                    return price
                chase_ok, chase_reason = check_chase_continuation(
                    df_ltf, state.bias,
                    min_body_abs=indicators.displacement_min_body(
                        indicators.range_atr(df_ltf), price,
                        DISPLACEMENT_ATR_MULT, DISPLACEMENT_MIN_PCT))
                if not chase_ok:
                    print(f"[{base}] ⚠️ Zone abandoned — price ran {moved:.1%} from setup "
                          f"(${state.zone_set_price:,.2f}→${price:,.2f}) without tapping; "
                          f"re-hunting the move.")
                    state.reset()
                    return price

                # Don't chase directly into the opposing pool — no room left to target.
                _chase_long = state.bias == "BULLISH"
                _chase_atr5 = float((df_ltf['high'] - df_ltf['low']).rolling(14).mean().iloc[-1])
                _chase_opp = ((eqh_level if eqh_found else eqh_h_level if eqh_h_found else None) if _chase_long
                              else (eql_level if eql_found else eql_h_level if eql_h_found else None))
                if indicators.blocked_by_overhead(price, _chase_long, _chase_opp,
                                                  min_room=OVERHEAD_MIN_ROOM_ATR * _chase_atr5):
                    print(f"[{base}] 🚫 Chase SKIPPED — opposing pool ${_chase_opp:,.4f} too close "
                          f"(<{OVERHEAD_MIN_ROOM_ATR}× ATR room to target); re-hunting.")
                    state.reset()
                    return price

                print(f"[{base}] 🚀 Chasing breakout — price ran {moved:.1%} without tapping "
                      f"(${state.zone_set_price:,.2f}→${price:,.2f}), momentum intact "
                      f"({chase_reason}) — entering directly.")
                is_long = state.bias == "BULLISH"
                state.is_chase = True
                state.stop_loss = (float(df_ltf['low'].tail(SWING_LOOKBACK).min()) * 0.999 if is_long
                                   else float(df_ltf['high'].tail(SWING_LOOKBACK).max()) * 1.001)
                chase_risk_amt = abs(price - state.stop_loss)
                if chase_risk_amt <= 0:
                    print(f"[{base}] ⏭ Chase skipped — swing SL invalid (no room).")
                    state.reset()
                    return price
                _chase_rng = df_htf['high'] - df_htf['low']
                chase_entry_atr = max(_chase_rng.rolling(14).mean().iloc[-1],
                                       _chase_rng.rolling(3).mean().iloc[-1])
                execute_confirmed_entry(symbol, base, state, paper, is_long, price, chase_risk_amt,
                                         df_ltf, df_htf, daily_trend, chase_entry_atr, now, risk_fraction,
                                         exchange=exchange)
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
            # Swing guard: push SL outside the structural swing high/low so a wick can't
            # sweep us before the market actually breaks structure (CHoCH). Combined with
            # the zone-edge breathing room by crypto_zone_stop_level (single source of
            # truth — was duplicated inline here and in the retest path below).
            _swing_s = (float(df_ltf['low'].tail(SWING_LOOKBACK).min()) * 0.999 if _is_long_s
                       else float(df_ltf['high'].tail(SWING_LOOKBACK).max()) * 1.001)
            _zone_lvl_s = indicators.crypto_zone_stop_level(
                _fill_s, _is_long_s, _fill_s, SL_ATR_MULT * _atr_s, _swing_s)
            # FLOOR (not cap) against 1H-ATR noise — this is the actual port: a stop can
            # no longer be crushed tighter than ~1.5x a real 1H candle's normal range.
            _sl_s = indicators.structural_stop_price(
                _fill_s, _zone_lvl_s, atr_1h or _atr_s, _is_long_s, MIN_STOP_ATR_MULT_HTF)
            _risk_s = abs(_fill_s - _sl_s)
            # TP pinned to the nearest 4H liquidity pool (≥MIN_AI_RR, ≤MAX_AI_RR) instead
            # of a flat MIN_AI_RR multiple — targets real structure, not just risk×2.
            _pool_s = indicators.find_next_liquidity_target(
                df_htf, _fill_s + MIN_AI_RR * _risk_s if _is_long_s else _fill_s - MIN_AI_RR * _risk_s,
                "bullish" if _is_long_s else "bearish")
            _tp_s = indicators.structural_take_profit(
                _fill_s, _risk_s, _pool_s, _is_long_s, MIN_AI_RR, MAX_AI_RR)
            # Same reachability clamp as the main path — the sniper arms its TP here and
            # never revisits it, so an unreachable target booked at arm time is locked in
            # for the whole 6h hold. (POL/USD 2026-09-04 was a sniper arm.)
            _tp_s, _rr_s_reach, _reach_ok_s = indicators.reachable_target(
                _fill_s, _sl_s, _tp_s, indicators.range_atr(df_htf),
                MAX_TARGET_ATR_MULT, MIN_AI_RR)
            if not _reach_ok_s:
                print(f"[{base}] 🚫 Sniper not armed — target unreachable inside "
                      f"{STALE_TRADE_HOURS}h (best reachable R:R 1:{_rr_s_reach:.1f}).")
            # Fixed-risk sizing: size so a stop-out loses exactly MAX_RISK_DOLLARS.
            # qty = $8 / stop-distance → a full 2R target pays ~$16, 3R ~$24; winners
            # outrun the $8 losses by design (replaces margin-% sizing that let a wide
            # stop risk far more than a tight one).
            _qty_s      = (paper.risk_budget / _risk_s) if _risk_s > 0 else 0
            _margin_s   = _qty_s * _fill_s / PAPER_LEVERAGE
            # Ceiling only: never deploy more margin than the daily cap allows. Trimming
            # here just lowers realized risk BELOW $8, never above it.
            _avail_s    = paper.check_daily_cap(BINANCE_CASH_AT_RISK * paper.balance)
            if _avail_s > 0 and _margin_s > _avail_s:
                _margin_s = _avail_s
                _qty_s    = _margin_s * PAPER_LEVERAGE / _fill_s
            _reward_s   = abs(_tp_s - _fill_s) * _qty_s
            if _qty_s > 1e-6 and _margin_s >= 2.0 and _reward_s >= 12.0 and _reach_ok_s:
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
        # Price must actually REACH the zone. The old check was a symmetric ±0.15% band,
        # which let a SHORT fill BELOW its supply zone — selling supply at a discount.
        # REAL INCIDENT 2026-08-11: AVAX filled at $6.231 against a $6.24–$6.454 zone,
        # clearing the old $6.2307 threshold by $0.0003 (0.005% of price) and shorting
        # 3.6% below the top of the supply it was supposedly selling into. Tolerance now
        # applies only on the far side, where overshooting improves the fill.
        in_fvg = indicators.price_in_entry_zone(
            price, state.fvg_low, state.fvg_high, state.bias == "BULLISH")

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

            # Confirmation depends on zone FRESHNESS (age since armed) and type:
            #  • FRESH zone (≤STALE_ZONE_BARS since armed): the setup that armed it (BOS
            #    displacement, wedge breakout, AMD sweep) is still recent.
            #      - CHoCH FVG: the displacement that left the gap already broke structure —
            #        it IS the change of character, so a quick retest is self-confirming
            #        (DIRECT TAP, no extra proof needed).
            #      - Everything else: still needs a REJECTION CANDLE at the level or a 15m
            #        CHoCH — proof the level is actually being defended, not just touched.
            #  • STALE zone (aged past STALE_ZONE_BARS, ANY type): the original momentum may
            #    have died while price chopped sideways waiting for the tap. This was the
            #    verified live gap — a BTC wedge_retest sat 37 bars (~3h on 5m) before firing
            #    on nothing but a bare rejection candle, well after the breakout's momentum
            #    was spent (the "buys after the move is already over, then drifts" pattern).
            #    A stale zone of ANY type now ALSO requires a FRESH displacement candle
            #    (has_displacement, ≤3 bars back) — real momentum happening NOW, not just a
            #    technically-qualifying signal from a dead tape. Fresh zones are untouched.
            is_fresh = state.bars_in_entry_wait <= STALE_ZONE_BARS
            rebounce = confirms or choch_aligned

            if state.amd_zone_type == "choch_fvg" and is_fresh:
                # The displacement gap IS the change of character, so no positive
                # confirmation is demanded — but "no confirmation needed" had been
                # implemented as "no check at all". POL/USD 2026-09-14 tapped this LONG
                # zone on a marubozu_bear, the bot printed '⚠️ marubozu_bear (no candle
                # confirm)', and entered anyway. That is not an unconfirmed tap, it is an
                # actively contradicted one. A VETO, not a requirement: only a decisive
                # opposing bar blocks it, so `normal` and `doji` taps still enter (XRP,
                # the one winner in this sample, tapped on `normal`).
                if indicators.tap_candle_opposes_bias(tap_candle, state.bias):
                    if _tap_changed:
                        print(f"[{base}] 🚫 Direct tap vetoed — tap bar is {tap_candle}, "
                              f"a decisive move AGAINST a {state.bias} entry. Waiting.")
                    return price
                print(f"[{base}] ⚡ Fresh displacement FVG retest — DIRECT TAP entry (no CHoCH needed)")
            elif is_fresh:
                if not rebounce:
                    if _tap_changed:
                        print(f"[{base}] ⏳ In zone — waiting for rebounce "
                              f"(rejection candle or 15m CHoCH {bias_str})")
                    return price
            else:
                # STALE ZONES NOW EXPIRE OUTRIGHT instead of trying to re-qualify via a
                # rebounce+fresh-displacement check. REAL INCIDENT 2026-07-30: an ADA
                # LONG fired through exactly this "stale but re-confirmed" path after
                # sitting armed 18 bars (~90min) — verified against real Coinbase data
                # (has_displacement independently recomputed = False on the exact
                # candles the bot would have seen) that there was NO real momentum, yet
                # it entered anyway. Traced this is NOT the sniper watcher (frozen
                # sniper_sl/tp didn't match the actual fill) and NOT the chase path
                # (price tapped normally, didn't overshoot) — the bug is somewhere in
                # this re-qualification logic itself and hasn't been pinned down yet.
                # Conservative interim fix: a stale zone is dead, full stop, no
                # exceptions, until the exact defect is found via live debugging.
                if _tap_changed:
                    print(f"[{base}] 💀 Zone expired — stale ({state.bars_in_entry_wait} bars/"
                          f"~{state.bars_in_entry_wait * 5}min) with no tap in time; "
                          f"resetting to IDLE rather than trying to re-qualify it.")
                state.reset()
                return price

            # 1. Calculate SL and risk distance
            # Structural SL: zone edge + 5m-ATR breathing room, widened further by the
            # swing-guard (a manipulation wick shouldn't sweep us before structure
            # actually breaks) — combined by crypto_zone_stop_level (same helper the
            # sniper-arm path above uses, so both paths agree). That level is then
            # FLOORED (not capped — the old MAX_SL_ATR_MULT cap crushed stops onto
            # quiet/cheap pairs, e.g. AVAX's $0.06 stop) against 1H-ATR noise via
            # structural_stop_price: a stop can never sit tighter than
            # MIN_STOP_ATR_MULT_HTF × a real 1H candle's range.
            _ltf_rng = df_ltf['high'] - df_ltf['low']
            _ltf_atr = float(_ltf_rng.rolling(14).mean().iloc[-1])
            _swing   = (float(df_ltf['low'].tail(SWING_LOOKBACK).min()) * 0.999 if is_long
                       else float(df_ltf['high'].tail(SWING_LOOKBACK).max()) * 1.001)
            _zone_edge = state.fvg_low if is_long else state.fvg_high
            _zone_lvl  = indicators.crypto_zone_stop_level(
                price, is_long, _zone_edge, SL_ATR_MULT * _ltf_atr, _swing)
            # Anchor to the ZONE EDGE, not the live fill — matching the sniper at
            # binance_bot.py:2298 (`structural_stop_price(_fill_s, ...)` with
            # _fill_s = state.fvg_low). structural_stop_price returns `entry - dist`, so
            # passing the live `price` let the stop SLIDE ALONG WITH THE FILL: risk stayed
            # pinned at ~1.5x 1H-ATR no matter how far past the zone price had already run,
            # and R:R on this path became structurally incapable of degrading with fill
            # quality. Measured on two live trades, same zone / same fill / same target:
            #   POL  sniper risk 0.0013 (1:1.69, REFUSED)  ->  5m risk 0.0010 (1:2.20, TAKEN)
            #   XRP  sniper risk 0.0268 (1:1.66, REFUSED)  ->  5m risk 0.0209 (1:2.12, TAKEN)
            # A 22-23% smaller denominator bought with nothing but a different anchor.
            state.stop_loss = indicators.structural_stop_price(
                _zone_edge, _zone_lvl, atr_1h or _ltf_atr, is_long, MIN_STOP_ATR_MULT_HTF)
            # ...but risk is still measured from the price we ACTUALLY fill at, so a worse
            # fill now costs MORE risk instead of dragging the stop up behind it. That is
            # what makes MIN_AI_RR a real filter on this path rather than an output.
            risk_amt = abs(price - state.stop_loss)
            print(f"[{base}] 📏 Structural SL ${state.stop_loss:,.4f}  "
                  f"(risk ${risk_amt:,.4f} = {risk_amt / (atr_1h or _ltf_atr):.1f}×1H-ATR)")

            state.is_chase = False   # this is a retest entry, not a chase
            execute_confirmed_entry(symbol, base, state, paper, is_long, price, risk_amt,
                                     df_ltf, df_htf, daily_trend, entry_atr, now, risk_fraction,
                                     exchange=exchange)

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
            if elapsed >= 45:
                print("    ⛔ 45 minutes is far past the worst-case Gatekeeper scan. "
                      "Something is wrong, not slow — stopping instead of waiting.",
                      flush=True)
                sys.exit(1)
    if _libs_failed.is_set():
        print("⛔ Refusing to start: the trading libraries did not load. "
              "See the error above.", flush=True)
        sys.exit(1)

    exchange     = connect_exchange()
    htf_exchange = connect_htf_exchange()
    paper  = PaperTrader(PAPER_BALANCE)
    states = {s: SymbolState() for s in symbols}
    _daily_snap = {"date": None, "open_balance": None, "open_btc": None}
    load_crypto_state(paper, states, symbols, _daily_snap)  # resume from last save if available
    if _daily_snap["open_balance"] is None:
        _daily_snap["open_balance"] = paper.equity({})   # no saved snapshot — baseline is current equity

    # ── Pure-sniper startup reconcile ───────────────────────────────────────────
    # A flag flip (ENABLE_TREND_FOLLOW/ENABLE_WEDGE_BREAKOUT) only stops NEW zones from
    # being armed — it can't touch a zone that's already sitting in ENTRY_WAIT, armed by
    # an OLDER process run before the flag was disabled, then restored verbatim here by
    # load_crypto_state. Fill logic at tap time is engine-agnostic (only checks
    # confirmation signals), so that stale zone could silently execute post-freeze.
    # REAL INCIDENT: an ADA trend_follow zone armed pre-freeze fired ~40min into a
    # restart already running the gated code. Reset any such pending (not yet filled)
    # zone to IDLE here so it can never tap in.
    for sym in symbols:
        st = states[sym]
        if st.state == "ENTRY_WAIT" and indicators.is_disabled_engine_zone(
                st.amd_phase, ENABLE_TREND_FOLLOW, ENABLE_WEDGE_BREAKOUT):
            print(f"[{sym.split('/')[0]}] 🚫 Startup: pending {st.amd_phase} zone is from a "
                  f"disabled engine (pure-sniper mode) — resetting to IDLE.")
            st.reset()

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
            # Scan the DOWNTIME, not just this instant. The stop lives only in this
            # process, so while the bot was off there was no resting order anywhere — and
            # comparing the CURRENT price against the levels misses a stop that was blown
            # through and recovered from during the gap, carrying the position on as
            # though it never happened. Walk the gap's 1m wicks instead; a resting order
            # fills the moment price TOUCHES a level.
            _gap_hit = None
            try:
                _since = int((st.entry_time.timestamp() if st.entry_time else 0) * 1000)
                _from = max(_since, int(getattr(paper, 'last_alive_ms', 0) or _since))
                if _from:
                    _gap = exchange.fetch_ohlcv(sym, "1m", since=_from, limit=1000)
                    _gap_hit = indicators.first_protective_breach(
                        _gap, st.stop_loss, st.take_profit, is_long, since_ms=_since)
            except Exception as _e:
                print(f"  ⚠️  {base}: could not replay the downtime ({type(_e).__name__}) "
                      f"— falling back to the current price only, which can MISS a stop "
                      f"that was hit and recovered from.", flush=True)

            if _gap_hit:
                _kind, fill, _ts = _gap_hit
                sl_hit, tp_hit = _kind == "STOP", _kind == "TARGET"
                label = (("🛡 BREAK-EVEN" if getattr(st, "breakeven_moved", False)
                          else "🔴 SL") if sl_hit else "🟢 TP")
                print(f"  ⏮ {base}: {_kind} was breached at "
                      f"{datetime.fromtimestamp(_ts/1000, timezone.utc):%Y-%m-%d %H:%M UTC} "
                      f"while the bot was DOWN — honouring it at ${fill:,.6g} "
                      f"(current price ${_cur:,.6g} would have missed it).", flush=True)
            else:
                sl_hit = (is_long and _cur <= st.stop_loss) or (not is_long and _cur >= st.stop_loss)
                tp_hit = (is_long and _cur >= st.take_profit) or (not is_long and _cur <= st.take_profit)
                fill  = st.stop_loss if sl_hit else st.take_profit
                label = (("🛡 BREAK-EVEN" if getattr(st, "breakeven_moved", False)
                          else "🔴 SL") if sl_hit else "🟢 TP")
            if sl_hit or tp_hit:
                close_qty = abs(held)
                if is_long:
                    paper.sell(sym, close_qty, fill)
                    pnl = (fill - st.entry_price) * close_qty
                else:
                    paper.buy(sym, close_qty, fill)
                    pnl = (st.entry_price - fill) * close_qty
                _fees = indicators.round_trip_fee(st.entry_price, fill, close_qty, TAKER_FEE_RATE)
                pnl -= _fees          # net — see close_position()
                _exit_reason = indicators.normalize_exit_reason(label, st.breakeven_moved)
                trade_print(base, f"{label} HIT (startup catch-up — bot was offline)", fill,
                            pnl=pnl, balance=paper.balance)
                _log_trade_close_to_sheet(base, is_long, st.entry_price, fill, close_qty, pnl, st, exchange,
                                          fees=_fees, exit_reason=_exit_reason)
                st.reset()
                print(f"  ⚠️  {base} {label} was missed while bot was offline — closed now at ${fill:,.4f}", flush=True)
            else:
                print(f"  ✅  {base} position intact  price=${_cur:,.4f}  SL=${st.stop_loss:,.4f}  TP=${st.take_profit:,.4f}", flush=True)
        except Exception as e:
            print(f"  Catch-up check failed for {sym}: {e}", flush=True)

    # ── Google Sheets: create tabs once at startup, track daily snapshot baseline ──
    ensure_tabs(get_sheet_client(), GOOGLE_SHEET_URL, {
        "Crypto Macro": MACRO_HEADER, "Crypto Ledger": LEDGER_HEADER,
    })
    prices = {}
    while True:
        print(f"\n{'─'*60}")
        print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"{'─'*60}")
        # Refresh the risk budget from LIVE equity, once, at the top of the cycle — so
        # every sizing site downstream agrees on one number for the whole pass. Equity
        # (not balance) because balance is free cash only and understates the account
        # while positions are open.
        _eq_now = paper.equity(prices)
        paper.risk_budget = indicators.risk_budget(
            _eq_now, RISK_PCT, RISK_FLOOR, RISK_CEILING)
        print(f"  Risk budget: ${paper.risk_budget:,.2f}/trade "
              f"({RISK_PCT*100:.2f}% of ${_eq_now:,.2f} equity)"
              + (f"  [{PAPER_LEVERAGE}x leverage — affects margin, not risk]"
                 if PAPER_LEVERAGE > 1 else ""))
        for symbol in symbols:
            try:
                result = process_symbol(exchange, paper, symbol, states[symbol],
                                        BINANCE_CASH_AT_RISK, htf_exchange=htf_exchange)
                if result is not None:
                    prices[symbol] = result
            except Exception as e:
                print(f"[{symbol}] Error [{type(e).__name__}]: {e}")

        positions_str = (
            "  ".join(f"{k.split('/')[0]}={v:.4f}" for k, v in paper.positions.items())
            or "none"
        )
        print(f"\n  Balance: ${paper.balance:,.2f}  |  Positions: {positions_str}")
        save_crypto_state(paper, states, symbols, prices, _daily_snap)

        # ── Google Sheets: once-per-UTC-day portfolio snapshot ──────────────────
        _btc_price = prices.get("BTC/USD")
        if _btc_price is None:
            try:
                _btc_price = float(exchange.fetch_ticker("BTC/USD")["last"])
            except Exception:
                _btc_price = None
        if _daily_snap["open_btc"] is None:
            _daily_snap["open_btc"] = _btc_price   # first tick ever — nothing to compare against yet
        _today = datetime.now().astimezone().date()   # LOCAL day — Macro rows dated by your date
        if _daily_snap["date"] != _today:
            _open_bal = _daily_snap["open_balance"]
            _open_btc = _daily_snap["open_btc"]
            # Portfolio Value = TRUE equity (cash + open positions), not free cash — the
            # Macro tab feeds the journal's Portfolio Value KPI, and free cash there made
            # it read hundreds low whenever a position was open (didn't reconcile with the
            # ledger's realized P&L). Same fix as the live header.
            _equity_now = paper.equity(prices)
            _daily_return_pct = ((_equity_now - _open_bal) / _open_bal * 100
                                  if _open_bal else 0.0)
            _btc_return_pct = (((_btc_price - _open_btc) / _open_btc * 100)
                                if _btc_price and _open_btc else 0.0)
            # Backfill any day the bot was down across midnight. Without this the rollover
            # check writes ONLY the current day and silently swallows the gap — REAL
            # INCIDENT: Crypto Macro jumped Aug 13 -> Aug 15, leaving a hole in the Quant
            # Desk equity curve. Missed days carry forward the last known equity: the bot
            # wasn't running, so no trade could have moved it, and a flat carry-forward is
            # honest where inventing a value would not be. 0% return marks them as
            # no-activity days rather than implying a real move.
            for _gap_day in missing_snapshot_dates(_daily_snap["date"], _today):
                print(f"[SHEETS] Backfilling missed Crypto Macro row for {_gap_day} "
                      f"(bot was down across that midnight) @ ${_open_bal:,.2f}")
                log_daily_snapshot(get_sheet_client(), GOOGLE_SHEET_URL, "Crypto Macro",
                                    _gap_day.isoformat(), _open_bal, 0.0,
                                    _btc_price or 0.0, 0.0)
            log_daily_snapshot(get_sheet_client(), GOOGLE_SHEET_URL, "Crypto Macro",
                                _today.isoformat(), _equity_now, _daily_return_pct,
                                _btc_price or 0.0, _btc_return_pct)
            _daily_snap["date"]         = _today
            _daily_snap["open_balance"] = _equity_now
            _daily_snap["open_btc"]     = _btc_price
            # Persist immediately — otherwise a restart before the next natural save
            # point re-reads the OLD (pre-update) dedup flag and re-logs today again.
            save_crypto_state(paper, states, symbols, prices, _daily_snap)
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
                # PAUSE_NEW_ENTRIES also blocks this — a zone armed BEFORE the pause
                # was deployed would otherwise still be able to fire here even though
                # process_symbol's main cycle no longer arms anything new.
                if (not PAUSE_NEW_ENTRIES
                        and st.state == "ENTRY_WAIT" and st.sniper_armed
                        and st.fvg_low and st.fvg_high
                        and not paper.get_position(sym)):
                    try:
                        _t      = exchange.fetch_ticker(sym)
                        _cur    = float(_t["last"])
                        _is_long = st.bias == "BULLISH"
                        # Use the SAME zone test as the 5m cycle. This was the old
                        # symmetric +/-0.15% band that price_in_entry_zone was written to
                        # kill: it let a LONG fill ABOVE the top of its own demand zone
                        # (HYPE 2026-09-14 filled $78.99 against a $78.60-$78.95 zone).
                        # Because the sniper freezes SL/TP to the zone edge but measures
                        # risk at the live fill, an out-of-zone fill manufactures a FALSE
                        # R:R decay — HYPE's "1:1.69" was a measurement artefact, not a
                        # verdict on the setup. Tolerance must apply only on the far side,
                        # where overshooting improves the fill.
                        _in     = indicators.price_in_entry_zone(
                            _cur, st.fvg_low, st.fvg_high, _is_long)
                        if _in:
                            _base    = sym.split("/")[0]

                            # Stale-zone gate — this watcher previously fired on PURE price-in-
                            # zone geometry with zero awareness of how long the zone had been
                            # armed. Real incident: a SOL zone sat stale 23 bars (~2h) with
                            # amd_zone_type=choch_fvg; the main 5m cycle's own staleness check
                            # (bars_in_entry_wait > STALE_ZONE_BARS requires fresh displacement)
                            # would have correctly rejected the eventual weak tap — but the
                            # sniper fired anyway because it never checked staleness at all.
                            # Fresh zones are completely unaffected (this only tightens the
                            # stale path, mirroring the main cycle's own gate exactly).
                            # Applies to FRESH zones too, not only stale ones. The old gate
                            # covered staleness alone while claiming to mirror the 5m cycle
                            # "exactly" — but the 5m cycle also requires a confirming tap
                            # candle on any non-choch_fvg zone, and the sniper skipped that
                            # entirely. Since it polls 10s against the cycle's 5m it wins
                            # nearly every race, so that gate became dead code: 19 of 22
                            # logged taps FAILED the bot's own confirmation check and were
                            # entered anyway, four of them marubozu_bear at LONG taps.
                            _is_stale = st.bars_in_entry_wait > STALE_ZONE_BARS
                            _fresh_mom = False
                            _tap_confirms = False
                            try:
                                _sniper_ltf = exchange.fetch_ohlcv(sym, "5m", limit=17)
                                _df_sniper  = pd.DataFrame(
                                    _sniper_ltf, columns=["t","open","high","low","close","v"])
                                _atr5_sniper = float((_df_sniper["high"] - _df_sniper["low"])
                                                      .rolling(14).mean().iloc[-1])
                                _px_sniper = float(_df_sniper["close"].iloc[-1])
                                _fresh_mom = indicators.has_displacement(
                                    _df_sniper.tail(3)[["open","high","low","close"]].values.tolist(),
                                    _is_long, min_body_frac=DISPLACEMENT_BODY_FRAC,
                                    min_body_abs=indicators.displacement_min_body(_atr5_sniper, _px_sniper,
                                        DISPLACEMENT_ATR_MULT, DISPLACEMENT_MIN_PCT))
                                _tap_confirms = indicators.candle_confirms_bias(
                                    indicators.classify_candle(_df_sniper.iloc[-1],
                                                               _df_sniper.iloc[-2]),
                                    "BULLISH" if _is_long else "BEARISH")
                            except Exception:
                                pass   # both stay False -> fail closed, do not fire

                            # choch_aligned=False on purpose: no 15m fetch on a 10s cadence.
                            # That makes the sniper STRICTER than the 5m path, never looser —
                            # a tap it declines is picked up by the next 5m cycle, which does
                            # have the 15m read. Declining late is recoverable.
                            _tap_ok, _tap_why = indicators.sniper_entry_allowed(
                                st.amd_zone_type, _is_stale, _fresh_mom,
                                _tap_confirms, choch_aligned=False)
                            if not _tap_ok:
                                print(f"[{_base}] ⏭ Sniper stood down — {_tap_why} "
                                      f"(zone={st.amd_zone_type}, {st.bars_in_entry_wait} bars armed)",
                                      flush=True)
                                continue

                            # Market-order realism: we fill at the price that actually
                            # printed, NOT the zone edge. Filling at fvg_high while the
                            # market trades at the zone's other side booked instant fake
                            # profit equal to the zone height (seen live: ADA "short from
                            # 0.164" while price was 0.1605 — 0.164 never traded).
                            _fill = _cur
                            _entry_liq = "taker"
                            if MAKER_ENTRIES:
                                # Rest at the zone's NEAR edge and fill there — only if
                                # price actually traded through it. Note this is usually a
                                # WORSE fill than _cur: a resting buy at fvg_high fills at
                                # fvg_high even though the market went on to fvg_low. That
                                # is what resting means, and modelling it is the whole
                                # point — the maker rate is paid for in fill quality.
                                _limit = st.fvg_high if _is_long else st.fvg_low
                                _rest = indicators.maker_limit_fill(
                                    _limit, bar_low=_cur, bar_high=_cur, is_long=_is_long)
                                if _rest is None:
                                    continue          # order still resting, not filled
                                _fill, _entry_liq = _rest, "maker"
                            # Geometry sanity: price can blow through the zone between
                            # arming and firing — never open a trade already past its
                            # own stop or target.
                            _past_sl = (_fill <= st.sniper_sl) if _is_long else (_fill >= st.sniper_sl)
                            _past_tp = (_fill >= st.sniper_tp) if _is_long else (_fill <= st.sniper_tp)
                            if _past_sl or _past_tp:
                                print(f"[{_base}] ⏭ Sniper aborted — fill ${_fill:,.4f} already beyond "
                                      f"{'SL' if _past_sl else 'TP'} "
                                      f"(SL ${st.sniper_sl:,.4f} / TP ${st.sniper_tp:,.4f})", flush=True)
                                st.sniper_armed = False
                                continue
                            # qty re-derived from the REAL fill so margin math stays exact
                            _qty = st.sniper_margin * PAPER_LEVERAGE / _fill

                            # ── Re-cap risk at the FILL, not the arm (fixed 2026-09-01) ──
                            # The sniper sized itself at ARM time: qty = MAX_RISK_DOLLARS /
                            # |arm_price - arm_sl|, which risks exactly the cap AT THAT PRICE.
                            # It then waits — often 10+ minutes — and fills somewhere else,
                            # while st.sniper_sl stays fixed. Re-deriving qty from
                            # sniper_margin preserves MARGIN, not RISK, so any drift away
                            # from the stop silently inflates the loss. The real risk was
                            # already being computed below as `_r` — and only PRINTED.
                            # Measured on real fills:
                            #   ADA 2026-08-30  risked $32.45 against a $20 cap  (+62%)
                            #   POL 2026-08-31  risked $23.04 against a $20 cap  (+15%)
                            _risk_at_fill = abs(_fill - st.sniper_sl)
                            _qty_capped = indicators.cap_qty_for_risk(
                                _qty, _risk_at_fill, paper.risk_budget)
                            if _qty_capped < _qty:
                                print(f"[{_base}] 🛡 Sniper risk re-cap at fill — qty "
                                      f"{_qty:.6f}→{_qty_capped:.6f} (fill ${_fill:,.4f} sits "
                                      f"${_risk_at_fill:,.4f} from the armed SL, which would "
                                      f"have risked ${_qty * _risk_at_fill:,.2f} > "
                                      f"${paper.risk_budget:,.2f})", flush=True)
                                _qty = _qty_capped
                                # margin must follow qty or the ledger's margin/notional lie
                                st.sniper_margin = _qty * _fill / PAPER_LEVERAGE

                            # R:R must survive the fill too, not just risk. The re-cap above
                            # holds the LOSS at MAX_RISK_DOLLARS, but the REWARD shrinks as the
                            # fill drifts away from the armed SL — and the only reward check
                            # below is an absolute $25 floor, which says nothing about the ratio.
                            # REAL FILL (SOL/USD 2026-09-10): armed on zone 101.06-101.25 with
                            # SL 99.5707 / TP 104.3579. At the zone LOW that is 1:2.21; it filled
                            # at 101.32 and went on at 1:1.74 — under the 1:2 floor the rest of
                            # the system enforces. Risk was a correct $20.00 the whole time,
                            # which is exactly why this slipped through unnoticed.
                            _rr_at_fill = (abs(st.sniper_tp - _fill) / _risk_at_fill
                                           if _risk_at_fill > 0 else 0.0)
                            if _rr_at_fill < MIN_AI_RR:
                                # RESET, don't merely disarm. R:R is a property of the
                                # SETUP (zone, stop, target) — not of which engine is
                                # looking at it. Clearing only `sniper_armed` left the zone
                                # live in ENTRY_WAIT, so the 5m cycle inherited a tap the
                                # bot had just rejected and took it minutes later (POL and
                                # XRP both did exactly this). carried_zone_age means a
                                # legitimate re-arm still reads as correctly aged rather
                                # than brand-new, which is what makes this safe.
                                print(f"[{_base}] 🚫 Setup rejected — R:R decayed to "
                                      f"1:{_rr_at_fill:.2f} at the fill (${_fill:,.4f} vs armed "
                                      f"zone), under the 1:{MIN_AI_RR:g} floor. Zone dropped so "
                                      f"the 5m cycle cannot re-take it.", flush=True)
                                st.reset()
                                continue

                            _min_a, _min_c = exchange_minimums(exchange, sym)
                            _ok_sz, _why_sz = indicators.meets_exchange_minimums(
                                _qty, _fill, _min_a, _min_c)
                            if not _ok_sz:
                                print(f"[{_base}] 🚫 Sniper stood down — {_why_sz}.",
                                      flush=True)
                                st.sniper_armed = False
                                continue

                            _sniper_reward = abs(st.sniper_tp - _fill) * _qty
                            if st.sniper_margin < 5.0 or _sniper_reward < 25.0:
                                print(f"[{_base}] ⏭ Sniper skipped — dust trade "
                                      f"(margin ${st.sniper_margin:.2f}, reward ${_sniper_reward:.2f})",
                                      flush=True)
                                st.sniper_armed = False
                                continue
                            if _is_long:
                                paper.buy(sym, _qty, _fill, _entry_liq)
                            else:
                                paper.sell(sym, _qty, _fill, _entry_liq)
                            paper.margin_used[sym] = st.sniper_margin
                            paper.record_margin(st.sniper_margin)
                            st.state       = "POSITION_OPEN"
                            st.entry_price = _fill
                            st.stop_loss   = st.sniper_sl
                            st.entry_stop_loss = st.sniper_sl  # snapshot BEFORE any breakeven trail
                            st.take_profit = st.sniper_tp
                            st.entry_time  = datetime.now(timezone.utc)
                            _lbl = "LONG" if _is_long else "SHORT"
                            _pv  = _qty * _fill
                            _r   = abs(_fill - st.sniper_sl) * _qty
                            _rw  = abs(st.sniper_tp - _fill) * _qty
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
                            save_crypto_state(paper, states, symbols, prices, _daily_snap)
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
                    prices[sym] = cur
                    save_crypto_state(paper, states, symbols, prices, _daily_snap)  # keep the live chart
                    # in sync every 10s — otherwise a scale-out/break-even sits unpersisted
                    # in memory until the next 5-min loop tick or an SL/TP exit.
                    if abs(held) < 1e-9:
                        continue

                    is_long   = held > 0
                    close_qty = abs(held)

                    # Use 1m wick data to catch crosses between polls. A real stop/limit
                    # order fills the instant price TOUCHES the level — so scan the wicks
                    # of EVERY candle since the last check, not just the forming one:
                    # the strategy-processing part of the cycle can take minutes (8
                    # symbols + AI calls), and a spike that pierced the level inside a
                    # candle that CLOSED during that blind window must still fill.
                    # Wick realism guard: a candle that STARTED before our entry may
                    # carry pre-entry prices in its wick — a stop that didn't exist yet
                    # can't fill on it (seen live: "TP hit" 1s after entry off a
                    # pre-fill wick), so those candles count via live price only.
                    try:
                        m1 = exchange.fetch_ohlcv(sym, "1m", limit=5)
                        candle_close = float(m1[-1][4]) if m1 else cur
                        candle_high  = cur
                        candle_low   = cur
                        entry_ms = int(st.entry_time.timestamp() * 1000) if st.entry_time else 0
                        # ...and not before the STOP itself. Same reasoning as pre-entry
                        # wicks, one level up: the break-even trail places the stop BELOW
                        # the price that triggered it, so every candle from before the
                        # move carries a low under the new stop and closes the trade on
                        # the same tick. See indicators.wick_fill_cutoff_ms.
                        _cutoff = indicators.wick_fill_cutoff_ms(
                            entry_ms, getattr(st, "stop_moved_ms", 0))
                        for c in (m1 or []):
                            if c[0] < _cutoff:
                                continue   # predates the entry OR the current stop
                            candle_high = max(candle_high, float(c[2]))
                            candle_low  = min(candle_low,  float(c[3]))
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
                    # A trailed stop is a BREAK-EVEN exit, not a stop-out. Labelling
                    # both "🔴 SL" is what made BTC (+$4.85, a trailed winner) get
                    # triaged as a "big SL loss" — normalize_exit_reason already
                    # draws the distinction two lines later for the ledger.
                    label = (("🛡 BREAK-EVEN" if getattr(st, "breakeven_moved", False)
                              else "🔴 SL") if sl_hit else "🟢 TP")
                    if is_long:
                        paper.sell(sym, close_qty, fill)
                        pnl = (fill - st.entry_price) * close_qty
                    else:
                        paper.buy(sym, close_qty, fill)
                        pnl = (st.entry_price - fill) * close_qty
                    _fees = indicators.round_trip_fee(st.entry_price, fill, close_qty, TAKER_FEE_RATE)
                    pnl -= _fees      # net — see close_position()
                    _exit_reason = indicators.normalize_exit_reason(label, st.breakeven_moved)
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
                    _log_trade_close_to_sheet(base, is_long, st.entry_price, fill, close_qty, pnl, st, exchange,
                                          fees=_fees, exit_reason=_exit_reason)
                    st.reset()
                    prices[sym] = cur
                    save_crypto_state(paper, states, symbols, prices, _daily_snap)
                except Exception:
                    pass


if __name__ == "__main__":
    run()

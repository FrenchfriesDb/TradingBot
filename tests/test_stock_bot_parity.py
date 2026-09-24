"""The stock bot gets the same four fixes the crypto bot got.

Four defects were found and fixed in binance_bot.py on 2026-09-24. The stock bot shares
bot/indicators.py, so it inherited ONE of them for free (candle-3 direction, which lives
inside detect_displacement_fvg). The other three were wired only into the crypto bot, and
a fourth — the durable ledger — existed nowhere in the stock path at all.

That split is this repo's dominant bug shape, now seen five times: a gate written
correctly and wired into the place someone happened to be looking at.

    fvg_tf label          3 of 9 arming branches
    MIN_FVG_PCT floor     3 of 5 zone producers
    opposing-candle veto  the 5m cycle, not the 10s sniper that wins every race
    drop_forming_candle   the 6H frame, not the 5m one
    all four of the above binance_bot, not bot/strategy.py

So these are source-level assertions rather than behavioural ones: the point is
COVERAGE, and a behavioural test of one call site would not have caught any of the five.
"""
import io
import re

import pytest

SRC = io.open("bot/strategy.py", encoding="utf-8").read()


# ── 1. zone edges from closed bars ───────────────────────────────────────────

def test_both_frames_expose_a_closed_bar_copy():
    """binance_bot had this on 6H only. This bot had it on NEITHER frame."""
    assert SRC.count("drop_forming_candle") >= 2, "one of the two frames is still live-only"
    assert '"df_closed"' in SRC


@pytest.mark.parametrize("fn", ["find_bullish_fvg", "find_bearish_fvg",
                                "find_supply_zone", "find_demand_zone"])
def test_no_zone_finder_is_handed_a_live_frame(fn):
    """Each of these produces a STORED level. Derived from a forming bar, the level
    redraws itself while the zone it armed stays frozen."""
    for m in re.finditer(rf"indicators\.{fn}\(\s*([^,\n]+)", SRC):
        arg = m.group(1).strip()
        assert "closed" in arg, f"{fn}() derives a stored zone from {arg}"


def test_displacement_detection_uses_the_closed_frames():
    for m in re.finditer(r"indicators\.detect_displacement_fvg\(\s*([^,\n]+)", SRC):
        assert "_hc" in m.group(1) or "_lc" in m.group(1) or "closed" in m.group(1)


def test_live_reads_still_use_the_forming_bar():
    """Guard the other direction — this must not become a blanket swap. Price, sweeps and
    the tap candle have to describe now."""
    assert re.search(r"check_liquidity_sweep\(df\)", SRC), "sweeps lost their live bar"
    assert 'zone_tapped(ltf["df"]' in SRC, "the tap must read live bars"


# ── 2. the displacement is sized against the risk ────────────────────────────

def test_every_displacement_gate_passes_the_1h_atr():
    """The 15m ATR is measured over the same chop the gate exists to exclude, so on a
    quiet tape an ordinary bar clears a multiple of its own collapsed ATR."""
    gates = re.findall(r"displacement_gates\((.*?)\)\)", SRC, re.S)
    assert gates, "no displacement_gates call found — the regex has drifted"
    for g in gates:
        assert "htf_atr" in g, f"a gate still has no risk-relative floor: {g[:70]}"


# ── 3. a retest must be a retest ─────────────────────────────────────────────

def test_the_retest_flag_exists_and_is_tracked():
    assert "self.zone_left" in SRC
    assert "zone_left_since_arming" in SRC, "the flag is declared but never updated"


def test_the_retest_flag_gates_the_tap():
    assert re.search(r"if _bar_tap and not self\.zone_left\[symbol\]", SRC), \
        "zone_left is tracked but does not actually block an entry"


def test_the_retest_flag_is_cleared_on_reset():
    """Left set, it would silently pre-approve the NEXT zone on that symbol."""
    reset = SRC[SRC.index("def _reset(self, symbol):"):]
    reset = reset[:reset.index("\n    def ", 10)]
    assert re.search(r"self\.zone_left\[symbol\]\s*=\s*False", reset)


# ── 4. the trade survives the Sheets call ────────────────────────────────────

def test_the_close_is_written_to_disk_before_sheets():
    assert "_ledger.append_row" in SRC
    assert "_ledger.flush_pending" in SRC


def test_the_sender_reports_failure_rather_than_swallowing_it():
    """log_trade() returns None either way, so a None client must count as failure —
    no credentials means the trade was NOT recorded."""
    fn = SRC[SRC.index("def _send_ledger_row"):]
    fn = fn[:fn.index("\n    def ", 10)]
    assert "return False" in fn and "client is None" in fn


def test_the_old_fire_and_forget_write_is_gone():
    """Exactly ONE real log_trade() call, and it is the one inside _send_ledger_row.

    Counted on code lines only — an earlier version of this test counted a mention of
    log_trade() inside a COMMENT and failed on prose."""
    calls = [ln for ln in SRC.splitlines()
             if "log_trade(" in ln and not ln.strip().startswith("#")]
    assert len(calls) == 1, calls
    fn = SRC[SRC.index("def _send_ledger_row"):]
    fn = fn[:fn.index("\n    def ", 10)]
    assert calls[0] in fn, "the only log_trade() call is outside the retry-aware sender"


def test_the_stock_ledger_is_separate_from_the_crypto_one():
    assert 'ledger_path("stock")' in SRC

"""The HTF frame is chosen at RUNTIME, so nothing may assume it from the constant.

binance_bot fetches higher-timeframe candles from Bybit (BYBIT_HTF_TIMEFRAME, 4h) and falls
back to the main exchange (HTF_TIMEFRAME, 6h) when Bybit does not answer — decided per
symbol, per cycle, by which exchange replied. `htf_name` carries the frame that actually
arrived.

Three things then read the CONSTANT instead of htf_name:
  * MAX_TARGET_ATR_MULT, derived from HTF_TIMEFRAME at import, while reachable_target is
    handed range_atr(df_htf) — a 4H ATR clamped by a 6H-derived multiple. Concretely: on an
    18h hold the clamp should be 6.75 on 4h data and 4.50 on 6h, so the guard that decides
    whether a target is reachable at all was 33% too tight whenever Bybit answered.
  * the FVG zone labels, which would name a 4H zone "6H".
  * a comment claiming "4H = full conviction", which sent the operator to a 4H chart to
    check a BOS the bot had measured on 6H.

Dormant on a US connection — Bybit returns 403 there and everything stays on 6h, which is
exactly why this would have gone unnoticed until the day it did not. Found 2026-10-04 while
tracing a BTC chart question, and it is the fifth instance in one session of a value being
correct at its definition and wrong at the point of use.
"""
import ast
import pathlib

import pytest

import binance_bot

ROOT = pathlib.Path(__file__).resolve().parents[1]


class TestClampFollowsTheLiveFrame:
    def test_the_frame_changes_the_clamp(self):
        four, six = (binance_bot.max_target_atr_mult_for("4h"),
                     binance_bot.max_target_atr_mult_for("6h"))
        assert four != six, "a 4h ATR and a 6h ATR cannot share one multiple"
        assert four > six, "a shorter frame fits MORE candles in the hold, so it reaches further"

    def test_values_match_the_hold_window_arithmetic(self):
        # 1.5 x (STALE_TRADE_HOURS / frame hours), floored at 1.5
        hold = binance_bot.STALE_TRADE_HOURS
        for name, hours in (("4h", 4), ("6h", 6), ("1h", 1)):
            expect = 1.5 * max(1.0, hold / hours)
            assert abs(binance_bot.max_target_atr_mult_for(name) - expect) < 1e-9, name

    def test_never_below_the_floor(self):
        """A frame longer than the hold must not shrink the clamp below 1.5x."""
        assert binance_bot.max_target_atr_mult_for("1d") >= 1.5

    def test_unknown_frame_falls_back_to_the_constant(self):
        # plain arithmetic, not pytest.approx: approx reaches into numpy, which is
        # mid-import when this module imports binance_bot and raises a circular-import
        # AttributeError. Second time in this suite — see test_stop_fills_pay_slippage.
        assert abs(binance_bot.max_target_atr_mult_for(None)
                   - binance_bot.MAX_TARGET_ATR_MULT) < 1e-9

    def test_explicit_env_override_still_wins(self, monkeypatch):
        """An operator who pins the number means it, whatever frame the data arrived on."""
        monkeypatch.setattr(binance_bot, "_MAX_TARGET_ATR_MULT_ENV", "9.9")
        monkeypatch.setattr(binance_bot, "MAX_TARGET_ATR_MULT", 9.9)
        assert binance_bot.max_target_atr_mult_for("4h") == 9.9
        assert binance_bot.max_target_atr_mult_for("6h") == 9.9


class TestNoCallSiteAssumesTheFrame:
    @pytest.mark.parametrize("rel", ["binance_bot.py", "backtest_crypto.py"])
    def test_reachability_is_never_clamped_by_the_bare_constant(self, rel):
        tree = ast.parse((ROOT / rel).read_text())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", getattr(node.func, "id", None)) != "reachable_target":
                continue
            args = [ast.unparse(a) for a in node.args]
            clamp = next((a for a in args if "ATR_MULT" in a or "atr_mult" in a), None)
            assert clamp is not None, f"{rel}:{node.lineno} — no clamp argument found"
            if clamp.strip() == "MAX_TARGET_ATR_MULT":
                offenders.append(f"{rel}:{node.lineno} — clamps with the import-time constant")
        assert not offenders, (
            "reachable_target must be clamped by max_target_atr_mult_for(<live frame>):\n  "
            + "\n  ".join(offenders))

    def test_zone_labels_name_the_frame_that_arrived(self):
        """A 4H zone labelled '6H' is a lie told to whoever reads the log."""
        tree = ast.parse((ROOT / "binance_bot.py").read_text())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", getattr(node.func, "id", None)) != "zone_source_tf":
                continue
            if any(ast.unparse(a).strip() == "HTF_TIMEFRAME" for a in node.args):
                offenders.append(f"binance_bot.py:{node.lineno}")
        assert not offenders, (
            "zone_source_tf must be passed htf_name, not HTF_TIMEFRAME: " + ", ".join(offenders))

    def test_the_bos_comment_no_longer_asserts_a_frame_it_cannot_know(self):
        src = (ROOT / "binance_bot.py").read_text()
        assert "# Dual HTF BOS: 4H = full conviction" not in src, (
            "the HTF frame is 4h OR 6h depending on which exchange answered — a comment "
            "that names one sends the operator to the wrong chart")

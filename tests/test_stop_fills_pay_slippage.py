"""A stop fills WORSE than its trigger, and each leg pays its own fee rate.

TWO defects, same shape, found 2026-10-03 — and the first attempt at guarding them had a
hole, which is why this file now enumerates by AST instead of matching strings.

1. STOP FILLS. Three separate live sites closed a position at the stop PRICE while
   detecting the hit by observing price trade THROUGH it — booking the best available
   price on direct evidence a worse one existed. The first fix caught one site. The
   startup catch-up path (a ternary with different spacing) and the downtime-replay path
   both survived it, AND survived a test that string-matched `fill = st.stop_loss\\n`.
   Everything now goes through indicators.stop_fill_price, the single producer.

2. FEE LEGS. round_trip_fee has taken an exit_fee_rate since it was written, and
   binance_bot documents the model at :483 — a resting entry and a resting TP earn MAKER,
   a stop crosses the book and pays TAKER. One of four call sites actually passed it.

Calibration: 0.15 R mean overshoot per stop-out (median 0.03, p90 0.39, max 0.65) from a
60d/13-symbol replay; 0.044 R per trade against a measured gross edge of +0.086 R — about
half the edge. Real, but NOT decisive, which is the opposite of what I predicted.
"""
import ast
import pathlib

import pytest

from bot import indicators

ROOT = pathlib.Path(__file__).resolve().parents[1]
FILES = ["binance_bot.py", "backtest_crypto.py"]


def _tree(rel):
    return ast.parse((ROOT / rel).read_text())


class TestStopFillPrice:
    def test_long_stop_fills_below_the_trigger(self):
        fill = indicators.stop_fill_price(100.0, 99.0, True, 0.15)
        assert fill < 99.0
        assert abs(fill - 98.85) < 1e-9

    def test_short_stop_fills_above_the_trigger(self):
        fill = indicators.stop_fill_price(100.0, 101.0, False, 0.15)
        assert fill > 101.0
        assert abs(fill - 101.15) < 1e-9

    @pytest.mark.parametrize("entry,stop,is_long", [(100.0, 99.0, True), (100.0, 101.0, False)])
    def test_a_stop_out_costs_strictly_more_than_1r(self, entry, stop, is_long):
        fill = indicators.stop_fill_price(entry, stop, is_long, 0.15)
        r = abs(entry - stop)
        realised = ((fill - entry) if is_long else (entry - fill)) / r
        assert realised < -1.0, f"a stop-out must cost MORE than 1R, got {realised:.3f}R"
        assert abs(realised - (-1.15)) < 1e-9

    def test_zero_slippage_reproduces_the_old_behaviour(self):
        assert indicators.stop_fill_price(100.0, 99.0, True, 0.0) == 99.0

    def test_bad_input_returns_the_stop_rather_than_raising(self):
        """Runs on a close path, after the position is already gone. Must not throw."""
        assert indicators.stop_fill_price(None, 99.0, True, 0.15) == 99.0

    def test_negative_slippage_cannot_flatter_a_fill(self):
        assert indicators.stop_fill_price(100.0, 99.0, True, -5.0) == 99.0


class TestEveryStopFillUsesTheProducer:
    @pytest.mark.parametrize("rel", FILES)
    def test_no_fill_variable_is_assigned_a_raw_stop_level(self, rel):
        """AST, not string matching — the previous version of this test passed while two
        live sites were still filling at the bare trigger."""
        offenders = []
        for node in ast.walk(_tree(rel)):
            if not isinstance(node, ast.Assign):
                continue
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not any(n == "fill" or n.endswith("_fill") for n in names):
                continue
            src = ast.unparse(node.value)
            if "stop" in src and "stop_fill_price" not in src:
                offenders.append(f"{rel}:{node.lineno} — {src[:90]}")
        assert not offenders, (
            "a stop fill must come from indicators.stop_fill_price:\n  " + "\n  ".join(offenders))

    def test_the_producer_is_actually_reached_from_both_files(self):
        for rel in FILES:
            assert "stop_fill_price" in (ROOT / rel).read_text(), (
                f"{rel} closes positions at stops but never calls the producer")


class TestFeeLegsAreChargedSeparately:
    @pytest.mark.parametrize("rel", FILES)
    def test_round_trip_fee_is_given_an_exit_rate(self, rel):
        """A resting TP earns MAKER; a stop pays TAKER. One blended rate understates
        losers and overstates winners.

        Allowlisted: a MANUAL dashboard close genuinely crosses the book on both legs, so
        taker-only is correct there — but it must say so at the call site.
        """
        src = (ROOT / rel).read_text()
        lines = src.splitlines()
        offenders = []
        for node in ast.walk(_tree(rel)):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", getattr(node.func, "id", None)) != "round_trip_fee":
                continue
            if len(node.args) + len(node.keywords) >= 5:
                continue
            context = "\n".join(lines[max(0, node.lineno - 4):node.lineno + 4])
            if "MANUAL CLOSE" in context:          # genuine taker/taker
                continue
            offenders.append(f"{rel}:{node.lineno} — only {len(node.args)} args, no exit rate")
        assert not offenders, (
            "round_trip_fee must be given a per-leg exit rate:\n  " + "\n  ".join(offenders))

    def test_maker_rate_assumes_no_discount_until_one_is_configured(self):
        """MAKER_FEE_RATE must default to the taker rate. Assuming a discount nobody has
        verified is how the crypto numbers got optimistic in the first place."""
        import binance_bot
        assert binance_bot.MAKER_FEE_RATE == binance_bot.TAKER_FEE_RATE, (
            "MAKER_FEE_RATE must default to TAKER_FEE_RATE — set it from a real fee tier")

    def test_maker_entries_default_off(self):
        """A resting entry trades fill quality for the maker rate and some taps stop
        filling entirely. It is not a free win and must be opted into."""
        import binance_bot
        assert binance_bot.MAKER_ENTRIES is False

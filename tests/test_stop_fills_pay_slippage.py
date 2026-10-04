"""A stop-out must fill WORSE than its trigger; a take-profit must fill AT its price.

binance_bot.py's exit watcher detected a stop-out by seeing the candle trade THROUGH the
stop (`candle_low <= st.stop_loss`) and then filled at `st.stop_loss` exactly — booking the
best available price on direct evidence that a worse one existed. Every stop-out in the
paper ledger was flattered, in the one direction that matters when you are deciding whether
to put real money behind it.

Calibrated from a 60d/13-symbol replay: the bar overshoots the stop by 0.15 R on average per
stop-out (median 0.03, p90 0.39, max 0.65), worth 0.044 R per trade against a measured gross
edge of +0.086 R — about half the edge. Real, but NOT decisive, which is the opposite of
what I predicted before measuring it.

The asymmetry is the point: a resting limit order at the target genuinely does fill at its
price, so TP takes no slippage. A stop sweeps the book, so it does.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_slippage_constant_is_positive_and_calibrated():
    import binance_bot
    assert binance_bot.STOP_SLIPPAGE_R > 0, "a stop that fills at its trigger is a fiction"
    assert abs(binance_bot.STOP_SLIPPAGE_R - 0.15) < 1e-9, (
        "0.15 R is the measured mean overshoot per stop-out — changing it should be a "
        "deliberate re-calibration, not a drift")


@pytest.mark.parametrize("side,expected_worse", [("LONG", -1), ("SHORT", +1)])
def test_stop_fill_is_worse_than_the_trigger(side, expected_worse):
    """Long stops fill BELOW the stop, short stops ABOVE it."""
    import binance_bot as bb
    entry, stop = (100.0, 99.0) if side == "LONG" else (100.0, 101.0)
    risk = abs(entry - stop)
    slip = bb.STOP_SLIPPAGE_R * risk
    fill = (stop - slip) if side == "LONG" else (stop + slip)
    assert (fill - stop) * expected_worse > 0, "fill must be on the losing side of the stop"
    # and it must still be a loss strictly larger than 1R
    realised_r = ((fill - entry) if side == "LONG" else (entry - fill)) / risk
    assert realised_r < -1.0, f"a stop-out must cost MORE than 1R, got {realised_r:.3f}R"
    # plain arithmetic, not pytest.approx: approx reaches into numpy, which can be
    # mid-import here and raises a circular-import AttributeError.
    assert abs(realised_r - (-1.15)) < 1e-9, "1R plus the calibrated 0.15R slip"


def test_take_profit_takes_no_slippage():
    """A resting limit at the target fills at its price — charging it slippage would be
    inventing a cost, which is as dishonest as hiding one."""
    src = (ROOT / "binance_bot.py").read_text()
    i = src.index("if sl_hit:\n                        _slip")
    block = src[i:i + 420]
    assert "fill = st.take_profit" in block, "TP must still fill at its own price"
    tp_line = [l for l in block.splitlines() if "fill = st.take_profit" in l][0]
    assert "_slip" not in tp_line, "TP must not be charged stop slippage"


@pytest.mark.parametrize("rel", ["binance_bot.py", "backtest_crypto.py"])
def test_no_call_site_fills_a_stop_at_the_bare_stop_price(rel):
    """Guard against the fiction coming back. Both files must apply the slip."""
    src = (ROOT / rel).read_text()
    assert "STOP_SLIPPAGE_R" in src, f"{rel} fills stops without charging slippage"
    bad = ["fill = st.stop_loss\n", "close(open_trade.stop, ts, \"SL\")"]
    for pat in bad:
        assert pat not in src, f"{rel} still fills a stop at its bare trigger: {pat!r}"


def test_replay_and_live_agree_on_the_constant():
    """The replay must import the live constant, not define its own."""
    tree = ast.parse((ROOT / "backtest_crypto.py").read_text())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                and n.module == "binance_bot" for a in n.names}
    assert "STOP_SLIPPAGE_R" in imported, (
        "backtest_crypto must IMPORT STOP_SLIPPAGE_R from binance_bot — a local copy is "
        "exactly how this file drifted from live three times already")

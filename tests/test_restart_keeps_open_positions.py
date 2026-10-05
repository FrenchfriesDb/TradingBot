"""A restart must not abandon an open position.

WHAT HAPPENED (2026-10-03/04). test_bot opened an ETH short, then restarted:

    [ETH] SWEEP SHORT @ $2,688.54  SL=$2,697.06  TP=$2,650.00  qty=1.859745
    [CRYPTO] Cash: $0.00  |  Equity: $5,009.39  |  ETH=S1.8597
    ===== 1H/1M SWEEP-REVERSAL TEST BOT =====         <- restart
    [STATE] Resumed today's risk budget used: $15.84

After that line the position did not exist. No stop, no target, no exit, no ledger row —
it evaporated, and the balance reset to the $5,000 start. The operator's dashboard kept
rendering it from the last state it had read, so the screen said "SHORT, +$9.39" about a
trade the bot had forgotten.

save_test_state had ALWAYS written the full record (qty, side, entry, SL, TP, entry_time).
_restore_daily_budget read back exactly two fields: daily_risked and daily_date. The data
was there the whole time and nothing loaded it.

On paper this costs a trade. On a real account it leaves a position with NO STOP while the
operator believes it is protected.
"""
import json

import pytest

import test_bot as T


class _Paper:
    def __init__(self, bal=5000.0):
        self.balance = bal
        self.positions = {}
        self.entry_prices = {}


def _write(tmp_path, monkeypatch, payload):
    f = tmp_path / "test_state.json"
    f.write_text(json.dumps(payload))
    monkeypatch.setattr(T, "TEST_STATE_FILE", str(f))
    return f


def _state(**over):
    base = {
        "balance": 5009.39,
        "stats": {"trades": 3, "wins": 2, "losses": 1,
                  "gross_win": 100.0, "gross_loss": 40.0},
        "positions": {
            "ETH/USD": {"qty": -1.859745, "side": "SHORT", "entry_price": 2688.54,
                        "stop_loss": 2697.0557, "take_profit": 2650.0,
                        "entry_time": "2026-10-03T18:10:00+00:00"},
        },
    }
    base.update(over)
    return base


def _fresh():
    syms = ["ETH/USD", "BTC/USD"]
    return (_Paper(), {s: None for s in syms}, {s: None for s in syms},
            {s: None for s in syms}, T.new_stats())


class TestRestore:
    def test_the_position_comes_back(self, tmp_path, monkeypatch):
        _write(tmp_path, monkeypatch, _state())
        paper, sl, tp, et, stats = _fresh()
        T.restore_open_positions(paper, sl, tp, et, stats)
        assert paper.positions["ETH/USD"] == pytest.approx(-1.859745)
        assert paper.entry_prices["ETH/USD"] == pytest.approx(2688.54)

    def test_the_stop_comes_back(self, tmp_path, monkeypatch):
        """The whole point: a resumed position that has no stop is worse than none."""
        _write(tmp_path, monkeypatch, _state())
        paper, sl, tp, et, stats = _fresh()
        T.restore_open_positions(paper, sl, tp, et, stats)
        assert sl["ETH/USD"] == pytest.approx(2697.0557)
        assert tp["ETH/USD"] == pytest.approx(2650.0)

    def test_balance_and_stats_survive(self, tmp_path, monkeypatch):
        _write(tmp_path, monkeypatch, _state())
        paper, sl, tp, et, stats = _fresh()
        T.restore_open_positions(paper, sl, tp, et, stats)
        assert paper.balance == pytest.approx(5009.39)
        assert stats["trades"] == 3 and stats["wins"] == 2

    def test_entry_time_is_restored(self, tmp_path, monkeypatch):
        _write(tmp_path, monkeypatch, _state())
        paper, sl, tp, et, stats = _fresh()
        T.restore_open_positions(paper, sl, tp, et, stats)
        assert et["ETH/USD"] is not None


class TestItRefusesHalfRecords:
    @pytest.mark.parametrize("broken", [
        {"qty": -1.0, "side": "SHORT"},                      # no entry price
        {"entry_price": 2688.54, "side": "SHORT"},           # no qty
        {"qty": 0, "entry_price": 2688.54},                  # flat
    ])
    def test_incomplete_position_is_not_resumed(self, tmp_path, monkeypatch, broken):
        """Resuming a half-written record would restore a position WITHOUT its stop.
        Skipping loudly is correct; silently resuming it is the dangerous option."""
        _write(tmp_path, monkeypatch, _state(positions={"ETH/USD": broken}))
        paper, sl, tp, et, stats = _fresh()
        T.restore_open_positions(paper, sl, tp, et, stats)
        assert "ETH/USD" not in paper.positions


class TestFailSoft:
    def test_missing_file_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(T, "TEST_STATE_FILE", str(tmp_path / "nope.json"))
        paper, sl, tp, et, stats = _fresh()
        T.restore_open_positions(paper, sl, tp, et, stats)   # must not raise
        assert paper.positions == {}

    def test_corrupt_file_does_not_raise(self, tmp_path, monkeypatch):
        f = tmp_path / "test_state.json"
        f.write_text("{not json")
        monkeypatch.setattr(T, "TEST_STATE_FILE", str(f))
        paper, sl, tp, et, stats = _fresh()
        T.restore_open_positions(paper, sl, tp, et, stats)
        assert paper.positions == {}

    def test_no_positions_key_is_fine(self, tmp_path, monkeypatch):
        _write(tmp_path, monkeypatch, {"balance": 5000.0})
        paper, sl, tp, et, stats = _fresh()
        T.restore_open_positions(paper, sl, tp, et, stats)
        assert paper.positions == {}


def test_startup_calls_the_restore():
    """Guard the wiring, not just the function — the save side was correct for months
    while nothing called a loader."""
    import inspect
    src = inspect.getsource(T.run_crypto_sweep)
    assert "restore_open_positions(" in src, "startup never restores open positions"
    assert src.index("sl_levels") < src.index("restore_open_positions("), \
        "restore must run AFTER the level dicts exist, or it writes into nothing"

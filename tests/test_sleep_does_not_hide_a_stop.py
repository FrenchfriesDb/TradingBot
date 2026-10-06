"""Closing the laptop must not let a stop be blown through unseen.

Operator moves around with the laptop and closes it. Nothing runs while the machine sleeps,
and crypto here is paper — the stop lives only inside the process — so a stop touched at
minute 10 of a 60-minute sleep and recovered from by minute 40 was never evaluated.

The startup catch-up already knew how to replay missed 1m wicks, but it ran only when the
process RESTARTED. A sleep does not restart anything: the process resumes as if no time had
passed, and the 10-second watcher looks at the last five 1m candles only.

Checking more often does not help. The operator's instinct was a 2.5-minute poll; the
watcher already polls every 10 seconds and tests candle wicks. What is missing is looking
BACK across a window in which nothing was running at all.
"""
import pathlib
import types

import pytest

import binance_bot as B

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True, scope="module")
def _libs_loaded():
    """binance_bot loads indicators on a background thread; wait for it, as production does."""
    assert B._libs_ready.wait(timeout=120)


MIN = 60_000


def _c(ts, high, low):
    return [ts, low, high, low, low, 1.0]          # [ts, open, high, low, close, vol]


class _State:
    def __init__(self, **kw):
        self.state = "POSITION_OPEN"
        self.entry_price, self.stop_loss, self.take_profit = 100.0, 99.0, 110.0
        self.entry_time = None
        self.breakeven_moved = False
        self.was_reset = False
        self.__dict__.update(kw)

    def reset(self):
        self.was_reset = True
        self.state = "IDLE"


class _Paper:
    def __init__(self, qty):
        self.pos, self.last_alive_ms = {"X/USD": qty}, 0
        self.balance, self.sold, self.bought = 1000.0, [], []

    def get_position(self, s):
        return self.pos.get(s, 0.0)

    def sell(self, s, q, px):
        self.sold.append((s, q, px)); self.pos[s] = 0.0

    def buy(self, s, q, px):
        self.bought.append((s, q, px)); self.pos[s] = 0.0


class _Exchange:
    def __init__(self, candles, last=103.0):
        self.candles, self.last, self.since_seen = candles, last, None

    def fetch_ticker(self, _):
        return {"last": self.last}

    def fetch_ohlcv(self, _sym, _tf, since=None, limit=None):
        self.since_seen = since
        return [c for c in self.candles if since is None or c[0] >= since]


@pytest.fixture
def quiet(monkeypatch):
    seen = {"closed": [], "logged": []}
    monkeypatch.setattr(B, "trade_print", lambda *a, **k: seen["closed"].append((a, k)))
    monkeypatch.setattr(B, "_log_trade_close_to_sheet",
                        lambda *a, **k: seen["logged"].append((a, k)))
    return seen


def _run(candles, from_ms, last=103.0, qty=1.0, **state_kw):
    paper, st = _Paper(qty), _State(**state_kw)
    ex = _Exchange(candles, last)
    B.catch_up_open_positions(paper, {"X/USD": st}, ["X/USD"], ex, from_ms=from_ms,
                              why="machine was asleep ~60m", what="the machine was ASLEEP")
    return paper, st, ex


class TestTheSleepCase:
    def test_a_stop_touched_mid_sleep_and_recovered_is_still_honoured(self, quiet):
        """THE case. Dips through 99 at minute 10, back above entry by minute 40, and the
        price right now is 103 — a current-price check sees nothing wrong."""
        candles = [_c(1 * MIN, 101, 100), _c(10 * MIN, 100, 98.0), _c(40 * MIN, 104, 103)]
        paper, st, _ = _run(candles, from_ms=1 * MIN, last=103.0)
        assert st.was_reset, "the stop was blown through during the sleep and ignored"
        assert paper.sold, "a long stopped out must be SOLD"

    def test_it_closes_below_the_stop_not_at_it(self, quiet):
        """A stop triggers on touch and then crosses the book; it does not fill at the level.
        Same fiction already removed from the other stop-fill sites."""
        candles = [_c(10 * MIN, 100, 98.0)]
        paper, _, _ = _run(candles, from_ms=1 * MIN)
        _, _, fill = paper.sold[0]
        assert fill < 99.0, "filled AT the stop level — the slippage fix was lost here"

    def test_a_target_touched_mid_sleep_is_honoured(self, quiet):
        candles = [_c(10 * MIN, 111.0, 105)]
        paper, st, _ = _run(candles, from_ms=1 * MIN, last=104.0)
        assert st.was_reset and paper.sold
        assert paper.sold[0][2] == pytest.approx(110.0), "a resting TP fills at its level"

    def test_a_quiet_sleep_changes_nothing(self, quiet):
        candles = [_c(10 * MIN, 102, 100.5), _c(40 * MIN, 103, 101)]
        paper, st, _ = _run(candles, from_ms=1 * MIN)
        assert not st.was_reset and not paper.sold and not quiet["closed"]

    def test_a_short_is_handled_symmetrically(self, quiet):
        candles = [_c(10 * MIN, 101.5, 99.5)]
        paper, st, _ = _run(candles, from_ms=1 * MIN, qty=-1.0,
                            stop_loss=101.0, take_profit=90.0)
        assert st.was_reset and paper.bought, "a short stopped out must be BOUGHT back"

    def test_it_replays_from_the_start_of_the_gap_not_from_the_entry(self, quiet):
        """Otherwise a long-held position re-reads old candles on every wake."""
        candles = [_c(2 * MIN, 100, 98.0), _c(50 * MIN, 102, 101)]     # old breach, then calm
        paper, st, ex = _run(candles, from_ms=30 * MIN, entry_time=None)
        assert ex.since_seen == 30 * MIN
        assert not st.was_reset, "an OLD breach, before the gap, was re-applied"


class TestStartupStillWorks:
    def test_default_from_ms_uses_the_saved_last_alive(self, quiet):
        """The startup path passes no from_ms and must behave exactly as before."""
        paper, st = _Paper(1.0), _State()
        paper.last_alive_ms = 5 * MIN
        ex = _Exchange([_c(10 * MIN, 100, 98.0)])
        B.catch_up_open_positions(paper, {"X/USD": st}, ["X/USD"], ex)
        assert ex.since_seen == 5 * MIN and st.was_reset

    def test_no_open_position_does_nothing(self, quiet):
        paper = _Paper(0.0)
        st = _State(state="IDLE")
        B.catch_up_open_positions(paper, {"X/USD": st}, ["X/USD"], _Exchange([]))
        assert not paper.sold and not paper.bought


class TestItIsWired:
    def _src(self):
        return (ROOT / "binance_bot.py").read_text()

    def test_startup_and_sleep_share_one_implementation(self):
        """A second copy of the replay-and-close logic is the repo's dominant bug shape:
        a rule on one path and not the other. The stop-fill slippage already had to be
        fixed in three separate copies."""
        src = self._src()
        assert src.count("def catch_up_open_positions") == 1
        assert src.count("indicators.first_protective_breach(") == 1, \
            "the replay logic has been duplicated"
        assert src.count("stop_fill_price(") >= 1

    def test_the_main_loop_checks_for_a_sleep_at_every_cycle_and_every_tick(self):
        src = self._src()
        assert src.count("_catch_up_if_machine_slept()") == 3, \
            "one definition + the cycle top + the 10s watcher tick"
        assert "SLEEP_GAP_SECONDS" in src

    def test_the_gap_threshold_clears_normal_operation(self):
        """Must be well above the 10s tick, or every tick would trigger a replay."""
        assert B.SLEEP_GAP_SECONDS >= 60
        assert B.SLEEP_GAP_SECONDS < B.SLEEP_SECONDS, \
            "a threshold at or above the 5m cycle would never see a sleep shorter than it"

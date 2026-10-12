"""The sleep catch-up must be EXECUTED by a test, not just compiled.

The first version lived as a closure inside run() and no test ever called it. Live, all 28 of
its runs crashed at `save_state(...)`, a name that does not exist in binance_bot.py (it was
copied from an older dashboard path without checking). The handler then printed "open stops were
NOT verified" although the replay had completed and only the save had died.

A second defect hid behind the first: a replay that FAILED (the network is routinely not back in
the first seconds after wake — 17 such failures are in the live log) was still marked as handled,
so that window of unwatched stops was silently written off.

Everything here drives SleepGuard with a fake clock and fake callbacks.
"""
import pytest

import binance_bot as B


@pytest.fixture(autouse=True, scope="module")
def _libs_loaded():
    assert B._libs_ready.wait(timeout=120)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Rig:
    """Records what the guard asked for."""
    def __init__(self, failures=(), open_position=True, save_raises=False, replay_raises=False):
        self.replays, self.saves = [], 0
        self.failures = list(failures)          # one entry per replay call: list of failed symbols
        self.open_position, self.save_raises, self.replay_raises = open_position, save_raises, replay_raises

    def replay(self, from_ms, gap):
        self.replays.append((from_ms, gap))
        if self.replay_raises:
            raise RuntimeError("boom")
        return self.failures.pop(0) if self.failures else []

    def save(self):
        self.saves += 1
        if self.save_raises:
            raise NameError("name 'save_state' is not defined")

    def any_open(self):
        return self.open_position


def _guard(clock, gap=150):
    return B.SleepGuard(gap, clock=clock)


def _check(g, rig):
    return g.check(rig.replay, rig.save, rig.any_open)


class TestNormalOperation:
    def test_a_ten_second_tick_does_nothing(self, capsys):
        c = Clock(); g = _guard(c); r = Rig()
        c.t += 10
        assert _check(g, r) == "ok" and r.replays == [] and r.saves == 0
        assert capsys.readouterr().out == ""

    def test_the_clock_advances_on_every_normal_tick(self):
        c = Clock(); g = _guard(c); r = Rig()
        c.t += 10; _check(g, r)
        assert g.last_wall == c.t

    def test_rearm_restarts_the_clock(self):
        c = Clock(); g = _guard(c)
        c.t += 140; g.rearm()
        c.t += 140
        assert _check(g, Rig()) == "ok", "140s after a rearm is not a sleep"


class TestAfterASleep:
    def test_an_open_position_is_replayed_from_the_start_of_the_gap_and_saved(self):
        c = Clock(); g = _guard(c); r = Rig()
        start = c.t
        c.t += 3600
        assert _check(g, r) == "replayed"
        assert r.replays == [(int(start * 1000), 3600)], "must replay from where the clock stopped"
        assert r.saves == 1 and g.last_wall == c.t

    def test_no_open_position_means_nothing_to_replay_but_the_clock_still_moves(self):
        c = Clock(); g = _guard(c); r = Rig(open_position=False)
        c.t += 3600
        assert _check(g, r) == "idle" and r.replays == [] and g.last_wall == c.t


class TestAFailedReplayIsNotWrittenOff:
    def test_a_failed_replay_keeps_the_window_and_does_not_advance(self):
        c = Clock(); g = _guard(c); r = Rig(failures=[["TAO/USD"]])
        start = c.t; c.t += 3600
        assert _check(g, r) == "retry"
        assert g.last_wall == start, "the window start moved even though the replay failed"
        assert r.saves == 0

    def test_the_retry_replays_the_ORIGINAL_window_and_then_clears_it(self):
        c = Clock(); g = _guard(c); r = Rig(failures=[["TAO/USD"], []])
        start = c.t; c.t += 3600; _check(g, r)
        c.t += 10
        assert _check(g, r) == "replayed"
        assert r.replays[1][0] == int(start * 1000), "retry must cover the whole unwatched window"
        assert g.last_wall == c.t and r.saves == 1

    def test_it_gives_up_loudly_after_max_retries(self, capsys):
        c = Clock(); g = _guard(c)
        r = Rig(failures=[["TAO/USD"]] * (B.SleepGuard.MAX_RETRIES + 1))
        c.t += 3600
        results = []
        for _ in range(B.SleepGuard.MAX_RETRIES + 1):
            results.append(_check(g, r)); c.t += 10
        assert results[:-1] == ["retry"] * B.SleepGuard.MAX_RETRIES and results[-1] == "gave_up"
        out = capsys.readouterr().out
        assert "GAVE UP" in out and "TAO/USD" in out and "NOT checked" in out
        assert g.last_wall != 1_000_000.0, "after giving up it must move on, not retry forever"

    def test_a_replay_that_raises_is_treated_as_a_failure_not_a_success(self):
        c = Clock(); g = _guard(c); r = Rig(replay_raises=True)
        start = c.t; c.t += 3600
        assert _check(g, r) == "retry" and g.last_wall == start


class TestTheSaveFailingIsReportedHonestly:
    def test_a_save_error_does_not_undo_a_completed_replay(self, capsys):
        """The live bug: NameError in save, and the message claimed the stops were unverified."""
        c = Clock(); g = _guard(c); r = Rig(save_raises=True)
        c.t += 3600
        assert _check(g, r) == "replayed"
        assert g.last_wall == c.t, "a failed SAVE must not cause the window to be replayed again forever"
        out = capsys.readouterr().out
        assert "replay itself completed" in out
        assert "NOT verified" not in out and "NOT checked" not in out


class TestCatchUpReportsWhatItCouldNotCheck:
    """catch_up_open_positions used to return nothing, so a failure looked like success."""
    class _St:
        state, entry_price, stop_loss, take_profit = "POSITION_OPEN", 100.0, 99.0, 110.0
        entry_time, breakeven_moved = None, False
        def reset(self): self.state = "IDLE"

    class _Paper:
        last_alive_ms = 0
        def get_position(self, s): return 1.0

    class _Ex:
        def __init__(self, ticker_raises=False, ohlcv_raises=False):
            self.t, self.o = ticker_raises, ohlcv_raises
        def fetch_ticker(self, _):
            if self.t: raise ConnectionError("network down")
            return {"last": 101.0}
        def fetch_ohlcv(self, *a, **k):
            if self.o: raise ConnectionError("network down")
            return []

    def _run(self, ex):
        return B.catch_up_open_positions(self._Paper(), {"X/USD": self._St()}, ["X/USD"], ex, from_ms=1)

    def test_a_clean_run_reports_nothing_failed(self):
        assert self._run(self._Ex()) == []

    def test_a_dead_network_is_reported(self):
        assert self._run(self._Ex(ticker_raises=True)) == ["X/USD"]

    def test_a_degraded_replay_is_reported(self):
        """Ticker worked, candles did not: it fell back to the current price, which can miss a stop."""
        assert self._run(self._Ex(ohlcv_raises=True)) == ["X/USD"]


class TestItIsWiredIn:
    def test_run_builds_a_guard_and_uses_the_real_save_function(self):
        import inspect
        src = inspect.getsource(B.run)
        assert "SleepGuard(" in src and "_sleep_guard.rearm()" in src
        assert "save_crypto_state(paper, states, symbols, prices, _daily_snap)" in src
        assert "save_state(" not in src.replace("save_crypto_state(", "")

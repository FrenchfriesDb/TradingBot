"""The research harness's simulator decides every conclusion drawn from it. Pin its rules.

tools/measure_hypotheses.py produced the 2026-10-09 finding that no simple strategy has an
edge on crypto or stocks. Until now its simulator was only checked INDIRECTLY (a random control
came out near zero). A bias in same-bar handling, exit timing or the stock session rules would
have moved every number silently, so each rule is asserted directly on hand-built bars.

Conventions in the fixtures: ATR is 1.0, entry is 100, k=2 -> risk 2, so the stop is 98 and
the 1:1 target is 102 for a long.
"""
import importlib.util
import pathlib

import numpy as np
import pandas as pd
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("measure_hypotheses", ROOT / "tools" / "measure_hypotheses.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(H, "MODE", "crypto")
    monkeypatch.setattr(H, "RR", 1.0)
    monkeypatch.setattr(H, "MAXBARS", 32)


def _df(bars):
    """bars: (open, high, low, close) per bar; bar 0 is the signal bar."""
    d = pd.DataFrame(bars, columns=["open", "high", "low", "close"])
    d["ts"] = np.arange(len(d)) * 3_600_000
    d["atr"] = 1.0
    return d


def _sig(n, **at):
    s = np.zeros(n, dtype=int)
    for k, v in at.items():
        s[int(k[1:])] = v
    return s


FLAT = (100, 100.5, 99.5, 100)


class TestExitRules:
    def test_a_long_that_reaches_the_target_wins_one_r(self):
        d = _df([FLAT, (100, 103, 99.5, 102), FLAT, FLAT])
        (_, r, kind, risk), = H.simulate(d, _sig(4, i0=1), 2.0)
        assert (r, kind) == (1.0, "T")
        assert risk == pytest.approx(0.02)             # 2 / 100

    def test_a_long_that_reaches_the_stop_loses_one_r(self):
        d = _df([FLAT, (100, 100.5, 97.5, 98), FLAT, FLAT])
        (_, r, kind, _), = H.simulate(d, _sig(4, i0=1), 2.0)
        assert (r, kind) == (-1.0, "S")

    def test_a_bar_containing_both_levels_counts_as_the_stop(self):
        """Intrabar order is unknowable from OHLC. Assuming the TARGET first would hand every
        strategy a free win on every wide bar."""
        d = _df([FLAT, (100, 103, 97, 100), FLAT, FLAT])
        (_, r, kind, _), = H.simulate(d, _sig(4, i0=1), 2.0)
        assert (r, kind) == (-1.0, "S")

    def test_a_short_mirrors_a_long(self):
        win = _df([FLAT, (100, 101, 97.5, 98), FLAT, FLAT])
        (_, r, kind, _), = H.simulate(win, _sig(4, i0=-1), 2.0)
        assert (r, kind) == (1.0, "T")
        lose = _df([FLAT, (100, 102.5, 99.5, 102), FLAT, FLAT])
        (_, r, kind, _), = H.simulate(lose, _sig(4, i0=-1), 2.0)
        assert (r, kind) == (-1.0, "S")

    def test_the_reward_ratio_moves_the_target(self, monkeypatch):
        monkeypatch.setattr(H, "RR", 2.0)
        d = _df([FLAT, (100, 103, 99.5, 102), (102, 104.5, 101, 104), FLAT])
        (_, r, kind, _), = H.simulate(d, _sig(4, i0=1), 2.0)
        assert (r, kind) == (2.0, "T"), "1:2 must wait for 104, not take 102"

    def test_a_timeout_exits_at_the_close_and_scores_the_fraction(self, monkeypatch):
        monkeypatch.setattr(H, "MAXBARS", 3)
        d = _df([FLAT, (100, 100.9, 99.5, 100.2), (100.2, 100.9, 99.5, 100.4),
                 (100.4, 100.9, 99.5, 100.6), (100.6, 150, 50, 100)])
        (_, r, kind, _), = H.simulate(d, _sig(5, i0=1), 2.0)
        assert kind == "X" and r == pytest.approx(0.3), "(100.6-100)/risk 2, and bar 4 must not be reached"

    def test_entry_is_the_NEXT_bars_open_not_the_signal_bars_close(self):
        d = _df([(100, 100.5, 99.5, 120), (101, 104, 100.5, 103), FLAT, FLAT])
        (_, r, kind, risk), = H.simulate(d, _sig(4, i0=1), 2.0)
        assert risk == pytest.approx(2 / 101), "entry must be 101 (bar 1 open), not 120 (bar 0 close)"


class TestOnePositionAtATime:
    def test_a_signal_while_a_trade_is_open_is_ignored(self):
        d = _df([FLAT, (100, 103, 99.5, 102), FLAT, FLAT, FLAT])
        assert len(H.simulate(d, _sig(5, i0=1, i1=1), 2.0)) == 1

    def test_a_signal_after_the_exit_trades_again(self):
        d = _df([FLAT, (100, 103, 99.5, 102), FLAT, (100, 103, 99.5, 102), FLAT, FLAT])
        assert len(H.simulate(d, _sig(6, i0=1, i2=1), 2.0)) == 2


class TestStockSession:
    """Regular session only, no entries in the last 30 minutes, flat by 15:45 ET."""

    def _stock(self, mods, closes, days=None):
        n = len(mods)
        d = _df([(100, 100.5, 99.5, c) for c in closes])
        d["mod"], d["day"] = mods, days if days is not None else [0] * n
        last_ok = d["mod"] <= 930
        d["daylast"] = d.index.to_series().where(last_ok).groupby(d["day"]).transform("max").ffill().astype(int)
        return d

    @pytest.fixture(autouse=True)
    def _stock_mode(self, monkeypatch):
        monkeypatch.setattr(H, "MODE", "stock")

    def test_it_is_flat_by_the_1545_bar_not_the_close(self):
        d = self._stock([885, 900, 915, 930, 945], [100, 100, 100, 101, 90])
        (_, r, kind, _), = H.simulate(d, _sig(5, i0=1), 2.0)
        assert kind == "X" and r == pytest.approx(0.5), "must exit on the 930 bar's close (101), not the 945 bar's (90)"

    def test_no_entry_on_a_bar_starting_after_15_15(self):
        d = self._stock([900, 915, 930, 945], [100, 100, 100, 100])
        assert H.simulate(d, _sig(4, i1=1), 2.0) == [], "entry bar would start at 15:30 — inside the cutoff"

    def test_an_entry_bar_starting_exactly_at_15_15_is_still_allowed(self):
        """The boundary, from the allowed side: signal on the 15:00 bar, entry bar 15:15."""
        d = self._stock([900, 915, 930, 945], [100, 100, 100, 100])
        assert len(H.simulate(d, _sig(4, i0=1), 2.0)) == 1

    def test_a_signal_on_the_last_bar_of_a_day_does_not_enter_tomorrow(self):
        d = self._stock([930, 945, 600, 615], [100, 100, 100, 100], days=[0, 0, 1, 1])
        assert H.simulate(d, _sig(4, i1=1), 2.0) == [], "overnight entry"

    def test_no_entry_in_the_first_bars_of_the_session(self):
        d = self._stock([570, 585, 600, 615], [100, 100, 100, 100])
        assert H.simulate(d, _sig(4, i0=1), 2.0) == [], "entry bar at 09:45 is before 10:00"


class TestStatistics:
    def test_tstat_of_a_constant_series_is_nan_not_infinite(self):
        assert np.isnan(H.tstat(pd.Series([1.0, 1.0, 1.0, 1.0])))

    def test_tstat_matches_the_textbook_formula(self):
        x = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        assert H.tstat(x) == pytest.approx(3.0 / (x.std(ddof=1) / np.sqrt(5)))

    def test_too_few_points_is_nan(self):
        assert np.isnan(H.tstat(pd.Series([1.0, 2.0])))


class TestTheSimulatorIsUnbiased:
    def test_random_entries_on_a_driftless_walk_earn_about_nothing(self):
        """The control arm's whole meaning. If this drifts away from zero, every strategy
        comparison made with the harness is measuring the simulator, not the strategy."""
        rng = np.random.default_rng(7)
        n = 30_000
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
        open_ = np.r_[close[0], close[:-1]]
        wick = np.abs(rng.normal(0, 0.002, n)) * close
        d = pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + wick,
                          "low": np.minimum(open_, close) - wick, "close": close, "volume": 1.0})
        d["ts"] = np.arange(n) * 3_600_000
        H.prep(d)
        sig = np.where(rng.random(n) < 0.02, rng.choice([-1, 1], n), 0)
        rows = H.simulate(d, sig, 2.0)
        R = np.array([r[1] for r in rows])
        assert len(R) > 300
        se = R.std(ddof=1) / np.sqrt(len(R))
        assert abs(R.mean()) < 4 * se, f"mean {R.mean():+.3f} is {abs(R.mean())/se:.1f} s.e. from zero"


class TestTheToolsStayOutOfTheRepo:
    TOOLS = ["measure_hypotheses.py", "fetch_research_crypto.py", "fetch_research_stocks.py"]

    @pytest.mark.parametrize("name", TOOLS)
    def test_no_hardcoded_home_directory(self, name):
        assert "/Users/" not in (ROOT / "tools" / name).read_text()

    def test_candles_are_cached_outside_the_repo(self):
        """~100MB of CSVs next to the scripts would be one careless `git add -A` away from
        being committed — and this repo gets committed with `-A` constantly."""
        cache = pathlib.Path(H.CACHE).expanduser().resolve()
        assert ROOT not in cache.parents and cache != ROOT

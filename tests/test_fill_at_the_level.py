"""A stop or target fills AT its level, never at wherever price has since travelled.

THE LIVE EVIDENCE (2026-09-17). Three paper positions closed at take-profit while the
Mac was asleep. The bot booked them like this:

    sym    take profit   recorded exit   booked    arithmetic max
    AVAX   $7.8040       $7.9320         $65.26    $45.06
    ADA    $0.2098       $0.2148         $73.63    $49.34
    SEI    $0.04522      $0.045825       $61.43    $41.82
                                         -------   -------
                                         $200.32   $136.23

"Arithmetic max" is qty x (take_profit - entry) — the most a position that size can
make by reaching its target. All three booked 1.47x that, because the machine slept for
5.5 hours, price ran 1.3-2.4% past target, and the bot filled at the price it saw when
it woke rather than at the target a resting order would have filled on hours earlier.

THE SPLIT THAT CAUSED IT. There are two exit watchers and only one was fixed. The
10-second watcher already says so in its own comment —

    # Fill at the level that was crossed (not the current last price)
    fill = st.stop_loss if sl_hit else st.take_profit

— while the 5-minute path detected the breach correctly off the candle wick and then
handed close_position() the CURRENT price. Same divergence this codebase keeps
producing: a fix lands on one path and the other quietly keeps the old behaviour.

WHY IT IS NOT MERELY OPTIMISTIC. It flatters the record in both directions. A stop
detected on a wick that price then recovered from books a SMALLER loss than a real stop
would have taken. A target reached and given back books a win that never existed. Both
corrupt the only record being used to decide whether the strategy works at all.
"""
import ast
import pathlib

import pytest

from bot.indicators import first_protective_breach

REPO = pathlib.Path(__file__).resolve().parent.parent


def _bar(high, low, ts=1000):
    """One ccxt OHLCV row: [ts, open, high, low, close, volume]."""
    return [ts, 0.0, high, low, 0.0, 0.0]


# ── the semantics the 5-minute path now relies on ─────────────────────────────

def test_a_target_overshoot_fills_at_the_target():
    """The AVAX case: target $7.804, the bar ran to $7.9320."""
    kind, fill, _ = first_protective_breach([_bar(high=7.9320, low=7.60)],
                                            stop=7.5176, target=7.8040, is_long=True)
    assert (kind, fill) == ("TARGET", 7.8040)


def test_a_stop_undershoot_fills_at_the_stop():
    """Mirror case: a gap straight through the stop does not fill at the gap's bottom."""
    kind, fill, _ = first_protective_breach([_bar(high=7.60, low=7.20)],
                                            stop=7.5176, target=7.8040, is_long=True)
    assert (kind, fill) == ("STOP", 7.5176)


def test_a_bar_that_touches_neither_closes_nothing():
    assert first_protective_breach([_bar(high=7.70, low=7.60)],
                                   stop=7.5176, target=7.8040, is_long=True) is None


def test_a_bar_that_spans_both_resolves_to_the_stop():
    """The sequence inside one bar is unknowable, so the simulation must not hand itself
    the better of two outcomes it cannot distinguish."""
    kind, fill, _ = first_protective_breach([_bar(high=7.95, low=7.20)],
                                            stop=7.5176, target=7.8040, is_long=True)
    assert (kind, fill) == ("STOP", 7.5176)


def test_the_short_side_mirrors():
    kind, fill, _ = first_protective_breach([_bar(high=7.60, low=7.20)],
                                            stop=7.90, target=7.30, is_long=False)
    assert (kind, fill) == ("TARGET", 7.30)


def test_the_three_live_trades_book_their_arithmetic_maximum_and_no_more():
    """Replays the actual positions. The point is the TOTAL: $136.23, not $200.32."""
    trades = [   # qty,       entry,   stop,    target,  recorded exit
        (226.457,    7.605,   7.5176,  7.8040,  7.9320),
        (6246.2014,  0.2019,  0.1990,  0.2098,  0.2148),
        (32416.5641, 0.04393, 0.0433,  0.04522, 0.045825),
    ]
    total = 0.0
    for qty, entry, stop, target, reached in trades:
        kind, fill, _ = first_protective_breach([_bar(high=reached, low=entry)],
                                                stop, target, is_long=True)
        assert kind == "TARGET"
        assert fill == target, "must fill at the target, not where price ran to"
        total += (fill - entry) * qty
    assert round(total, 2) == 136.23, total


# ── the wiring: the 5-minute path must pass a level, not the live price ───────

def test_both_protective_exits_pass_an_explicit_fill_level():
    """close_position() defaults to the live price, which is right for a STALE timeout
    (a market close really does fill wherever the market is) and wrong for a stop or a
    target (a resting order fills at its own level). So the SL and TP call sites have to
    say which level, explicitly — asserted structurally because the default is silent
    and its wrongness only shows up as money."""
    tree = ast.parse((REPO / "binance_bot.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "close_position"]
    assert calls, "close_position() is never called — did it get renamed?"

    protective = []
    for c in calls:
        first = c.args[0] if c.args else None
        text = ""
        if isinstance(first, ast.Constant):
            text = str(first.value)
        elif isinstance(first, ast.JoinedStr):
            text = "".join(p.value for p in first.values
                           if isinstance(p, ast.Constant) and isinstance(p.value, str))
        if "SL" in text or "TP" in text:
            protective.append((c.lineno, text, {k.arg for k in c.keywords}))

    assert len(protective) >= 2, f"expected an SL and a TP call site, found {protective}"
    missing = [(ln, t) for ln, t, kw in protective if "fill" not in kw]
    assert not missing, (
        "these protective exits fill at the live price instead of their own level: "
        + "; ".join(f"line {ln} ({t!r})" for ln, t in missing))


def test_the_stale_timeout_still_fills_at_the_market():
    """Not everything should fill at a level. A timeout crosses the book by choice."""
    tree = ast.parse((REPO / "binance_bot.py").read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and getattr(n.func, "id", None) == "close_position"
                and n.args and isinstance(n.args[0], ast.JoinedStr)):
            text = "".join(p.value for p in n.args[0].values
                           if isinstance(p, ast.Constant) and isinstance(p.value, str))
            if "Stale" in text:
                assert "fill" not in {k.arg for k in n.keywords}, (
                    "a stale timeout is a market close — pinning it to a level would "
                    "invent a fill the bot never got")
                return
    pytest.skip("no stale-timeout close site found")

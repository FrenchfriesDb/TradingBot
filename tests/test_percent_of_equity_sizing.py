"""Risk per trade is a PERCENTAGE of live equity, not a hardcoded dollar amount.

WHY THIS CHANGED. MAX_RISK_DOLLARS = 20.0 was calibrated against a $10,000 paper balance,
where it means 0.2% per trade. Pointed at the operator's real $100 Coinbase account the
SAME constant means:

    $20 of $100  =  20% per trade  ->  5 consecutive losses is the whole account

A fixed dollar amount is a different bet at every account size. The constant silently
changes meaning the moment the balance does.

Percent-of-equity fixes the meaning and buys two things dollars cannot: it COMPOUNDS as
the account grows, and it DE-RISKS in a drawdown without anyone intervening.

Default is 0.2%, which reproduces today's $20 on today's balance — this change is a
change of MECHANISM, not of risk appetite. Raising the percentage is a separate decision
that belongs after ~30 clean trades show positive expectancy.
"""
import pytest

from bot.indicators import meets_exchange_minimums, risk_budget


# ── the change is a no-op at today's numbers ──

def test_default_reproduces_todays_twenty_dollars():
    assert risk_budget(10_135.89, 0.002) == pytest.approx(20.27, abs=0.01)


def test_the_hundred_dollar_account_no_longer_risks_twenty_percent():
    budget = risk_budget(100.0, 0.002)
    assert budget == pytest.approx(0.20)
    assert budget / 100.0 == pytest.approx(0.002), "0.2% at ANY account size"


# ── the two properties dollars cannot give you ──

def test_it_compounds_as_the_account_grows():
    assert risk_budget(10_000, 0.002) == 20.0
    assert risk_budget(20_000, 0.002) == 40.0


def test_it_de_risks_in_a_drawdown():
    """Down 30% -> risk per trade drops 30% with no intervention."""
    assert risk_budget(7_000, 0.002) == pytest.approx(14.0)


# ── leverage independence: the point of keeping leverage configurable ──

def test_budget_is_identical_on_1x_spot_and_10x_perps():
    """Risk is (entry-stop) x qty. Leverage only changes posted margin, so the SAME
    constants must work on both venues."""
    entry, stop = 100.0, 98.0
    budget = risk_budget(10_000, 0.002)
    qty = budget / (entry - stop)
    assert qty == pytest.approx(10.0)
    for lev in (1, 3, 10):
        margin = qty * entry / lev
        assert qty * (entry - stop) == pytest.approx(budget), "risk unchanged by leverage"
        assert margin == pytest.approx(1000.0 / lev), "only the margin posted changes"


# ── clamps ──

def test_ceiling_is_a_circuit_breaker_for_a_bad_equity_read():
    assert risk_budget(1_000_000, 0.002, ceiling=50.0) == 50.0


def test_floor_keeps_a_tiny_account_from_computing_dust():
    assert risk_budget(50.0, 0.002, floor=1.0) == 1.0


@pytest.mark.parametrize("bad", [0, -1, None, float("nan"), "x"])
def test_bad_equity_returns_zero_rather_than_sizing_off_garbage(bad):
    assert risk_budget(bad, 0.002) == 0.0


def test_non_positive_pct_is_refused():
    assert risk_budget(10_000, 0) == 0.0
    assert risk_budget(10_000, -0.01) == 0.0


# ── exchange minimums ──

def test_an_order_that_clears_both_limits_passes():
    ok, _ = meets_exchange_minimums(qty=10.0, price=100.0, min_amount=1.0, min_cost=10.0)
    assert ok


def test_below_minimum_quantity_is_refused_not_rounded_up():
    """Rounding UP to the minimum silently breaks the risk cap that produced the number —
    a $20-risk order rounded to a $50-risk order is not the trade that was approved."""
    ok, why = meets_exchange_minimums(qty=0.4, price=100.0, min_amount=1.0)
    assert not ok
    assert "risk cap" in why


def test_below_minimum_notional_is_refused():
    ok, why = meets_exchange_minimums(qty=0.01, price=100.0, min_cost=10.0)
    assert not ok and "notional" in why


def test_a_hundred_dollar_account_trips_a_typical_minimum():
    """0.2% of $100 is $0.20 of risk; on a 2%-wide stop that is a ~$10 position."""
    budget = risk_budget(100.0, 0.002)
    entry, stop = 100.0, 98.0
    qty = budget / (entry - stop)
    ok, why = meets_exchange_minimums(qty, entry, min_cost=25.0)
    assert not ok, f"expected refusal, got {why}"


def test_absent_limits_are_not_constraints():
    assert meets_exchange_minimums(0.0001, 1.0)[0]


@pytest.mark.parametrize("qty,price", [(0, 100), (10, 0), (-0, 100), (None, 100)])
def test_degenerate_input_is_refused(qty, price):
    assert not meets_exchange_minimums(qty, price)[0]

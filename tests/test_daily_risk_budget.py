"""A daily cap expressed in DEPLOYED CAPITAL undoes risk-based sizing.

THE OPERATOR'S REQUEST, in their words: make risk "1% of your account is how much you
lose", not "1% is what you put into the trade".

size_position() already did exactly that, and had for a while:

    qty = (balance * risk_pct) / abs(entry - sl)

What silently converted it back was the cap applied immediately afterwards.
DAILY_ACCOUNT_PCT = 0.10 limited NOTIONAL — dollars deployed — to 10% of the account
per UTC day, summed across every trade and never released when one closed. On the
$5,000 paper balance that is $500 of notional for the entire day, against a 1% risk
budget of $50 per trade:

    stop    notional needed   after the cap   risk actually taken
    0.8%    $6,250            $500            $4.00
    1.2%    $4,167            $500            $6.00
    2.0%    $2,500            $500            $10.00
    5.0%    $1,000            $500            $25.00

So the tighter and better the stop, the smaller the real risk — the exact inversion
risk-based sizing exists to prevent. And because the budget is cumulative and never
released, the second trade of any day got whatever was left, which was usually nothing.

THE FIX IS TO CAP THE SAME THING THE SIZING MEASURES. A daily budget is a sane idea —
it stops a bad day compounding — but it has to be denominated in RISK, not in capital.
"No more than 3% of the account lost in one day" is three full-size trades at 1%,
whatever their stops happen to be, and it leaves position size alone.
"""
import pytest

from bot.indicators import daily_risk_remaining, trim_qty_to_risk


BAL = 5_000.0
DAILY = 0.03          # 3% of the account per day = three full-risk trades


# ── the budget ────────────────────────────────────────────────────────────────

def test_a_fresh_day_offers_the_whole_budget():
    assert daily_risk_remaining(0.0, BAL, DAILY) == 150.0


def test_three_full_risk_trades_use_it_up_exactly():
    """1% each against a 3% day. The point of denominating in risk: this arithmetic
    holds no matter how wide or tight the individual stops were."""
    assert daily_risk_remaining(50.0, BAL, DAILY) == 100.0
    assert daily_risk_remaining(100.0, BAL, DAILY) == 50.0
    assert daily_risk_remaining(150.0, BAL, DAILY) == 0.0


def test_a_fourth_trade_gets_nothing_rather_than_a_negative_budget():
    assert daily_risk_remaining(200.0, BAL, DAILY) == 0.0


def test_the_budget_follows_the_account_down():
    """After a losing run the day's allowance shrinks with the balance, instead of
    risking a fixed dollar amount against a smaller account."""
    assert daily_risk_remaining(0.0, 2_500.0, DAILY) == 75.0


# ── trimming a position to what is left ───────────────────────────────────────

def test_a_trade_that_fits_the_budget_is_untouched():
    qty, trimmed = trim_qty_to_risk(qty=100.0, entry=10.0, stop=9.5, risk_remaining=150.0)
    assert qty == 100.0 and trimmed is False


def test_a_trade_larger_than_the_remaining_budget_is_trimmed_to_fit():
    """$0.50 of risk per unit, $25 left -> 50 units, risking exactly the $25."""
    qty, trimmed = trim_qty_to_risk(qty=100.0, entry=10.0, stop=9.5, risk_remaining=25.0)
    assert qty == 50.0 and trimmed is True
    assert qty * abs(10.0 - 9.5) == 25.0


def test_an_exhausted_budget_returns_nothing_to_trade():
    assert trim_qty_to_risk(100.0, 10.0, 9.5, 0.0) == (0.0, True)
    assert trim_qty_to_risk(100.0, 10.0, 9.5, -5.0) == (0.0, True)


def test_a_zero_width_stop_is_refused_rather_than_dividing_by_zero():
    """A stop at the entry is not a free unlimited position; it is a broken setup."""
    assert trim_qty_to_risk(100.0, 10.0, 10.0, 150.0) == (0.0, True)


def test_the_trim_never_rounds_the_risk_upward():
    """Floor, not round: a trade must never end up risking a cent more than the budget
    allows, or the day's cap is not a cap."""
    qty, _ = trim_qty_to_risk(qty=100.0, entry=10.0, stop=9.97, risk_remaining=1.0)
    assert qty * abs(10.0 - 9.97) <= 1.0 + 1e-9


def test_a_short_is_measured_the_same_way():
    qty, trimmed = trim_qty_to_risk(qty=100.0, entry=10.0, stop=10.5, risk_remaining=25.0)
    assert qty == 50.0 and trimmed is True


@pytest.mark.parametrize("bad", [None, "x"])
def test_unreadable_input_refuses_the_trade_rather_than_guessing(bad):
    assert trim_qty_to_risk(bad, 10.0, 9.5, 150.0) == (0.0, True)


# ── the behaviour the operator actually asked for ─────────────────────────────

def test_risk_taken_no_longer_depends_on_how_tight_the_stop_is():
    """The whole complaint in one test. Under the old NOTIONAL cap a tighter stop meant
    LESS money at risk; under a risk budget every setup risks the same 1%, and the stop
    decides position size instead of decides risk."""
    risk_budget = BAL * 0.01                      # $50 intended per trade
    taken = []
    for stop_pct in (0.008, 0.012, 0.020, 0.050):
        entry = 100.0
        stop = entry * (1 - stop_pct)
        qty = risk_budget / (entry - stop)        # what size_position() computes
        sized, _ = trim_qty_to_risk(qty, entry, stop, daily_risk_remaining(0.0, BAL, DAILY))
        taken.append(round(sized * (entry - stop), 2))
    assert taken == [50.0, 50.0, 50.0, 50.0], taken


# ── risk is a share of the ACCOUNT, not of whatever cash is left ──────────────
#
# Live on 2026-09-19, two trades in a row:
#
#   ADA   risk $50.00  (1.00% of the $5,000 account)   <- first trade, full size
#   DOGE  risk $27.44  (0.55%)                          <- second trade, half size
#
# The daily budget was not the cause: $100 of the $150 was still unspent. The cause was
# the call site, size_position(paper.balance, ...), because paper.balance is FREE CASH
# and ADA had locked $2,255.96 of it as margin:
#
#   account equity             $5,000.00  -> 1% = $50.00
#   free cash after ADA        $2,744.04  -> 1% = $27.44   <- exactly what was risked
#
# So every additional position risked 1% of what was LEFT, and a third would have risked
# less again. "1% of your account is how much you lose" has to mean 1% of the account
# every time, or position size quietly depends on how many trades happen to be open.
#
# The capital cap is a different question and still belongs on free cash: you cannot
# deploy money that is already committed, whatever your equity says. Hence two inputs.
from test_bot import size_position, MAX_MARGIN_PCT, TEST_LEVERAGE


def test_risk_is_a_share_of_equity_not_of_the_cash_left_over():
    """The live DOGE case, to the cent."""
    equity, free_cash = 5_000.0, 2_744.04
    qty = size_position(equity, 0.01, entry=0.08509, sl=0.08341, cash_available=free_cash)
    assert qty * (0.08509 - 0.08341) == pytest.approx(50.0, abs=0.01)


def test_a_second_position_is_sized_the_same_as_the_first():
    """The property that was broken: size must not depend on how many trades are open."""
    first = size_position(5_000.0, 0.01, entry=10.0, sl=9.5, cash_available=5_000.0)
    second = size_position(5_000.0, 0.01, entry=10.0, sl=9.5, cash_available=2_744.04)
    assert first * 0.5 == pytest.approx(second * 0.5), "same risk either way"


def test_deployment_still_cannot_exceed_the_cash_actually_available():
    """Equity sets the RISK; free cash still caps the CAPITAL. Sizing off equity alone
    would deploy money that is already committed to another position."""
    qty = size_position(5_000.0, 0.01, entry=60_000, sl=59_880, cash_available=1_000.0)
    assert qty * 60_000 <= 1_000.0 * MAX_MARGIN_PCT * TEST_LEVERAGE + 1e-6


def test_omitting_cash_available_keeps_the_old_single_basis_behaviour():
    """Back-compat: the flat account case, where equity and cash are the same number."""
    a = size_position(5_000.0, 0.01, entry=60_000, sl=59_880)
    b = size_position(5_000.0, 0.01, entry=60_000, sl=59_880, cash_available=5_000.0)
    assert a == b


def test_a_drawdown_shrinks_the_risk_because_equity_shrank():
    """Sizing off equity is not sizing off a fixed number — a smaller account risks less."""
    full = size_position(5_000.0, 0.01, entry=10.0, sl=9.0, cash_available=5_000.0)
    down = size_position(4_000.0, 0.01, entry=10.0, sl=9.0, cash_available=4_000.0)
    assert down == pytest.approx(full * 0.8, rel=1e-6)

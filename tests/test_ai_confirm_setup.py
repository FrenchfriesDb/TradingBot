"""One AI confirmation call, shared, that cannot invent an approval.

test_bot had no AI at all — zero call sites, while binance_bot.py had five and
bot/strategy.py six. Wiring a third private copy of the prompt-and-parse machinery is
how the other two ended up with the same bug in two places (the fail-open on an
unparsed reply, fixed 2026-09-18 in both), so this is the shared version.

THE RULES IT ENCODES, which are the ones that cost real trades to learn:

  * the model said NO            -> no trade.
  * the model said YES           -> trade.
  * the model SPOKE but we could not find a decision -> NO TRADE. That is content, not
    infrastructure, and on 2026-09-18 it was content reasoning its way toward NO while
    the bot read it as a yes and put BTC on at $77,214.
  * the model never spoke at all (timeout, DNS, 500) -> proceed on technicals, because a
    setup that already passed every other gate should not die because a server was slow
    — but the reason says so, and is_no_opinion() makes the log print "⚠️ NO OPINION"
    instead of a green tick nobody earned.

The transport is injected so this is testable without a network, and so the caller keeps
control of its own timeouts.
"""
import pytest

from bot.ai_model import confirm_setup, is_no_opinion

CTX = {"symbol": "ADA/USD", "direction": "LONG", "entry": 0.2291,
       "stop": 0.2265, "target": 0.2350, "rr": 2.3, "trend": "UP",
       "pattern": "indecision then momentum"}


def _says(text):
    return lambda prompt: text


# ── the model's verdict is honoured ───────────────────────────────────────────

def test_a_yes_opens_the_trade():
    ok, rr, reason = confirm_setup(CTX, call=_says("DECISION: YES\nRR: 3.2\nREASON: clean"))
    assert ok is True and rr == 3.2
    assert not is_no_opinion(reason)


def test_a_no_refuses_it():
    ok, _, reason = confirm_setup(CTX, call=_says("DECISION: NO\nRR: 2\nREASON: mid-range"))
    assert ok is False
    assert "mid-range" in reason


# ── the failure that cost a trade ─────────────────────────────────────────────

def test_an_unreadable_reply_refuses_rather_than_approving():
    """Verbatim shape of the BTC reply that opened a position at $77,214."""
    ok, _, reason = confirm_setup(CTX, call=_says(
        "We need to analyze the 4H chart data and decide if a genuine liquidity sweep"))
    assert ok is False
    assert is_no_opinion(reason)


@pytest.mark.parametrize("junk", ["", "   ", "thinking...", "maybe?"])
def test_every_unreadable_shape_refuses(junk):
    assert confirm_setup(CTX, call=_says(junk))[0] is False


# ── infrastructure failure is different, and must be labelled ─────────────────

def test_a_transport_failure_proceeds_but_never_claims_an_approval():
    def boom(prompt):
        raise ConnectionError("no route to host")
    ok, rr, reason = confirm_setup(CTX, call=boom)
    assert ok is True, "a slow server must not kill a setup that passed every other gate"
    assert is_no_opinion(reason), "but it may never print as a green approval"


def test_a_missing_api_key_is_treated_as_no_opinion_not_as_a_yes():
    ok, _, reason = confirm_setup(CTX, call=None, api_key="")
    assert is_no_opinion(reason)


# ── the numbers it hands back ─────────────────────────────────────────────────

def test_the_suggested_rr_is_clamped_to_the_callers_bounds():
    hi = confirm_setup(CTX, call=_says("DECISION: YES\nRR: 99"), min_rr=2.0, max_rr=15.0)[1]
    lo = confirm_setup(CTX, call=_says("DECISION: YES\nRR: 0.1"), min_rr=2.0, max_rr=15.0)[1]
    assert hi == 15.0 and lo == 2.0


def test_a_missing_rr_falls_back_to_the_floor_without_losing_the_decision():
    ok, rr, _ = confirm_setup(CTX, call=_says("DECISION: YES\nREASON: fine"), min_rr=2.0)
    assert ok is True and rr == 2.0


# ── the prompt actually carries the setup ─────────────────────────────────────

def test_the_prompt_contains_the_facts_the_model_needs_to_judge():
    seen = {}
    def capture(prompt):
        seen["p"] = prompt
        return "DECISION: NO"
    confirm_setup(CTX, call=capture)
    p = seen["p"]
    # 0.2350 reprs as "0.235" — same number. Assert on the value, not the typing.
    for needle in ("ADA/USD", "LONG", "0.2291", "0.2265", "0.235", "UP"):
        assert needle in p, f"prompt is missing {needle!r}"
    assert "indecision then momentum" in p, "the sweep confirmation must reach the model"


def test_the_prompt_asks_for_the_decision_first():
    """A reasoning model truncated before its verdict is what created the fail-open.
    Asking for the answer on line one is what makes a short reply still parseable."""
    seen = {}
    confirm_setup(CTX, call=lambda p: seen.setdefault("p", p) and "DECISION: NO")
    assert "FIRST LINE" in seen["p"].upper() or "first line" in seen["p"]


# ── a truncated reply is not a verdict, even when a decision appears in it ────
#
# Measured against the live model on 2026-09-18, same prompt, three budgets:
#
#   max_tokens= 400  finish=length  parsed=NO   last line: "RR: we still need to give..."
#   max_tokens= 900  finish=stop    parsed=NO   last line: "REASON: The stop is too tight..."
#   max_tokens=1600  finish=stop    parsed=NO   last line: "REASON: The tight stop is..."
#
# At 400 the model was cut off mid-reasoning and the NO was an ACCIDENT — matched out of
# deliberation text, not an answer. It happened to fail closed, which is the safe
# direction, but a coin-flip that lands safe is still a coin flip. When the transport can
# tell us the reply was truncated, that is decisive: no verdict was reached.

def test_a_truncated_reply_is_refused_even_if_it_contains_a_decision():
    truncated = lambda p: ("...deliberating... DECISION: YES might be right if", "length")
    ok, _, reason = confirm_setup(CTX, call=truncated)
    assert ok is False
    assert "truncat" in reason.lower()


def test_a_complete_reply_is_trusted():
    complete = lambda p: ("DECISION: YES\nRR: 3\nREASON: clean", "stop")
    assert confirm_setup(CTX, call=complete)[0] is True


def test_a_caller_returning_plain_text_still_works():
    """The transport may not report a finish reason; absence is not truncation."""
    assert confirm_setup(CTX, call=lambda p: "DECISION: YES\nRR: 3")[0] is True

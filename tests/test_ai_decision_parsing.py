"""An AI answer nobody could read is not an approval.

WHAT HAPPENED (2026-09-18, BTC @ $77,214). The log said:

    [BTC] 🤖 AI Bot Approval: ✅ YES ... (unparsed AI response, defaulting approve)
    We need to analyze the 4H chart data and decide if a genuine liquidity sweep
    occurred at $76,182.

That is not an approval. It is the model's opening sentence, cut off mid-thought,
followed by the bot deciding on its behalf. Both bots did this — binance_bot.py:1375
set `confirm = True` when the DECISION line was missing, and bot/strategy.py:214 did
the same one file over.

THE ROOT CAUSE UNDER THE ROOT CAUSE. Both called the model with max_tokens=120. The
configured model is a REASONING model: it spends its budget thinking before it answers,
so it was being truncated before it ever reached the "DECISION:" line it was asked to
emit. The parse did not fail occasionally — it was structurally guaranteed to fail
whenever the model reasoned for more than ~120 tokens, and the bot read every one of
those as a yes.

THE DISTINCTION THIS FILE ENCODES. Two different failures were being treated the same:

  * the model never spoke    (timeout, DNS, 500) — the AI has no opinion, and a setup
    that already passed every technical filter should not die because a server was
    slow. Proceed, but SAY that no opinion was obtained.

  * the model spoke and we could not find a decision in it — that is content, not
    infrastructure, and the content may well have been reasoning toward NO. It was, in
    the BTC case: the model was working through whether the sweep was genuine. Treating
    that as a yes invents an approval nobody gave.

So an unreadable answer is now NOT_A_DECISION, and NOT_A_DECISION does not open trades.
"""
import pytest

from bot.ai_model import parse_ai_decision


# ── the exact live failure ────────────────────────────────────────────────────

BTC_TRUNCATED = ("We need to analyze the 4H chart data and decide if a genuine "
                 "liquidity sweep occurred at $76,182.")


def test_the_btc_reply_is_not_a_decision():
    decision, _, _ = parse_ai_decision(BTC_TRUNCATED)
    assert decision is None


def test_a_truncated_reply_is_never_reported_as_yes():
    """The whole defect in one line: this used to come back as an approval."""
    assert parse_ai_decision(BTC_TRUNCATED)[0] != "YES"


# ── well-formed replies ───────────────────────────────────────────────────────

def test_the_requested_format_parses():
    decision, rr, reason = parse_ai_decision(
        "DECISION: YES\nRR: 3.5\nREASON: clean sweep and displacement off the 4H low")
    assert decision == "YES"
    assert rr == 3.5
    assert reason.startswith("clean sweep")


def test_a_no_is_carried_through_as_a_no():
    decision, _, reason = parse_ai_decision(
        "DECISION: NO\nRR: 2.0\nREASON: price is mid-range with no liquidity taken")
    assert decision == "NO"
    assert "mid-range" in reason


@pytest.mark.parametrize("raw", ["decision: yes", "DECISION:YES", "Decision:  Yes  ",
                                 "**DECISION:** YES", "DECISION: YES."])
def test_formatting_noise_does_not_lose_the_decision(raw):
    """Models bold things, drop spaces and add punctuation. None of that is a refusal,
    and reading it as one would silently stop the bot trading."""
    assert parse_ai_decision(raw + "\nRR: 4\nREASON: ok")[0] == "YES"


def test_a_reasoning_block_before_the_answer_is_ignored():
    """Reasoning models emit their scratchpad first. The answer is what counts."""
    decision, _, _ = parse_ai_decision(
        "<think>the 4H low at 76,182 was swept, then displaced up...</think>\n"
        "DECISION: YES\nRR: 3.8\nREASON: swept then displaced")
    assert decision == "YES"


def test_the_last_decision_wins_when_a_model_restates_itself():
    """A model that thinks out loud often writes 'DECISION: NO... actually DECISION: YES'.
    The final answer is the answer."""
    decision, _, _ = parse_ai_decision(
        "First pass DECISION: NO\nOn reflection the sweep is clean.\nDECISION: YES\nRR: 4")
    assert decision == "YES"


def test_a_bare_yes_on_its_own_line_still_counts():
    """Worth accepting: it is unambiguous, and rejecting it would veto a real answer."""
    assert parse_ai_decision("YES")[0] == "YES"
    assert parse_ai_decision("NO")[0] == "NO"


def test_yes_buried_in_prose_is_not_a_bare_answer():
    """"...whether this is a yes depends on..." is not a decision. Only an unambiguous
    standalone answer counts, or we are back to inventing approvals."""
    assert parse_ai_decision(
        "It is hard to say whether this is a yes or a no on current structure.")[0] is None


# ── degenerate input ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("junk", [None, "", "   ", "\n\n", 12345, b"DECISION: YES"])
def test_junk_is_not_a_decision(junk):
    assert parse_ai_decision(junk)[0] is None


def test_an_unreadable_rr_does_not_take_the_decision_down_with_it():
    """A missing RR is recoverable — the caller clamps to its own floor. A missing
    DECISION is not. They must fail independently."""
    decision, rr, _ = parse_ai_decision("DECISION: YES\nRR: banana\nREASON: fine")
    assert decision == "YES"
    assert rr is None


def test_the_reason_falls_back_to_the_raw_text_so_the_log_still_shows_something():
    _, _, reason = parse_ai_decision("DECISION: NO\nRR: 2")
    assert reason


def test_a_decision_with_no_reason_is_still_a_decision():
    assert parse_ai_decision("DECISION: NO")[0] == "NO"


# ── never print an approval the model did not give ────────────────────────────
#
# Both bots fail OPEN on infrastructure failure — timeout, no API key, network — and
# that is a defensible choice: a setup that already passed every technical filter should
# not die because a server was slow. What is NOT defensible is printing
#
#     🤖 AI Bot Approval: ✅ YES
#
# over the top of it, which is what the eye actually reads. binance_bot.py already had
# an ad-hoc guard for this, matching "AI SKIPPED" and "unavailable" — and it missed the
# timeout path, whose reason says "proceeding on technicals". One tested predicate, used
# by both callers, instead of two string checks that drift apart.

from bot.ai_model import is_no_opinion


@pytest.mark.parametrize("reason", [
    "AI timeout (25s) — proceeding on technicals",
    "AI unavailable (ConnectionError) — proceeding on technicals",
    "no AI key — proceeding on technicals",
    "AI SKIPPED — model resolution failed",
    "AI gave no readable decision — standing aside (We need to analyze...)",
])
def test_every_fallback_reason_is_recognised_as_no_opinion(reason):
    assert is_no_opinion(reason)


@pytest.mark.parametrize("reason", [
    "clean sweep and displacement off the 4H low",
    "price is mid-range with no liquidity taken",
    "structure supports continuation to the 4H pool",
])
def test_a_real_verdict_is_not_mistaken_for_a_fallback(reason):
    assert not is_no_opinion(reason)


@pytest.mark.parametrize("junk", [None, "", 123])
def test_junk_counts_as_no_opinion_rather_than_as_approval(junk):
    """Fail toward 'we do not know', never toward 'the model said yes'."""
    assert is_no_opinion(junk)


# ── the fail-open hiding in the PROMPT ────────────────────────────────────────
#
# Found 2026-09-18 by smoke-testing the live model. Every prompt in this repo asked for:
#
#     DECISION: YES or NO
#
# and a model that echoes or reasons about that template emits that exact line back. The
# parser then matches "DECISION: YES" out of the echoed instruction and reports an
# approval the model never gave — the same bug as the unparsed-reply fail-open, one layer
# further out, and invisible because the parser was behaving correctly on the text it was
# handed. Seen live: a reply that got as far as "REASON: one concise sentence. Must be on
# first line? Actually they say..." was being scored as a verdict.
#
# The template now writes the placeholder as <YES or NO>, which cannot self-match, and
# test_no_prompt_can_match_its_own_template keeps it that way in every bot.

def test_an_echoed_template_is_not_a_decision():
    echoed = ("Reply in EXACTLY this format, one field per line, nothing else:\n"
              "DECISION: <YES or NO>\nRR: a number\nREASON: one concise sentence")
    assert parse_ai_decision(echoed)[0] is None


def test_a_model_musing_about_the_format_is_not_a_decision():
    """Verbatim shape of the live reply that exposed this."""
    musing = ("REASON: one concise sentence. Must be on first line? Actually they say "
              "answer on the very first line, so I should put DECISION: <YES or NO> there")
    assert parse_ai_decision(musing)[0] is None


def test_no_prompt_in_the_repo_can_match_its_own_template():
    """Any prompt whose literal text parses as a decision is a fail-open waiting for a
    model to echo it. Checked across every bot, not just the one that was caught."""
    import pathlib
    repo = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for name in ("bot/ai_model.py", "binance_bot.py", "bot/strategy.py"):
        src = (repo / name).read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            # Source COMMENTS may quote a decision while explaining the parser — a model
            # cannot echo those. Only text a model could actually be shown counts.
            if stripped.startswith("#"):
                continue
            if "DECISION" in line and parse_ai_decision(line)[0] is not None:
                offenders.append(f"{name}: {stripped[:70]}")
    assert not offenders, (
        "these prompt/template lines parse as a real decision, so a model echoing them "
        "would be read as a verdict:\n  " + "\n  ".join(offenders))

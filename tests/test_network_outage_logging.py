"""A DNS outage must cost ONE log line per minute, not eight hundred.

WHAT THE OPERATOR SAW. On 2026-09-16 the Mac lost DNS twice (07:44 → 11:37, then
13:01). Both times the stock bot's log filled with material that reads exactly like a
fatal crash:

  * ~200-line nested tracebacks headed `Job "On Trading Iteration Main Thread ..."
    raised an exception`, repeated for every failed Alpaca call, and
  * `Executing the on_bot_crash event method` at ~5 lines per SECOND — 100+ copies in
    22 seconds — because lumibot's run loop retries a crashed session with no backoff
    (strategy_executor.py:2205-2216, no sleep on the exception path).

Nothing was actually wrong. The bot resumes on its own when DNS returns.

WHY THE EXISTING SUPPRESSION MISSED BOTH. tradingbot.py already collapses network
noise, and the log proves it fired — two `🌐 Network unreachable` lines are right
there. It matched on `record.getMessage()` alone, which is the whole defect:

  * APScheduler logs `logger.exception('Job "%s" raised an exception', job)`. The
    MESSAGE names the job and nothing else; the DNS failure lives in `exc_info`. No
    sign in the message -> record passes -> the traceback prints in full.
  * `Executing the on_bot_crash event method` contains no network text at all, because
    it is a CONSEQUENCE of the outage rather than a report of it.

THE LINE THIS DRAWS. Suppressing a consequence line is only safe while an outage is
known to be in progress. `on_bot_crash` also fires for genuine bugs, and a genuine bug
during an outage must still be visible — so the verdict is time-scoped, and anything
that is not recognisably network noise passes through untouched. Those two properties
are what the tests below mostly exist to pin down.
"""
import logging

import pytest

from bot.log_filters import (NETWORK_ERROR_SIGNS, CollapseNetworkTracebackFilter,
                             network_outage_verdict)


DNS_FAILURE = (
    "HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries "
    "exceeded with url: /v2/positions (Caused by NewConnectionError('<urllib3."
    "connection.HTTPSConnection object at 0x11bdd1f90>: Failed to establish a new "
    "connection: [Errno 8] nodename nor servname provided, or not known'))")


def _apscheduler_record():
    """The record APScheduler emits: job name in the message, outage in exc_info."""
    try:
        raise ConnectionError(DNS_FAILURE)
    except ConnectionError:
        import sys
        return logging.LogRecord(
            "apscheduler.executors.base", logging.ERROR, "base.py", 145,
            'Job "%s" raised an exception', ("On Trading Iteration Main Thread",),
            sys.exc_info())


def _plain(msg, name="lumibot.DebbieLaSMC", level=logging.INFO):
    return logging.LogRecord(name, level, "strategy_executor.py", 1199, msg, (), None)


# ── the pure verdict ──────────────────────────────────────────────────────────

def test_error_hidden_in_the_traceback_is_still_recognised():
    """The defect: the message names a job, so message-only matching saw nothing."""
    assert network_outage_verdict('Job "x" raised an exception', DNS_FAILURE) == "report"


def test_first_outage_line_is_reported_not_dropped():
    """One honest line must survive, or an outage becomes invisible."""
    assert network_outage_verdict(DNS_FAILURE, outage_active=False) == "report"


def test_repeat_inside_the_window_is_suppressed():
    assert network_outage_verdict(DNS_FAILURE, outage_active=True) == "suppress"


def test_crash_hook_is_suppressed_only_while_an_outage_is_live():
    line = "Executing the on_bot_crash event method"
    assert network_outage_verdict(line, outage_active=True) == "suppress"
    # No outage in progress -> this is a REAL crash and must never be hidden.
    assert network_outage_verdict(line, outage_active=False) == "pass"


def test_a_genuine_error_during_an_outage_still_gets_through():
    """The window suppresses outage fallout, not everything that happens to co-occur."""
    assert network_outage_verdict("KeyError: 'entry_price'", outage_active=True) == "pass"
    assert network_outage_verdict(
        "[MSFT] 🚫 No fresh displacement at tap", outage_active=True) == "pass"


def test_broker_cash_failures_are_outage_fallout():
    for line in ("Unable to get cash from broker, trying again.",
                 "Unable to get the cash balance after 3 tries; leaving last known cash"):
        assert network_outage_verdict(line, outage_active=True) == "suppress"
        assert network_outage_verdict(line, outage_active=False) == "pass"


@pytest.mark.parametrize("sign", NETWORK_ERROR_SIGNS)
def test_each_known_signature_is_matched(sign):
    """Driven off the real tuple, so a signature added later cannot go untested.

    Carried on an APScheduler-shaped record, which is recognised as transport by its
    fixed phrase — so the SIGNATURE is the only thing left deciding the verdict."""
    assert network_outage_verdict('Job "x" raised an exception', f"boom: {sign}") == "report"


def test_the_scheduler_rebuild_notice_is_outage_fallout():
    """_on_bot_crash -> gracefully_exit() nulls the scheduler; the next pass announces
    it is rebuilding one. Noise during an outage, real news outside one."""
    line = "⚠️ Scheduler is None, attempting to recreate"
    assert network_outage_verdict(line, outage_active=True) == "suppress"
    assert network_outage_verdict(line, outage_active=False) == "pass"


# ── the filter wired to a clock ───────────────────────────────────────────────

class _Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def test_the_apscheduler_traceback_is_stripped_not_merely_shortened():
    """exc_info AND the cached exc_text both have to go — a handler that already
    formatted the record will happily reprint from the cache otherwise."""
    f = CollapseNetworkTracebackFilter(clock=_Clock())
    rec = _apscheduler_record()
    assert f.filter(rec) is True, "the outage itself must be reported once"
    assert rec.exc_info is None
    assert rec.exc_text is None
    assert "Network unreachable" in rec.getMessage()


def test_the_hundred_line_crash_flood_collapses_to_one_line():
    """Replays the 13:01:29 burst: one network error, then 100 crash-hook lines."""
    clock = _Clock()
    f = CollapseNetworkTracebackFilter(clock=clock)
    assert f.filter(_plain(DNS_FAILURE, level=logging.ERROR)) is True
    survivors = 0
    for _ in range(100):
        clock.t += 0.18          # the observed ~180ms spin
        if f.filter(_plain("Executing the on_bot_crash event method")):
            survivors += 1
    assert survivors == 0


def test_a_long_outage_reports_once_per_window_so_it_stays_visible():
    clock = _Clock()
    f = CollapseNetworkTracebackFilter(quiet_window_secs=60, clock=clock)
    reported = 0
    for _ in range(600):                     # 10 minutes at one failure/second
        clock.t += 1.0
        if f.filter(_plain(DNS_FAILURE, level=logging.ERROR)):
            reported += 1
    assert reported == 10, "one line per minute of continuous outage"


def test_normal_logging_is_untouched_when_nothing_is_wrong():
    f = CollapseNetworkTracebackFilter(clock=_Clock())
    rec = _plain("[MSFT] state=ENTRY_WAIT price=493.7650")
    assert f.filter(rec) is True
    assert rec.getMessage() == "[MSFT] state=ENTRY_WAIT price=493.7650"


def test_a_record_that_cannot_be_formatted_fails_open():
    """A broken record must never cost us a real message."""
    f = CollapseNetworkTracebackFilter(clock=_Clock())
    assert f.filter(logging.LogRecord("x", logging.ERROR, "f.py", 1,
                                      "%d apples", ("not-an-int",), None)) is True


# ── the line this filter must NEVER be allowed to eat ─────────────────────────
#
# Found by adversarial review of the first version of this fix, which WOULD have eaten
# both of the lines below. The bot composes its own operational warnings by
# interpolating the transport exception into a sentence:
#
#   bot/strategy.py:656  f"[{symbol}] ⚠️ Couldn't verify/re-attach protection: {e}"
#   bot/strategy.py:594  f"[{symbol}] ⚠️ Could not verify broker state after a failed "
#                        f"bracket ({e}) — assuming the order landed, NOT re-sending."
#
# During an outage `e` is a requests ConnectionError, so the MESSAGE contains
# "Max retries exceeded" and matched NETWORK_ERROR_SIGNS. Collapsing it replaced
#
#   "this position may be NAKED"        with        "no action needed"
#
# on a bot holding real money. The direction of error matters more than the noise:
# a missed suppression costs the operator a few lines of log, a wrong suppression
# costs them an unprotected position they were told not to worry about. So anything
# in the bot's OWN voice — anything carrying a [TICKER] tag or a safety marker — is
# checked FIRST and never collapsed, whatever else it happens to contain.

OUTAGE_EXC = "Max retries exceeded with url: /v2/orders (Caused by NewConnectionError)"


def test_a_failed_stop_loss_attach_is_never_collapsed():
    msg = f"[MSFT] ⚠️ Couldn't verify/re-attach protection: {OUTAGE_EXC}"
    assert network_outage_verdict(msg, outage_active=True) == "pass"
    assert network_outage_verdict(msg, outage_active=False) == "pass"


def test_the_duplicate_fill_guards_assumption_is_never_collapsed():
    msg = (f"[GOOGL] ⚠️ Could not verify broker state after a failed bracket "
           f"({OUTAGE_EXC}) — assuming the order landed, NOT re-sending.")
    assert network_outage_verdict(msg, outage_active=True) == "pass"


def test_a_naked_position_report_is_never_collapsed():
    msg = f"[TSLA] 🛡 Re-attached protection — position was NAKED, posted GTC OCO"
    assert network_outage_verdict(msg, outage_active=True) == "pass"
    assert network_outage_verdict(
        f"[AAPL] ⚠️ _ensure_protection has no sl/tp to work with {OUTAGE_EXC}",
        outage_active=True) == "pass"


def test_any_symbol_tagged_line_survives_even_carrying_transport_text():
    """Structural, so a safety line added to strategy.py LATER is covered without
    anyone remembering to update a list here."""
    for msg in (f"[SPY] ⚠️ some future safety warning: {OUTAGE_EXC}",
                f"[QQQ] HTF error: {OUTAGE_EXC}",
                f"[STATE] save failed: {OUTAGE_EXC}"):
        assert network_outage_verdict(msg, outage_active=True) == "pass", msg


def test_the_ansi_colour_wrapper_does_not_defeat_the_check():
    """log_message(color="red") wraps the text in escape codes before it ever reaches
    a filter — \\x1b[31m contains a '[' and must not be mistaken for a ticker tag,
    and must not hide the real one."""
    msg = f"\x1b[31m[MSFT] ⚠️ Couldn't verify/re-attach protection: {OUTAGE_EXC}\x1b[0m"
    assert network_outage_verdict(msg, outage_active=True) == "pass"
    # the escape codes alone are not a ticker tag
    assert network_outage_verdict(f"\x1b[31m{OUTAGE_EXC}\x1b[0m") == "report"


def test_transport_noise_with_no_bot_voice_still_collapses():
    """The protection above must not neuter the fix: lumibot's and APScheduler's own
    records carry no ticker tag and are still the thing we came here to silence."""
    assert network_outage_verdict(DNS_FAILURE) == "report"
    assert network_outage_verdict('Job "x" raised an exception', DNS_FAILURE) == "report"
    assert network_outage_verdict(
        "Executing the on_bot_crash event method", outage_active=True) == "suppress"


def test_a_sentence_in_front_of_the_exception_is_the_whole_discrimination():
    """Same exception text, opposite verdicts — decided only by whether something
    composed a sentence around it. This is the entire safety property in one test."""
    assert network_outage_verdict(DNS_FAILURE) == "report"
    assert network_outage_verdict(
        f"Startup sync skipped (broker not ready): {DNS_FAILURE}",
        outage_active=True) == "pass"


def test_an_opener_only_counts_at_the_start_of_the_message():
    """"Max retries exceeded" is both a signature and an opener. Buried mid-sentence it
    is the bot quoting the transport, not the transport speaking."""
    assert network_outage_verdict("Max retries exceeded talking to Alpaca") == "report"
    assert network_outage_verdict(
        "Could not flatten the position: Max retries exceeded",
        outage_active=True) == "pass"


@pytest.mark.parametrize("tag", ["[MSFT]", "[SPY]", "[BTC/USD]", "[STATE]", "[BRK.B]"])
def test_real_ticker_shapes_are_recognised_as_the_bots_own_voice(tag):
    assert network_outage_verdict(f"{tag} something: {DNS_FAILURE}", outage_active=True) == "pass"


# ── why this is a whitelist and not a blacklist ───────────────────────────────
#
# The [TICKER] guard above still missed three real lines, because they carry no symbol
# tag — adversarial review found them after the first guard shipped:
#
#   bot/strategy.py:501  f"Startup sync skipped (broker not ready): {e}"
#   bot/strategy.py:291  f"Stale order cleanup skipped: {e}"
#   bot/strategy.py:289  f"Stale order {oid} already gone: {e}"
#
# Line 501 is the except arm around the ENTIRE startup reconciliation block — position
# sync, overnight-flatten, gap-open close submission. Collapsed, the bot can silently
# skip reconciling real positions at startup and say "no action needed" about it.
#
# Two guards in a row both missed lines they were written to catch, which is the
# argument against enumerating what to hide. So the rule is inverted: a record is
# collapsed ONLY if it is positively recognised as transport noise — a bare transport
# exception logged as the whole message, APScheduler's job-failure record, or a known
# fixed fallout string. Everything else is kept, including every sentence the bot
# composes itself, including ones nobody has written yet.

def test_startup_sync_failure_is_kept_even_though_it_has_no_ticker_tag():
    msg = f"Startup sync skipped (broker not ready): {DNS_FAILURE}"
    assert network_outage_verdict(msg, outage_active=True) == "pass"


def test_stale_order_cleanup_failures_are_kept():
    for msg in (f"Stale order cleanup skipped: {DNS_FAILURE}",
                f"Stale order abc-123 already gone: {DNS_FAILURE}"):
        assert network_outage_verdict(msg, outage_active=True) == "pass", msg


def test_an_unknown_future_bot_sentence_is_kept_by_default():
    """The property that makes this safe: not-recognised means kept, never hidden."""
    assert network_outage_verdict(
        f"Some safety line nobody has written yet: {DNS_FAILURE}",
        outage_active=True) == "pass"


def test_a_bare_transport_exception_is_still_recognised_and_collapsed():
    """lumibot's strategy_executor.py:2211 does logger.error(e) — the message IS the
    exception, with no sentence in front of it. That is the arming record."""
    assert network_outage_verdict(DNS_FAILURE) == "report"
    assert network_outage_verdict(DNS_FAILURE, outage_active=True) == "suppress"


# ── the shapes the whitelist missed in production ─────────────────────────────
#
# Found 2026-09-18 in logs/stock_bot.log, written by a bot that HAD this filter loaded:
# 65 traceback lines and 0 "Network unreachable" notices. The whitelist shipped too
# narrow. Lumibot logs a transport failure in three shapes, none of which start with a
# transport exception, so is_transport_record() said "not mine" and let all of it through:
#
#   [DebbieLaSMC] Traceback (most recent call last):            <- traceback AS the message
#   [DebbieLaSMC] An error occurred during the on_trading_iteration lifecycle method: ...
#   alpaca_data.py:943 | Could not get pricing data from Alpaca for SPY with error: ...
#
# These are lumibot's own strings, never the bot reasoning about a position, and every
# one of them still has to carry a network signature before anything is collapsed — so a
# genuine crash with the same wrapper still prints in full.
#
# The extra guard: a traceback that names OUR files is never collapsed, however much
# transport text it contains. "During handling of the above exception, another exception
# occurred" is exactly how a real bug in our code hides inside a network failure.

LUMIBOT_TRACEBACK = (
    "\x1b[31mTraceback (most recent call last):\n"
    '  File ".../lumibot/strategies/strategy_executor.py", line 1080, in _on_trading_iteration\n'
    "    self.sync_broker()\n"
    "requests.exceptions.ConnectionError: HTTPSConnectionPool("
    "host='paper-api.alpaca.markets', port=443): Max retries exceeded\x1b[0m")


def test_a_traceback_logged_as_the_message_is_recognised():
    assert network_outage_verdict(LUMIBOT_TRACEBACK) == "report"
    assert network_outage_verdict(LUMIBOT_TRACEBACK, outage_active=True) == "suppress"


def test_the_lifecycle_wrapper_is_recognised():
    msg = ("\x1b[31mAn error occurred during the on_trading_iteration lifecycle method: "
           "HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): "
           "Read timed out. (read timeout=None)\x1b[0m")
    assert network_outage_verdict(msg) == "report"


def test_the_alpaca_data_layer_message_is_recognised():
    msg = ("Could not get pricing data from Alpaca for SPY with error: "
           "('Connection aborted.', ConnectionResetError(54, 'Connection reset by peer'))")
    assert network_outage_verdict(msg) == "report"


def test_a_traceback_with_no_network_signature_still_prints_in_full():
    """A real crash wears the same wrapper. Only the signature may collapse it."""
    crash = ("Traceback (most recent call last):\n"
             '  File "binance_bot.py", line 2109, in _ensure_protection\n'
             "KeyError: 'entry_price'")
    assert network_outage_verdict(crash, outage_active=True) == "pass"


def test_a_traceback_touching_our_own_code_is_never_collapsed():
    """"During handling of the above exception, another exception occurred" is exactly
    how a real bug in our code hides inside a network failure. If our files appear in
    the stack, the operator sees the whole thing whatever else it contains."""
    mixed = ("Traceback (most recent call last):\n"
             "requests.exceptions.ConnectionError: Max retries exceeded\n"
             "During handling of the above exception, another exception occurred:\n"
             'Traceback (most recent call last):\n'
             '  File "/Users/x/TradingBot/bot/strategy.py", line 656, in _ensure_protection\n'
             "TypeError: unsupported operand")
    assert network_outage_verdict(mixed, outage_active=True) == "pass"


@pytest.mark.parametrize("ours", ["bot/strategy.py", "binance_bot.py", "tradingbot.py",
                                  "bot/indicators.py", "test_bot.py"])
def test_our_files_in_the_stack_do_not_by_themselves_protect_a_traceback(ours):
    """The first version of this guard refused to collapse any traceback naming one of
    our files — which is nearly all of them, since the network calls happen INSIDE our
    strategy code. Measured on logs/stock_bot.log it disabled collapsing entirely.

    Whose frames are in the stack is not the question. What the FINAL exception is, is."""
    tb = (f"Traceback (most recent call last):\n  File \"/x/{ours}\", line 1, in f\n"
          f"requests.exceptions.ConnectionError: Max retries exceeded")
    assert network_outage_verdict(tb, outage_active=True) == "suppress", ours


def test_the_terminal_exception_decides_not_the_frames():
    """Same stack, same transport text in the middle, different ending."""
    head = ("Traceback (most recent call last):\n"
            "requests.exceptions.ConnectionError: Max retries exceeded\n"
            "During handling of the above exception, another exception occurred:\n"
            'Traceback (most recent call last):\n  File "/x/bot/strategy.py", line 656\n')
    assert network_outage_verdict(head + "TypeError: unsupported operand",
                                  outage_active=True) == "pass"
    assert network_outage_verdict(head + "requests.exceptions.ReadTimeout: timed out",
                                  outage_active=True) == "suppress"


def test_a_trailing_ansi_reset_does_not_hide_the_terminal_exception():
    """log_message(color=...) closes with \x1b[0m on its own line, so the last line of
    a real record is an escape code, not the exception. Seen verbatim in production."""
    tb = ("Traceback (most recent call last):\n"
          "requests.exceptions.ConnectionError: Max retries exceeded\n\x1b[0m")
    assert network_outage_verdict(tb, outage_active=True) == "suppress"


def test_the_strategy_name_prefix_does_not_hide_a_transport_record():
    """The reason the production log still had 65 uncollapsed traceback lines AFTER the
    openers above were added. lumibot's log_message() prepends the strategy name INTO
    the message text, so the record actually reads

        [DebbieLaSMC] \x1b[31mTraceback (most recent call last): ...

    and startswith() never matched. Verbatim from logs/stock_bot.log."""
    real = ('[DebbieLaSMC] \x1b[31mTraceback (most recent call last):\n'
            '  File ".../lumibot/strategies/strategy_executor.py", line 1080\n'
            "requests.exceptions.ConnectionError: HTTPSConnectionPool("
            "host='paper-api.alpaca.markets', port=443): Max retries exceeded\x1b[0m")
    assert network_outage_verdict(real) == "report"
    assert network_outage_verdict(real, outage_active=True) == "suppress"


def test_stripping_that_prefix_does_not_expose_the_bots_own_lines():
    """[MSFT] is the same shape as [DebbieLaSMC]. Stripping it must still leave a
    sentence that is not a transport opener, or the safety-critical lines come back
    into scope."""
    assert network_outage_verdict(
        f"[MSFT] ⚠️ Couldn't verify/re-attach protection: {DNS_FAILURE}",
        outage_active=True) == "pass"
    assert network_outage_verdict(
        f"[DebbieLaSMC] Startup sync skipped (broker not ready): {DNS_FAILURE}",
        outage_active=True) == "pass"


def test_an_errno_prefix_is_not_mistaken_for_a_strategy_name():
    """"[Errno 8]" has a space inside the brackets; a strategy name never does."""
    assert network_outage_verdict(f"[Errno 8] {DNS_FAILURE}") == "report"

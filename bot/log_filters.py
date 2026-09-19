"""Collapse a network outage into one honest line per minute.

When the Mac loses DNS — laptop sleeps, WiFi drops, network changes — every Alpaca
call fails with `socket.gaierror: [Errno 8] nodename nor servname provided`. Lumibot
catches it, fires on_bot_crash, and recovers on its own once DNS returns. Nothing is
broken. But on the way through it produces two floods that read exactly like a fatal
crash, and on 2026-09-16 they buried four hours of the stock bot's log:

  1. APScheduler logs `logger.exception('Job "%s" raised an exception', job)` for each
     failed iteration — a ~200-line nested traceback apiece. The MESSAGE names the job;
     the DNS failure is in `exc_info`, which is why matching on the message alone
     (the original filter in tradingbot.py) never caught it.
  2. Lumibot's run loop retries the crashed session with NO backoff
     (strategy_executor.py:2205-2216), so `Executing the on_bot_crash event method`
     repeats about five times a second for as long as the network is down.

WHY THIS IS A WHITELIST.

The obvious design — hide anything that looks like a network error — is wrong here, and
two successive attempts at it both shipped with holes:

  attempt 1  collapsed `[MSFT] ⚠️ Couldn't verify/re-attach protection: {e}`
             (bot/strategy.py:656). The bot composes that sentence around the transport
             exception, so it contains "Max retries exceeded". Collapsing it replaced
             "this position may be NAKED" with "no action needed".

  attempt 2  guarded lines carrying a [TICKER] tag, and still missed
             `Startup sync skipped (broker not ready): {e}` (bot/strategy.py:501) —
             the except arm around the ENTIRE startup reconciliation block: position
             sync, overnight flatten, gap-open close submission.

Both holes were the same mistake in different clothes: enumerating what to hide means
every line nobody thought of is hidden by default. On a bot trading real money the
asymmetry is stark — a missed suppression costs the operator some log noise, a wrong
suppression costs them an unprotected position they were told not to worry about.

So the rule is inverted. A record is collapsed ONLY if it is positively recognised as
transport noise: a bare transport exception logged as the whole message, APScheduler's
job-failure record, or a known fixed fallout string. Anything else is kept — including
every sentence the bot composes itself, including the ones nobody has written yet.
"""
import logging
import re
import time
import traceback as _traceback

# Substrings that identify a transient connectivity failure. Matched against the log
# message AND the formatted exception chain.
NETWORK_ERROR_SIGNS = (
    "nodename nor servname provided",   # macOS DNS failure (the common one here)
    "Failed to establish a new connection",
    "Max retries exceeded",
    "NewConnectionError",
    "Temporary failure in name resolution",
    "Connection aborted",
    "Read timed out",
    "ReadTimeout",
    "getaddrinfo failed",
    # asyncio/aiohttp transports (alpaca-py's trading stream) and raw socket errors
    # phrase the same outage differently from requests/urllib3.
    "gaierror",
    "ClientConnectorError",
    "ServerDisconnectedError",
    "Cannot connect to host",
    "Connection reset by peer",
    "RemoteDisconnected",
)

# A message that BEGINS with one of these is a transport exception logged as the entire
# record — lumibot's strategy_executor.py:2211 does a bare `logger.error(e)`. The
# leading position is the whole point: the bot's own lines put a sentence in front of
# the exception, and that is what separates "the network is down" from "I could not
# attach a stop-loss because the network is down".
TRANSPORT_MESSAGE_OPENERS = (
    "HTTPSConnectionPool(",
    "HTTPConnectionPool(",
    "<urllib3",
    "Max retries exceeded",
    "Failed to establish a new connection",
    "Cannot connect to host",
    "ClientConnectorError",
    "ServerDisconnectedError",
    "NewConnectionError",
    "[Errno ",
    # Lumibot's own phrasings, found in production on 2026-09-18: logs/stock_bot.log had
    # 65 traceback lines and ZERO notices, because none of these start with a transport
    # exception and the whitelist therefore said "not mine". All three are lumibot
    # strings, never the bot reasoning about a position — and each still has to carry a
    # network signature before anything is collapsed.
    "Traceback (most recent call last)",
    "An error occurred during the on_trading_iteration lifecycle method",
    "Could not get pricing data from",
)

# WHAT DECIDES A TRACEBACK is its FINAL exception, not whose frames appear in it.
#
# The first version of this guard refused to collapse any traceback naming one of our
# own files — and measured against logs/stock_bot.log that disabled collapsing outright,
# because the network calls happen INSIDE our strategy code, so our files are in almost
# every stack. Whose frames are present says nothing; the exception the stack ENDS on
# says everything. "During handling of the above exception, another exception occurred"
# is precisely how a real defect hides inside a network failure, and it is the tail that
# tells them apart.
_EXC_LINE = re.compile(r"^(?:[\w.]+\.)?\w*(?:Error|Exception|Timeout|Interrupt)\b")


def terminal_exception_is_transport(text):
    """True when the LAST exception in a traceback is a connectivity failure.

    Returns False for anything it cannot read as a traceback tail, because not knowing
    means not collapsing.
    """
    lines = [l for l in _strip_ansi(text or "").split("\n") if l.strip()]
    for line in reversed(lines):                 # skip trailing ANSI resets and blanks
        stripped = line.strip()
        if _EXC_LINE.match(stripped):
            return _has_sign(stripped)
        if stripped.startswith(("File \"", "Traceback", "During handling",
                                "The above exception")) or stripped.startswith(" "):
            continue
        return False
    return False

# Records whose outage lives in exc_info rather than the message, identified by a fixed
# phrase their emitter always uses.
COLLAPSIBLE_RECORDS = (
    "raised an exception",              # APScheduler executors/base.py:145 and :195
)

# Lines that carry no network text of their own but only ever appear as fallout from an
# outage already reported. Suppressed INSIDE the quiet window and nowhere else, so that
# the same line during a genuine non-network crash still reaches the operator.
OUTAGE_CONSEQUENCE_LINES = (
    "Executing the on_bot_crash event method",
    "Unable to get cash from broker",
    "Unable to get the cash balance after",
    # _on_bot_crash calls gracefully_exit(), which shuts the scheduler down and sets it
    # to None; the next iteration then announces it is rebuilding one. Fallout, not news.
    "Scheduler is None, attempting to recreate",
)

OUTAGE_NOTICE = (
    "🌐 Network unreachable (DNS/connection failure) — Alpaca calls are failing. The "
    "bot recovers automatically once the connection is back; no action needed unless "
    "this persists. Further network errors suppressed for {secs}s.")

# lumibot's log_message(color=...) runs the text through termcolor BEFORE it reaches any
# filter, so real messages arrive wrapped as "\x1b[31m...\x1b[0m". Strip that before
# testing how a message STARTS, or every coloured record looks like prose.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text):
    return _ANSI.sub("", text)


def _has_sign(text):
    return any(sign in text for sign in NETWORK_ERROR_SIGNS)


# lumibot's log_message() prepends the STRATEGY NAME into the message text, so a record
# arrives as "[DebbieLaSMC] Traceback (most recent call last): ..." and startswith() never
# matches. That alone left 65 uncollapsed traceback lines in logs/stock_bot.log after the
# openers above were added. A strategy name never contains whitespace inside its brackets,
# which is what keeps "[Errno 8] ..." from being eaten as one.
_NAME_PREFIX = re.compile(r"^\[[^\]\s]+\]\s+")


def is_transport_record(message):
    """True only for records emitted BY the transport layer or its scheduler.

    Deliberately narrow: not recognised means not collapsed. Stripping the name prefix
    is safe for the bot's own lines — "[MSFT] ⚠️ Couldn't verify/re-attach protection"
    becomes "⚠️ Couldn't verify...", which is still not a transport opener.
    """
    clean = _strip_ansi(message).lstrip()
    if any(phrase in clean for phrase in COLLAPSIBLE_RECORDS):
        return True
    return (clean.startswith(TRANSPORT_MESSAGE_OPENERS)
            or _NAME_PREFIX.sub("", clean).startswith(TRANSPORT_MESSAGE_OPENERS))


def network_outage_verdict(message, exception_text="", *, outage_active=False):
    """Decide what to do with one log record. Pure.

    Returns "report" (show it, collapsed to a single line), "suppress" (drop it), or
    "pass" (not our business — leave the record exactly as it is).
    """
    message = _strip_ansi(message or "").lstrip()
    exception_text = exception_text or ""

    combined = message if "Traceback" in message else (exception_text or message)
    if "Traceback" in combined:
        # A stack: only its terminal exception decides.
        if is_transport_record(message) and terminal_exception_is_transport(combined):
            return "suppress" if outage_active else "report"
    elif is_transport_record(message) and (_has_sign(message) or _has_sign(exception_text)):
        return "suppress" if outage_active else "report"

    if outage_active and any(s in message for s in OUTAGE_CONSEQUENCE_LINES):
        return "suppress"

    return "pass"          # unrecognised is kept, never hidden


def record_exception_text(record):
    """The formatted exception chain behind a record, or "" if there is none.

    APScheduler's traceback reaches us only through here — `getMessage()` never shows
    it. Prefers `exc_text` when a handler has already formatted (and cached) it.
    """
    cached = getattr(record, "exc_text", None)
    if cached:
        return cached
    exc_info = getattr(record, "exc_info", None)
    if not exc_info:
        return ""
    try:
        return "".join(_traceback.format_exception(*exc_info))
    except Exception:
        return ""


class CollapseNetworkTracebackFilter(logging.Filter):
    """Keep ONE line per quiet window of continuous outage; drop the rest."""

    def __init__(self, quiet_window_secs=60, clock=time.time):
        super().__init__()
        self.quiet_window_secs = quiet_window_secs
        self._clock = clock
        self._reported_at = None

    def _outage_active(self, now):
        return (self._reported_at is not None
                and now - self._reported_at < self.quiet_window_secs)

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True            # a record we cannot read is one we must not eat
        now = self._clock()
        verdict = network_outage_verdict(
            message, record_exception_text(record),
            outage_active=self._outage_active(now))
        if verdict == "pass":
            return True
        if verdict == "suppress":
            return False
        self._reported_at = now
        record.msg = OUTAGE_NOTICE.format(secs=int(self.quiet_window_secs))
        record.args = ()
        # Both, or a handler that already cached a formatted traceback reprints it.
        record.exc_info = None
        record.exc_text = None
        return True

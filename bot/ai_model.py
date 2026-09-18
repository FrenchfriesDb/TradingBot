"""Resolve a live NVIDIA NIM model, shared by BOTH bots.

The model id was hardcoded to `meta/llama-3.3-70b-instruct` in binance_bot.py AND in
bot/strategy.py. NVIDIA decommissioned it, and because both callers fail OPEN on an API
error, every trade in both bots logged "🤖 AI Bot Approval: ✅ YES" from an exception
handler — a verdict no model ever gave, for weeks.

Two lessons are baked in here:

1. A HARDCODED ID WILL ROT AGAIN, so resolve by PROBING.
2. THE /v1/models LISTING CANNOT BE TRUSTED to do that probing for you. Measured
   2026-09-15 on this account: the endpoint returned 81 models while only 4 of 14 tried
   were actually callable — the rest answered 404, 410 or 503. Listing != callable.

It lives in bot/ rather than in either bot because these two have silently diverged on
shared logic more than once; one definition means one place to fix it next time.
"""
import json
import os
import re
import urllib.request

ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

# Read LAZILY, never at import. config.py calls load_dotenv() at ITS module level, and
# this module can be imported first — in which case os.getenv() at import time returns the
# hardcoded default and NVIDIA_MODEL in .env is silently ignored. That is exactly the kind
# of "I set it and nothing happened" bug that wastes an afternoon.
PREFERRED_DEFAULT = "nvidia/nemotron-3-super-120b-a12b"


def configured_model():
    """The operator's chosen model, read at call time so .env is guaranteed loaded."""
    return os.getenv("NVIDIA_MODEL") or PREFERRED_DEFAULT


# Errors that mean the model is GONE — demote immediately, retrying is pointless.
PERMANENT_CODES = {400, 401, 403, 404, 410, 422}
FALLBACKS = [
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
]

_RESOLVED = None          # None = not probed yet, "" = probed and nothing works


def _classify(exc):
    """('permanent'|'transient', code) — a 404 is gone, a 503 or timeout is just busy."""
    code = getattr(exc, "code", None)
    if code in PERMANENT_CODES:
        return "permanent", code
    return "transient", code or type(exc).__name__


def _probe(model, api_key, timeout):
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": "ok"}],
                       "max_tokens": 4, "temperature": 0}).encode()
    req = urllib.request.Request(
        ENDPOINT, data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=timeout).read(1)


def resolve(api_key, timeout=20, log=print):
    """First model that actually answers, or None.

    None is the honest state — the caller must then stop claiming an approval rather
    than print ✅ YES off a fail-open.
    """
    global _RESOLVED
    if _RESOLVED is not None:
        return _RESOLVED or None
    if not api_key:
        _RESOLVED = ""
        log("🤖 AI disabled — no NVIDIA_API_KEY. Trading on technicals only.")
        return None
    preferred = configured_model()
    tried = []
    for model in [preferred] + [m for m in FALLBACKS if m != preferred]:
        # Retry a TRANSIENT failure before demoting. Measured live: the preferred model
        # answered 503 on one probe and worked on the very next, and the first version of
        # this resolver cached that single blip as "dead" for the whole process lifetime —
        # pinning the bot to a lesser model until someone restarted it. A 404/410 means
        # gone and breaks out immediately; a 503 or timeout only means busy.
        attempts = 1 if model != preferred else 3
        for attempt in range(attempts):
            try:
                _probe(model, api_key, timeout)
                _RESOLVED = model
                if model != preferred:
                    log(f"⚠️  AI model '{preferred}' unavailable — using '{model}'. "
                        f"Set NVIDIA_MODEL in .env to pin a different one.")
                else:
                    log(f"🤖 AI model live: {model}")
                return model
            except Exception as e:
                kind, code = _classify(e)
                if kind == "permanent" or attempt == attempts - 1:
                    tried.append(f"{model} ({code}{'' if kind == 'permanent' else ', transient'})")
                    break
                log(f"   …{model} returned {code} (transient) — retrying "
                    f"{attempt + 2}/{attempts}")
    _RESOLVED = ""
    log("⛔ AI UNAVAILABLE — no model answered. Trading on technicals only; the log will "
        "say SKIPPED, not approved.\n     tried: " + "; ".join(tried))
    return None


def reset_cache():
    """Forget the probe result — for tests, and for a key change without a restart."""
    global _RESOLVED
    _RESOLVED = None


# ── reading the model's answer ────────────────────────────────────────────────
# Both bots asked for "DECISION: YES or NO" and, when they could not find that line,
# set confirm = True and logged "🤖 AI Bot Approval: ✅ YES". On 2026-09-18 that put
# BTC on at $77,214 off this reply, which is the model's first sentence and nothing else:
#
#   "We need to analyze the 4H chart data and decide if a genuine liquidity sweep
#    occurred at $76,182."
#
# It was truncated because both callers passed max_tokens=120 to a REASONING model — it
# spends that budget thinking and never reaches the DECISION line. So the parse did not
# fail occasionally, it was structurally guaranteed to fail on any setup the model
# thought hard about, and every one of those became a yes.
#
# Returning None here (rather than a decision) is what lets the callers tell apart "the
# model never spoke", which is an infrastructure problem and should not veto a setup
# that already passed every technical filter, from "the model spoke and we could not
# find a decision", which is content — and in the BTC case was content reasoning its way
# toward NO. Only the first of those may proceed.
_THINK    = re.compile(r"<think>.*?</think>|<thinking>.*?</thinking>",
                       re.IGNORECASE | re.DOTALL)
_DECISION = re.compile(r"DECISION\s*[:\-]?\s*\**\s*(YES|NO)\b", re.IGNORECASE)
# A whole line that is nothing but YES/NO is unambiguous and worth accepting; "whether
# this is a yes" inside prose is not, and reading it as one re-invents the bug.
_BARE     = re.compile(r"^\s*\**\s*(YES|NO)\s*\**\s*\.?\s*$",
                       re.IGNORECASE | re.MULTILINE)
_RR       = re.compile(r"\bRR\s*[:\-]?\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_REASON   = re.compile(r"REASON\s*[:\-]?\s*(.+)", re.IGNORECASE | re.DOTALL)


def parse_ai_decision(text):
    """(decision, rr, reason) from a model reply. decision is "YES", "NO" or None.

    None means "no decision found" — never treat it as approval. rr is None when no
    usable number was given; a missing RR is recoverable (the caller clamps to its own
    floor) and must not take the decision down with it.
    """
    if not isinstance(text, str):
        return None, None, ""
    clean = _THINK.sub(" ", text).strip()
    if not clean:
        return None, None, ""

    # A model that thinks out loud often restates itself ("DECISION: NO ... on
    # reflection ... DECISION: YES"). The final answer is the answer.
    found = _DECISION.findall(clean) or _BARE.findall(clean)
    decision = found[-1].upper() if found else None

    rr = None
    m = _RR.search(clean)
    if m:
        try:
            rr = float(m.group(1))
        except ValueError:
            rr = None

    m = _REASON.search(clean)
    reason = m.group(1).strip() if m else clean
    return decision, rr, reason


# Markers that mean "no verdict was obtained" — either the model never spoke
# (infrastructure) or it spoke unreadably (content). Both bots fail OPEN on the first
# kind, which is defensible; printing "🤖 AI Bot Approval: ✅ YES" over the top of it is
# not, because that icon is what the eye actually reads. binance_bot.py carried an
# ad-hoc version of this that matched "AI SKIPPED" and "unavailable" and missed the
# timeout path entirely. One predicate, used by both callers.
NO_OPINION_MARKERS = (
    "proceeding on technicals",
    "standing aside",
    "ai skipped",
    "unavailable",
    "timeout",
    "no ai key",
)


def is_no_opinion(reason):
    """True when `reason` describes a fallback rather than a verdict the model gave.

    Unreadable input counts as no-opinion: fail toward "we do not know", never toward
    "the model said yes".
    """
    if not isinstance(reason, str) or not reason.strip():
        return True
    lowered = reason.lower()
    return any(m in lowered for m in NO_OPINION_MARKERS)

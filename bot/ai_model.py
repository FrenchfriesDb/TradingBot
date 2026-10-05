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
import time
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
# Both bots asked for "DECISION: <YES or NO>" and, when they could not find that line,
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


def reply_text(resp):
    """The model's text from a chat completion, or "" — never raises.

    THE BUG. Both bots did `resp.choices[0].message.content.strip()`, which dies with

        AI unavailable ('NoneType' object has no attribute 'strip')

    the moment `content` is None. That is not an outage and not a rate limit: the model
    answered. Reasoning models (nvidia/nemotron-3-super-*, deepseek-r1 and friends) put
    their chain of thought in a SEPARATE field and can return content=None entirely —
    typically when the whole max_tokens budget was spent reasoning and the reply was cut
    off before any final text. The caller then reported "AI unavailable", which sent the
    operator looking for an API problem that did not exist.

    Falls back to the reasoning field, because parse_ai_decision already strips <think>
    blocks and can find a DECISION line inside reasoning text. If the reasoning really was
    cut off before the verdict, parse_ai_decision returns no decision and the caller stands
    aside — which is the correct, safe outcome, reached honestly instead of via a crash.
    """
    try:
        choice = (getattr(resp, "choices", None) or [None])[0]
        if choice is None:
            return ""
        msg = getattr(choice, "message", None)
        if msg is None:
            return ""
        for field in ("content", "reasoning_content", "reasoning"):
            val = getattr(msg, field, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""
    except Exception:
        return ""


def finish_hint(resp):
    """Why the reply ended, when that explains an empty one. "" when unremarkable.

    finish_reason == "length" means the token budget ran out mid-thought, which is the
    usual cause of an empty content field on a reasoning model — and the actionable fix is
    raising max_tokens, not debugging the network. Surfacing it turns a 20-minute hunt into
    a sentence in the log.
    """
    try:
        choice = (getattr(resp, "choices", None) or [None])[0]
        fr = getattr(choice, "finish_reason", None) if choice else None
        if fr == "length":
            return " (reply hit the max_tokens ceiling mid-reasoning — raise max_tokens)"
        return f" (finish_reason={fr})" if fr and fr != "stop" else ""
    except Exception:
        return ""


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


# ── one shared confirmation call ──────────────────────────────────────────────
# binance_bot.py and bot/strategy.py each grew their own copy of prompt-and-parse, and
# each grew the SAME fail-open bug, fixed separately in both on 2026-09-18. test_bot had
# no AI at all. This is the shared version, so a third private copy does not repeat the
# lesson a third time. The transport is injected: testable without a network, and each
# caller keeps control of its own timeout.
CONFIRM_PROMPT = """You are grading a mechanical trading setup. Be sceptical; most
setups are not worth taking.

Symbol     : {symbol}
Direction  : {direction}
Entry      : {entry}
Stop       : {stop}
Target     : {target}
Structural R:R : 1:{rr}
Higher-timeframe trend : {trend}
Sweep confirmation     : {pattern}

Answer on the VERY FIRST LINE, before any reasoning — if you think first you will be cut
off before you answer, and an answer nobody can read is treated as a refusal.

Reply in EXACTLY this format, one field per line, nothing else:
DECISION: <YES or NO>
RR: a number
REASON: one concise sentence"""


def _default_call(api_key, model, timeout):
    """Real NVIDIA NIM transport. Returns a callable taking the prompt, returning text."""
    def call(prompt):
        import urllib.request
        body = json.dumps({"model": model,
                           "messages": [{"role": "user", "content": prompt}],
                           # Measured 2026-09-18 against the live model: 400 finishes
                           # with finish_reason "length" mid-deliberation, 900 completes.
                           "max_tokens": 1000, "temperature": 0.1}).encode()
        req = urllib.request.Request(
            "https://integrate.api.nvidia.com/v1/chat/completions", data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            choice = json.loads(resp.read())["choices"][0]
            # finish_reason "length" means the model was cut off mid-thought. Report it:
            # a decision string found inside truncated deliberation is not a verdict.
            return choice["message"]["content"], choice.get("finish_reason")
    return call


def confirm_setup(context, *, call=None, api_key=None, model=None,
                  min_rr=2.0, max_rr=15.0, timeout=25,
                  attempts=3, retry_wait=1.0):
    """(confirm, rr, reason) — should this setup be taken?

    An unreadable reply REFUSES. A transport failure proceeds on technicals but says so,
    so is_no_opinion() can keep the log from printing a green tick nobody earned. See
    tests/test_ai_confirm_setup.py for why each of those is the way round it is.
    """
    if call is None:
        api_key = api_key if api_key is not None else os.getenv("NVIDIA_API_KEY", "")
        if not api_key:
            return True, min_rr, "no AI key — proceeding on technicals"
        call = _default_call(api_key, model or configured_model(), timeout)

    # NVIDIA's endpoint is intermittently overloaded. Measured 2026-09-20, four calls
    # back to back on a working key: 503, 503, then 200 in 6.1s and 200 in 12.7s. A
    # single attempt turned a blip that clears in seconds into "AI unavailable", and the
    # gate silently stopped gating — one of this bot's four production verdicts was
    # exactly that. The model RESOLVER in this same file already retried transient
    # failures three times; the confirmation call did not.
    #
    # Permanent codes are not retried: a bad key or a dead model will not fix itself,
    # and the setup is waiting on an answer.
    prompt = CONFIRM_PROMPT.format(**context)
    result, failure = None, None
    for attempt in range(max(1, attempts)):
        try:
            result = call(prompt)
            failure = None
            break
        except Exception as exc:
            failure = exc
            if getattr(exc, "code", None) in PERMANENT_CODES:
                break
            if attempt < attempts - 1 and retry_wait:
                time.sleep(retry_wait)

    if failure is not None:
        code = getattr(failure, "code", None)
        detail = f"{type(failure).__name__}{f' {code}' if code else ''}"
        return True, min_rr, f"AI unavailable ({detail}) — proceeding on technicals"

    # A transport may report why generation stopped; one that does not is not truncated.
    text, finish = result if isinstance(result, tuple) else (result, None)

    decision, rr, reason = parse_ai_decision(text)
    rr = max(min_rr, min(rr if rr is not None else min_rr, max_rr))
    if finish == "length":
        # Cut off mid-thought. Measured live: at max_tokens=400 this model produced a
        # "NO" matched out of its own deliberation, not an answer. It happened to fail
        # closed, but a coin flip that lands safe is still a coin flip.
        return False, rr, "AI reply was truncated before its verdict — standing aside"
    if decision is None:
        # The model SPOKE and no decision could be found in it — content, not
        # infrastructure, and on BTC it was content reasoning its way toward NO.
        return False, rr, f"AI gave no readable decision — standing aside ({str(text)[:120]})"
    return decision == "YES", rr, reason

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
import urllib.request

ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

# Override in .env. Default and fallbacks all verified callable 2026-09-15.
DEFAULT_MODEL = os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3-super-120b-a12b")
FALLBACKS = [
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
]

_RESOLVED = None          # None = not probed yet, "" = probed and nothing works


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
    tried = []
    for model in [DEFAULT_MODEL] + [m for m in FALLBACKS if m != DEFAULT_MODEL]:
        try:
            _probe(model, api_key, timeout)
            _RESOLVED = model
            if model != DEFAULT_MODEL:
                log(f"⚠️  AI model '{DEFAULT_MODEL}' is dead — fell back to '{model}'. "
                    f"Set NVIDIA_MODEL in .env to make this permanent.")
            else:
                log(f"🤖 AI model live: {model}")
            return model
        except Exception as e:
            tried.append(f"{model} ({getattr(e, 'code', None) or type(e).__name__})")
    _RESOLVED = ""
    log("⛔ AI UNAVAILABLE — no model answered. Trading on technicals only; the log will "
        "say SKIPPED, not approved.\n     tried: " + "; ".join(tried))
    return None


def reset_cache():
    """Forget the probe result — for tests, and for a key change without a restart."""
    global _RESOLVED
    _RESOLVED = None

"""One AI-model definition, shared by both bots, resolved by PROBING not listing.

THE BUG. `meta/llama-3.3-70b-instruct` was hardcoded in binance_bot.py AND in
bot/strategy.py. NVIDIA decommissioned it. Both callers fail OPEN on an API error, so for
weeks every trade in both bots logged "🤖 AI Bot Approval: ✅ YES" from an exception
handler — a verdict no model ever gave.

WHY PROBING, NOT LISTING. Measured against the live account 2026-09-15: /v1/models
returned 81 models while only 4 of 14 tried were actually callable — the rest answered
404, 410 or 503. A listing is not a capability check.
"""
import pytest

from bot import ai_model


@pytest.fixture(autouse=True)
def _clear():
    ai_model.reset_cache()
    yield
    ai_model.reset_cache()


def test_no_api_key_disables_rather_than_pretending():
    logs = []
    assert ai_model.resolve("", log=logs.append) is None
    assert any("AI disabled" in m for m in logs)


def test_the_first_working_model_wins():
    tried = []
    def fake(model, key, timeout):
        tried.append(model)
        if model != ai_model.FALLBACKS[-1]:
            raise RuntimeError("404")
    ai_model._probe = fake
    got = ai_model.resolve("key", log=lambda m: None)
    assert got == ai_model.FALLBACKS[-1]
    assert len(tried) > 1, "it must walk the chain, not give up on the first failure"


def test_a_dead_default_is_reported_not_silently_swapped():
    logs = []
    ai_model._probe = lambda m, k, t: (_ for _ in ()).throw(RuntimeError("410")) \
        if m == ai_model.DEFAULT_MODEL else None
    ai_model.resolve("key", log=logs.append)
    assert any("is dead" in m and "NVIDIA_MODEL" in m for m in logs), \
        "the operator must be told to pin the working one"


def test_nothing_callable_returns_none_and_says_so():
    """None is the honest state — the caller then stops claiming an approval."""
    logs = []
    ai_model._probe = lambda m, k, t: (_ for _ in ()).throw(RuntimeError("410"))
    assert ai_model.resolve("key", log=logs.append) is None
    assert any("AI UNAVAILABLE" in m for m in logs)
    assert any("SKIPPED, not approved" in m for m in logs)


def test_the_result_is_cached_so_it_probes_once_not_per_trade():
    calls = []
    ai_model._probe = lambda m, k, t: calls.append(m)
    ai_model.resolve("key", log=lambda m: None)
    ai_model.resolve("key", log=lambda m: None)
    ai_model.resolve("key", log=lambda m: None)
    assert len(calls) == 1, "a 20s probe per trade would stall the loop"


def test_a_failed_probe_is_also_cached():
    calls = []
    def fake(m, k, t):
        calls.append(m); raise RuntimeError("410")
    ai_model._probe = fake
    ai_model.resolve("key", log=lambda m: None)
    n = len(calls)
    ai_model.resolve("key", log=lambda m: None)
    assert len(calls) == n, "must not re-probe a dead endpoint every cycle"


def test_reset_cache_allows_a_key_change_without_a_restart():
    ai_model._probe = lambda m, k, t: None
    assert ai_model.resolve("key", log=lambda m: None)
    ai_model.reset_cache()
    calls = []
    ai_model._probe = lambda m, k, t: calls.append(m)
    ai_model.resolve("newkey", log=lambda m: None)
    assert calls, "after reset it must probe again"


def test_the_dead_model_is_not_in_the_fallback_chain():
    assert "meta/llama-3.3-70b-instruct" not in ai_model.FALLBACKS
    assert "meta/llama-3.3-70b-instruct" != ai_model.DEFAULT_MODEL

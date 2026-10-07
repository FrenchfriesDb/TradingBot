"""EXECUTE the AI call path. Compiling it proves nothing.

2026-10-07: raising the AI token ceiling and timeout meant referencing a shared constant
from the call site. In the crypto bot that name was imported INSIDE the nested function,
AFTER the create() call that now read it — so Python made it a local for the whole function
and raised UnboundLocalError before any request left the machine. The outer
`_future.result(timeout=...)` could not see the name at all.

Both land in `except Exception`, which returns "AI SKIPPED — technicals only". Every trade
would have walked past the AI gate with a log line as the only evidence, and
py_compile / the existing suite passed throughout, because nothing ever RAN the function.

These tests run both get_ai_confirmation functions end to end against a fake model.
"""
import types

import pandas as pd
import pytest

import binance_bot as B
from bot import ai_model as A
from bot import strategy as S


@pytest.fixture(autouse=True, scope="module")
def _libs_loaded():
    assert B._libs_ready.wait(timeout=120)


class _Msg:
    def __init__(self, content=None, reasoning_content=None):
        self.content, self.reasoning_content = content, reasoning_content


class _Resp:
    def __init__(self, msg, finish="stop"):
        self.choices = [types.SimpleNamespace(message=msg, finish_reason=finish)]
        self.usage = types.SimpleNamespace(completion_tokens=900)


class _FakeOpenAI:
    """Records exactly what the bot asked for, so the budget is asserted, not assumed."""
    calls = []
    reply = _Resp(_Msg(content="DECISION: YES\nRR: 4.0\nREASON: clean sweep."))

    def __init__(self, **kw):
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kw):
        _FakeOpenAI.calls.append(kw)
        return _FakeOpenAI.reply


@pytest.fixture
def fake(monkeypatch):
    import openai
    _FakeOpenAI.calls = []
    _FakeOpenAI.reply = _Resp(_Msg(content="DECISION: YES\nRR: 4.0\nREASON: clean sweep."))
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    return _FakeOpenAI


def _bars(n=60, px=100.0):
    idx = pd.date_range("2026-10-06", periods=n, freq="15min")
    return pd.DataFrame({"open": px, "high": px + 1, "low": px - 1, "close": px + 0.2,
                         "volume": 1000.0}, index=idx)


def _crypto():
    return B.get_ai_confirmation("BTC/USD", 100.0, "bullish", "bullish", 98.0, 101.0, 97.0,
                                 95.0, 5.0, None, _bars(), _bars())


def _stock():
    # news lookup would hit the network; it is fail-soft by design but slow — stub it
    return S.get_ai_confirmation("NFLX", 100.0, "bearish", "bearish", 98.0, 101.0, 105.0,
                                 106.0, 6.0, None, _bars(), _bars())


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(B, "NVIDIA_API_KEY", "test-key")
    monkeypatch.setattr(S, "NVIDIA_API_KEY", "test-key")
    monkeypatch.setattr(B, "resolve_ai_model", lambda *a, **k: "fake/model")
    monkeypatch.setattr(S._ai_model, "resolve", lambda *a, **k: "fake/model")
    import bot.news as N
    monkeypatch.setattr(N, "get_news_context",
                        lambda *a, **k: ("RECENT NEWS: none", 0, 0.0, 0), raising=False)


@pytest.mark.parametrize("fn", [_crypto, _stock], ids=["crypto", "stock"])
class TestBothBotsReallyCallTheModel:
    def test_a_normal_reply_is_a_real_decision_not_a_skip(self, fake, fn):
        """THE regression. The unbound-name bug turned this into 'AI SKIPPED'."""
        ok, rr, why = fn()
        assert fake.calls, "no request was ever sent — the call path crashed first"
        assert "SKIPPED" not in why and "unavailable" not in why.lower(), why
        assert ok is True and rr >= 1.0

    def test_it_asks_for_the_reasoning_budget_and_the_longer_timeout(self, fake, fn):
        fn()
        kw = fake.calls[0]
        assert kw["max_tokens"] == A.AI_MAX_TOKENS >= 3000
        assert kw["timeout"] == A.AI_CALL_TIMEOUT >= 45

    def test_a_no_stays_a_no(self, fake, fn):
        fake.reply = _Resp(_Msg(content="DECISION: NO\nRR: 3.5\nREASON: weak."))
        ok, _, why = fn()
        assert ok is False and "SKIPPED" not in why

    def test_a_reply_that_is_only_reasoning_stands_aside(self, fake, fn):
        """The 2026-10-06 NFLX case: chain-of-thought, cut off before any verdict."""
        fake.reply = _Resp(_Msg(content=None, reasoning_content=(
            "We need to decide YES or NO for taking the short based on analysis. "
            "Provide RR >=3.5. Provide concise reason.")), finish="length")
        ok, _, why = fn()
        assert ok is False, "an unreadable answer must not become an approval"
        assert "standing aside" in why

    def test_a_totally_empty_reply_stands_aside(self, fake, fn):
        fake.reply = _Resp(_Msg(content=None), finish="length")
        ok, _, why = fn()
        assert ok is False and "max_tokens" in why


class TestTimeoutsOutlastTheModel:
    def test_the_outer_deadline_is_above_the_http_timeout(self):
        """Otherwise the executor fires first and the longer timeout never matters."""
        import inspect
        src = inspect.getsource(B.get_ai_confirmation)
        assert "AI_CALL_TIMEOUT + 10" in src

    def test_the_budget_is_not_back_to_the_knife_edge(self):
        """1000 tokens cut ~1 reply in 3 for this model — it spends 900-1000 reasoning."""
        assert A.AI_MAX_TOKENS >= 3000
        assert A.AI_CALL_TIMEOUT >= 45

    def test_no_call_site_hardcodes_a_budget_any_more(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        for rel in ("bot/strategy.py", "binance_bot.py"):
            src = (root / rel).read_text()
            assert "max_tokens=1000" not in src, f"{rel} still hardcodes the old ceiling"

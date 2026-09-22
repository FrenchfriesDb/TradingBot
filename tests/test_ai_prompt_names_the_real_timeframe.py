"""The AI prompt must name the chart it is actually showing the model.

The prompt said "4H" in ten places — "4H candles (oldest -> newest)", "4H BOS",
"Analyze using the full 4H chart above", "does the 4H chart show a brutal sweep of
lows" — while the candles printed underneath it were SIX-hour bars.

Why it drifted: Bybit is the only 4H source (Coinbase offers 1H and 6H, not 4H), and
Bybit 403s from this machine. From the bot's own startup log:

    HTF data:  Bybit unavailable (bybit GET .../instruments-info?category=spot
    403 Forbidden {error:The Amazon CloudFront distribution is configured to block
    access from your country}) — falling back to 6H on main exchange

So in practice the fallback is always taken, and the model was asked to judge sweeps,
displacement and BOS on bars 1.5x the size of the ones it was told to expect. It is the
same defect as tf_tag saying "4H" over 6h data, but pointed at the thing making the
GO/NO-GO call rather than at a chart label.

The fix is structural, not a string swap: htf_name is the SAME variable handed to
fetch_ohlcv_retry, so the name cannot disagree with the data. The 4H path is still real
and still correct when Bybit is reachable, which is why this is parameterised over both.
"""
import re
import sys
import types

import pandas as pd
import pytest

import binance_bot


@pytest.fixture
def captured(monkeypatch):
    """Intercept the OpenAI call and hand back the exact prompt string."""
    box = {}

    class _Msg:
        content = "DECISION: NO\nRR: 3.5\nREASON: test"

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Resp:
        choices = [_Choice()]

    class _FakeClient:
        def __init__(self, **kw):
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=self._create))

        def _create(self, **kw):
            box["prompt"] = kw["messages"][0]["content"]
            return _Resp()

    # Stub the news lookup. Without this the test hits the live Alpaca news API and runs
    # FinBERT — slow, non-deterministic, and headlines carry relative stamps like
    # "[6h ago]" / "[4h ago]" that would make the assertions below meaningless.
    import bot.news
    # get_news_context returns (prompt_block, headlines, sentiment, probability) — match
    # it exactly, or the caller's unpack raises and we silently test the failure branch.
    monkeypatch.setattr(bot.news, "get_news_context",
                        lambda *a, **k: ("RECENT NEWS: none in the last 24h.", [], None, 0.0))
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=_FakeClient))
    monkeypatch.setattr(binance_bot, "NVIDIA_API_KEY", "test-key-not-a-real-credential")
    monkeypatch.setattr(binance_bot, "resolve_ai_model", lambda *a, **k: "test-model")
    return box


def _df(n=60, base=100.0):
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-09-21", periods=n, freq="6h"),
        "open":  [base + i for i in range(n)],
        "high":  [base + i + 2 for i in range(n)],
        "low":   [base + i - 2 for i in range(n)],
        "close": [base + i + 1 for i in range(n)],
        "volume": [10.0] * n,
    })


def _ask(captured, htf_name):
    binance_bot.get_ai_confirmation(
        "BTC/USD", 100.0, "bullish", "bullish",
        99.0, 101.0, 98.0,
        97.0, 3.0, 110.0,
        _df(), _df(), amd_phase=None, zone_type="bullish_fvg",
        htf_name=htf_name,
    )
    return captured["prompt"]


@pytest.mark.parametrize("htf_name,expected", [("6h", "6H"), ("4h", "4H"), ("1h", "1H")])
def test_the_prompt_names_the_timeframe_that_was_actually_fetched(captured, htf_name, expected):
    prompt = _ask(captured, htf_name)
    assert f"{expected} candles (oldest" in prompt
    assert f"Analyze using the full {expected} chart above" in prompt
    assert f"{expected} BOS" in prompt


def _mentions(prompt, tf):
    """Lines naming `tf` as a timeframe in its own right.

    A plain substring test is wrong in both directions: "24h" contains "4h", and a
    headline may legitimately say "4h ago". So require that no digit precedes it, and
    ignore the news block, which is arbitrary text from an external feed that this
    code neither writes nor controls."""
    body = prompt.split("RECENT NEWS")[0]
    return [ln for ln in body.splitlines()
            if re.search(rf"(?<![0-9]){tf}\b", ln, re.IGNORECASE)]


def test_a_6h_prompt_never_claims_to_be_4h(captured):
    """The actual live configuration on this machine."""
    prompt = _ask(captured, "6h")
    assert not _mentions(prompt, "4h"), _mentions(prompt, "4h")
    assert _mentions(prompt, "6h"), "it stopped naming the frame at all"


def test_the_4h_path_is_untouched_when_bybit_is_reachable(captured):
    """Bybit really does serve 4H — this must not be hardcoded to 6H either."""
    prompt = _ask(captured, "4h")
    assert not _mentions(prompt, "6h"), _mentions(prompt, "6h")
    assert _mentions(prompt, "4h")


def test_the_liquidity_pool_line_names_the_same_frame(captured):
    """It describes a level derived from the HTF candles, so it must agree with them."""
    assert "Nearest 6H liquidity pool target" in _ask(captured, "6h")


def test_the_setup_summary_stays_aligned(captured):
    """The BOS row is in a fixed-width block; a shorter/longer name must not skew it."""
    for name in ("6h", "4h"):
        rows = [ln for ln in _ask(captured, name).splitlines() if ":" in ln
                and ln.startswith(("Symbol", "Direction", "Current Price", "Daily Trend",
                                   "Swept level", name.upper() + " BOS"))]
        cols = {ln.index(":") for ln in rows}
        assert len(cols) == 1, f"{name}: colons misaligned at {sorted(cols)}\n" + "\n".join(rows)


def test_the_unavailable_branch_also_names_the_right_frame(captured):
    binance_bot.get_ai_confirmation(
        "BTC/USD", 100.0, "bullish", "bullish", 99.0, 101.0, 98.0,
        97.0, 3.0, 110.0, _df(), None, htf_name="6h")
    assert "(6H data unavailable)" in captured["prompt"]


def test_the_default_matches_the_bots_configured_fallback(captured):
    """Called without the argument, it must not invent a frame."""
    binance_bot.get_ai_confirmation(
        "BTC/USD", 100.0, "bullish", "bullish", 99.0, 101.0, 98.0,
        97.0, 3.0, 110.0, _df(), _df())
    assert binance_bot.HTF_TIMEFRAME.upper() in captured["prompt"]

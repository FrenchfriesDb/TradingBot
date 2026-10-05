"""An empty `content` is not an outage, and must not be reported as one.

LIVE, 2026-10-05 06:51, with the model confirmed up:

    🤖 AI model live: nvidia/nemotron-3-super-120b-a12b
    [META] 🤖 AI Bot Approval: ⚠️ NO OPINION  R:R=1:1.0
           AI unavailable ('NoneType' object has no attribute 'strip') — proceeding on technicals

Both bots did `resp.choices[0].message.content.strip()`. Reasoning models
(nvidia/nemotron-3-super-*, deepseek-r1) put their chain of thought in a SEPARATE field and
can return content=None outright — usually when the whole max_tokens budget went on
reasoning. So the model ANSWERED, the code crashed on the reply, and the log blamed the
API. The operator went looking for a network problem that did not exist.

Two things matter here and they pull in opposite directions:
  * the reply must be read when it exists, including out of the reasoning field;
  * an empty reply must still STAND ASIDE, not fail open. A gate that approves whenever it
    cannot read the answer is not a gate.
"""
import pytest

from bot import ai_model as A


class _Msg:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _Choice:
    def __init__(self, message, finish_reason="stop"):
        self.message, self.finish_reason = message, finish_reason


class _Resp:
    def __init__(self, choices):
        self.choices = choices


def _resp(finish="stop", **fields):
    return _Resp([_Choice(_Msg(**fields), finish)])


class TestReplyText:
    def test_plain_content(self):
        assert A.reply_text(_resp(content="DECISION: YES")) == "DECISION: YES"

    def test_none_content_falls_back_to_reasoning(self):
        """The actual live failure."""
        r = _resp(content=None, reasoning_content="...so DECISION: NO")
        assert "DECISION: NO" in A.reply_text(r)

    def test_none_content_falls_back_to_reasoning_alt_field(self):
        assert "NO" in A.reply_text(_resp(content=None, reasoning="DECISION: NO"))

    def test_content_wins_over_reasoning(self):
        r = _resp(content="DECISION: YES", reasoning_content="maybe NO")
        assert A.reply_text(r) == "DECISION: YES"

    def test_whitespace_only_content_is_not_a_reply(self):
        r = _resp(content="   ", reasoning_content="DECISION: NO")
        assert "DECISION: NO" in A.reply_text(r)

    @pytest.mark.parametrize("bad", [
        _Resp([]), _Resp(None), _resp(content=None), _resp(), object(),
    ])
    def test_never_raises(self, bad):
        """It runs inside the entry path. A crash here reads as 'AI unavailable' and
        hides a model that actually answered."""
        assert A.reply_text(bad) == ""


class TestFinishHint:
    def test_length_is_called_out(self):
        """The actionable cause of an empty reasoning reply — raise max_tokens, don't
        debug the network."""
        assert "max_tokens" in A.finish_hint(_resp("length", content=None))

    def test_stop_is_unremarkable(self):
        assert A.finish_hint(_resp("stop", content="x")) == ""

    def test_never_raises(self):
        assert A.finish_hint(object()) == ""


class TestCallersUseIt:
    @pytest.mark.parametrize("rel", ["bot/strategy.py", "binance_bot.py"])
    def test_no_bot_dereferences_content_directly(self, rel):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1] / rel).read_text()
        assert "message.content.strip()" not in src, (
            f"{rel} still calls .strip() straight on content — None crashes it")
        assert "reply_text(" in src, f"{rel} must read the reply through ai_model.reply_text"

    @pytest.mark.parametrize("rel", ["bot/strategy.py", "binance_bot.py"])
    def test_empty_reply_stands_aside(self, rel):
        """Must not fail OPEN. An unreadable answer is not an approval."""
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1] / rel).read_text()
        i = src.index("reply_text(")
        window = src[i:i + 400]
        assert "EMPTY reply" in window and "False" in window, (
            f"{rel} does not stand aside on an empty reply")
